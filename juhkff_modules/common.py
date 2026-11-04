"""juhkff 综合插件的公共设施：路径、JSON 读写、定时调度、模块基类。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from abc import ABC
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context

from .http_utils import public_request

# Reuse AstrBot's path and temporary-image helpers. Remote requests use the
# shared bounded public HTTP client instead of an unrestricted core downloader.
try:
    from astrbot.core.utils.io import (
        ensure_dir as _builtin_ensure_dir,
    )
    from astrbot.core.utils.io import (
        save_temp_img as _builtin_save_temp_img,
    )
except Exception:  # pragma: no cover - 兼容 AstrBot 内部结构调整
    _builtin_ensure_dir = None
    _builtin_save_temp_img = None

PLUGIN_NAME = "astrbot_plugin_juhkff"
DEFAULT_TZ = "Asia/Shanghai"


# ---------------------------------------------------------------- 路径


def data_dir() -> Path:
    """插件数据目录 data/plugin_data/<插件名>/，不存在则创建。

    优先问 AstrBot 要标准路径（换部署目录也不会丢数据）；
    拿不到时退回相对路径，保证插件在非标准环境里也能跑。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path())
    except Exception:  # 兼容无法导入内部工具的情况
        base = Path("data") / "plugin_data"
    path = base / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def subdir(name: str) -> Path:
    """数据子目录，复用 AstrBot 的 ensure_dir。"""
    path = data_dir() / name
    if _builtin_ensure_dir is not None:
        _builtin_ensure_dir(path)
    else:
        path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- JSON


def load_json(path: Path | str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except FileNotFoundError:
        return default
    except Exception as exc:
        logger.warning(f"[juhkff] 读取 {path} 失败: {exc}")
        return default


def save_json(path: Path | str, data: Any) -> None:
    """原子写 JSON：先写 .tmp，再 os.replace 换名。

    避免进程在写一半时被杀导致状态文件损坏（表情库、指纹表都用它）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 时间


def get_tz(name: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo("UTC")


def now(tz_name: str | None = None) -> datetime:
    return datetime.now(get_tz(tz_name))


def next_daily_at(hhmm: str, tz_name: str | None = None) -> datetime:
    """返回下一个 HH:MM 时刻（今天的还没到就是今天，否则明天）。

    解析失败时按 23:59 处理，不抛异常——配置写错不应该让定时任务挂掉。
    """
    tz = get_tz(tz_name)
    try:
        hour, minute = (int(x) for x in hhmm.split(":", 1))
    except Exception:
        hour, minute = 23, 59
    current = datetime.now(tz)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target


# ---------------------------------------------------------------- 调度


def _to_crontab(expression: str) -> str:
    """把上游的 Quartz 6 段表达式（`0 0 4 * * ?`）转成 5 段 crontab。

    秒字段 AstrBot 的 CronTrigger 用不上，直接丢掉；`?` 等价于 `*`；
    Quartz 的 `0/5` 步进写法改成 crontab 的 `*/5`。
    """
    parts = [p for p in str(expression).replace("?", "*").split() if p]
    if len(parts) >= 6:
        # 6 段：秒 分 时 日 月 周 → 丢掉秒，取中间 5 段
        parts = parts[1:6]
    if len(parts) != 5:
        return "0 4 * * *"
    fixed: list[str] = []
    for part in parts:
        match = re.fullmatch(r"(\d+)/(\d+)", part)
        fixed.append(f"*/{match.group(2)}" if match else part)
    return " ".join(fixed)


class PluginLike(Protocol):
    """插件实例暴露给模块的最小接口（`main.JuhkffPlugin` 实现它）。

    全部声明成只读 property 而不是可变属性：可变成员在 Protocol 里要求
    严格不变（invariant），`JuhkffPlugin` 的 `config` 是 `AstrBotConfig`
    子类、`global_enabled` 是 property，都会被判为不兼容。只读成员允许
    协变，子类/属性实现都能满足。
    """

    @property
    def config(self) -> dict[str, Any]: ...

    @property
    def global_enabled(self) -> bool: ...

    @property
    def version(self) -> str: ...

    @property
    def modules(self) -> Mapping[str, BaseModule]: ...

    @property
    def context(self) -> Context: ...

    @property
    def scheduler(self) -> Scheduler: ...


class Scheduler:
    """定时任务的薄封装，直接复用 AstrBot 的 CronJobManager。

    CronJobManager 基于 APScheduler，自带时区、crontab 解析与执行失败日志，
    插件没必要再自己写一套 sleep 循环。这里只做两件事：
    1. 把「HH:MM」「每 N 分钟」翻译成 5 段 crontab；
    2. 记录 job_id，插件卸载时删掉，避免残留任务。

    `persistent=False`：这些是插件运行期任务，重启后由插件重新注册，
    不应该进 DB 的持久任务列表（否则重载插件会重复堆积）。
    """

    def __init__(self, context: Context, name: str = PLUGIN_NAME) -> None:
        self.context = context
        self.name = name
        self._jobs: list[str] = []

    def _wrap(
        self, fn: Callable[[], Awaitable[Any]], label: str
    ) -> Callable[[], Awaitable[None]]:
        async def runner() -> None:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[juhkff] 定时任务 {label} 执行失败")

        return runner

    async def _add(
        self,
        expression: str,
        fn: Callable[[], Awaitable[Any]],
        label: str,
        tz_name: str | None,
    ) -> str | None:
        try:
            job = await self.context.cron_manager.add_basic_job(
                name=f"{self.name}:{label}",
                cron_expression=expression,
                handler=self._wrap(fn, label),
                description=f"{self.name} 插件的内部定时任务：{label}",
                timezone=tz_name or DEFAULT_TZ,
                persistent=False,
            )
        except Exception:
            logger.exception(f"[juhkff] 注册定时任务 {label} 失败（{expression}）")
            return None
        self._jobs.append(job.job_id)
        return job.job_id

    async def daily(
        self,
        hhmm: str,
        fn: Callable[[], Awaitable[Any]],
        label: str,
        tz_name: str | None = None,
    ) -> str | None:
        try:
            hour, minute = (int(x) for x in str(hhmm).split(":", 1))
        except Exception:
            hour, minute = 23, 59  # 配置写错时退到 23:59，而不是报错
        expression = f"{minute} {hour} * * *"
        return await self._add(expression, fn, label, tz_name)

    async def cron(
        self,
        expression: str,
        fn: Callable[[], Awaitable[Any]],
        label: str,
        tz_name: str | None = None,
    ) -> str | None:
        """接受 5 段 crontab，或上游那种 Quartz 6 段表达式（`0 0 4 * * ?`）。"""
        return await self._add(_to_crontab(expression), fn, label, tz_name)

    async def every_minutes(
        self,
        minutes: float,
        fn: Callable[[], Awaitable[Any]],
        label: str,
        tz_name: str | None = None,
    ) -> str | None:
        # crontab 没有「每 N 分钟」以外的粒度，按区间近似：
        # < 1 小时 → */N 分钟；< 1 天 → 整点每 N 小时；再大 → 每天 0 点
        total = max(1, int(minutes))
        if total < 60:
            expression = f"*/{total} * * * *"
        elif total < 1440:
            hours = max(1, total // 60)
            expression = "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
        else:
            expression = "0 0 * * *"
        return await self._add(expression, fn, label, tz_name)

    async def shutdown(self) -> None:
        for job_id in list(self._jobs):
            try:
                await self.context.cron_manager.delete_job(job_id)
            except Exception:
                logger.warning(f"[juhkff] 删除定时任务 {job_id} 失败")
        self._jobs.clear()


# ---------------------------------------------------------------- 模块基类


class BaseModule(ABC):
    """所有功能模块的统一接口。

    约定：
    - key   : 配置里的分组名
    - title : 展示名
    - start/stop : 生命周期（注册定时任务、初始化连接）
    - handle : 收到消息时调用，返回 MessageChain 表示需要回复，返回 None 表示不处理
    """

    key: str = ""
    title: str = ""

    def __init__(self, plugin: PluginLike) -> None:
        self.plugin = plugin

    # -- 配置

    @property
    def cfg(self) -> dict[str, Any]:
        """本模块在配置页里对应分组的字典（`_conf_schema.json` 同名 object）。"""
        return self.plugin.config.get(self.key) or {}

    @property
    def enabled(self) -> bool:
        """总开关 + 分组开关同时为真才算启用。"""
        return bool(self.plugin.global_enabled and self.cfg.get("enable", False))

    @property
    def scheduler(self) -> Scheduler:
        return self.plugin.scheduler

    @property
    def debug_enabled(self) -> bool:
        """配置页「全局设置 → 调试日志」是否打开（`global.debug`）。"""
        return bool((self.plugin.config.get("global") or {}).get("debug", True))

    def debug(self, msg: str, **kwargs: Any) -> None:
        """Log details at INFO by default, or DEBUG when explicitly disabled.

        Args:
            msg: Detail message for the plugin log.
            **kwargs: Additional arguments forwarded to the logger.
        """
        if self.debug_enabled:
            logger.info(msg, **kwargs)
        else:
            logger.debug(msg, **kwargs)

    @property
    def proxy(self) -> str:
        """全局代理（`global.proxy`），空字符串表示直连。"""
        return (self.plugin.config.get("global") or {}).get("proxy", "") or ""

    @property
    def tz_name(self) -> str:
        """时区名，用于「今天」的边界判断和定时任务。"""
        return (self.plugin.config.get("global") or {}).get("timezone") or DEFAULT_TZ

    # -- 生命周期

    async def start(self) -> None:  # noqa: D401 - 子类按需覆盖
        """模块启用时调用。"""

    async def stop(self) -> None:  # noqa: D401
        """模块停用时调用。"""

    # -- 帮助

    def help_entries(self) -> list[tuple[str, str]]:
        """[(用法, 说明), ...]，供 `#帮助` 汇总。"""
        return []

    # -- 消息

    def intercepts(self, event: AstrMessageEvent) -> bool:
        """Claim a command or an interactive input before passive handlers.

        Args:
            event: Incoming message after AstrBot's wake checks.

        Returns:
            Whether this module should exclusively handle the message.
        """
        return False

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        """处理一条消息，返回待发送的 MessageChain 或 None。"""
        return None


# ---------------------------------------------------------------- 文本


def message_text(event: AstrMessageEvent) -> str:
    """取消息纯文本；`message_str` 为空时退回拼接 Plain 段。"""
    text = event.message_str
    if text:
        return text
    return "".join(
        seg.text for seg in event.message_obj.message or [] if isinstance(seg, Plain)
    )


_UMO_TYPE_FIX = {"group": "GroupMessage", "friend": "FriendMessage"}


def resolve_umo(context: Context, target: str) -> str:
    """把「群号」或「UMO」统一成合法的 unified_msg_origin。

    AstrBot 的 umo 形如 `平台实例ID:GroupMessage:群号`，三段缺一不可，
    直接拿群号拼 `aiocqhttp:group:123` 会因为 MessageType 大小写不对而抛
    `ValueError: 不合法的 session 字符串`。这里做两件事：

    - 已经是三段式：把历史配置里小写的 `group` / `friend` 修正成枚举值；
    - 只有群号：找第一个 aiocqhttp 平台实例，拼出完整 umo。
    """
    target = str(target or "").strip()
    if not target:
        return ""
    parts = target.split(":", 2)
    if len(parts) == 3:
        parts[1] = _UMO_TYPE_FIX.get(parts[1].lower(), parts[1])
        return ":".join(parts)

    platform_id = ""
    try:
        insts = list(context.platform_manager.platform_insts)
    except Exception:
        insts = []
    for inst in insts:
        meta = inst.meta()
        if str(getattr(meta, "name", "")) == "aiocqhttp":
            platform_id = str(getattr(meta, "id", "") or "")
            break
    if not platform_id and insts:
        platform_id = str(getattr(insts[0].meta(), "id", "") or "")
    return f"{platform_id or 'aiocqhttp'}:GroupMessage:{target}"


def truncate(text: str, limit: int = 120) -> str:
    """压成一行并截断，超长用省略号。用于日志、prompt 和摘要。"""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- HTTP


async def fetch_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    proxy: str | None = None,
    timeout: float = 20.0,
    max_bytes: int = 3 * 1024 * 1024,
    allowed_hosts: set[str] | None = None,
) -> Any:
    """Read bounded public JSON, validating and pinning every redirect target.

    Args:
        url: Public HTTP or HTTPS endpoint.
        params: Optional query parameters appended to the existing query.
        headers: Optional caller-controlled request headers.
        proxy: Explicit proxy configured by the operator.
        timeout: Total request deadline, including redirects and DNS.
        max_bytes: Maximum decompressed response size.
        allowed_hosts: Optional exact host allowlist for all hops.

    Returns:
        The decoded JSON response.
    """
    if params:
        parsed = urlsplit(url)
        query = "&".join(
            part for part in (parsed.query, urlencode(params, doseq=True)) if part
        )
        url = urlunsplit(parsed._replace(query=query))
    payload, _ = await public_request(
        url,
        headers=headers,
        proxy=proxy or None,
        timeout=timeout,
        max_bytes=max_bytes,
        allowed_hosts=allowed_hosts,
    )
    return json.loads(payload)


async def fetch_bytes(
    url: str,
    *,
    headers: dict[str, Any] | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
    max_bytes: int = 64 * 1024 * 1024,
    allowed_hosts: set[str] | None = None,
) -> bytes:
    """Read bounded public bytes without changing or compressing the payload.

    Args:
        url: Public HTTP or HTTPS resource.
        headers: Optional caller-controlled request headers.
        proxy: Explicit proxy configured by the operator.
        timeout: Total request deadline, including redirects and DNS.
        max_bytes: Maximum decompressed response size, independent of compression.
        allowed_hosts: Optional exact host allowlist for all hops.

    Returns:
        The original response bytes.
    """
    payload, _ = await public_request(
        url,
        headers=headers,
        proxy=proxy or None,
        timeout=timeout,
        max_bytes=max_bytes,
        allowed_hosts=allowed_hosts,
    )
    return payload


async def download_image_bytes(
    url: str,
    *,
    headers: dict[str, Any] | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> bytes:
    """Download public image bytes with the shared redirect and size protections.

    Args:
        url: Public HTTP or HTTPS image resource.
        headers: Optional request headers such as Referer.
        proxy: Explicit proxy configured by the operator.
        timeout: Total request deadline.

    Returns:
        Original response bytes bounded to the common image download limit.
    """
    return await fetch_bytes(url, headers=headers, proxy=proxy, timeout=timeout)


def save_temp_image(data: bytes, suffix: str = ".png") -> str:
    """把图片字节写进临时文件，优先复用 AstrBot 的 `save_temp_img`。"""
    if _builtin_save_temp_img is not None:
        try:
            return _builtin_save_temp_img(data)
        except Exception as exc:
            logger.debug(f"[juhkff] 内置 save_temp_img 失败，回退临时文件: {exc}")
    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix,
        prefix="juhkff_",
    ) as fp:
        fp.write(data)
        return fp.name
