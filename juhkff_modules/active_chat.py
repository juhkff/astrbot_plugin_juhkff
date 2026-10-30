"""主动群聊：行为与配置项对齐上游 `src/apps/autoReply.ts`。

和上游一致的部分：

- 触发条件：**被 @ 或被引用回复**且 `replyAtBot` 时必回；否则按
  `defaultChatRate` / `groupChatRate` 里的概率掷骰子；**没有冷却、没有最小消息数**；
- 指令消息：以唤醒前缀（AstrBot 全局 `wake_prefix`，默认 `/`）开头的消息、
  `@全体成员` 这类「被唤醒但不是在叫机器人」的消息，既不写历史也不回复，
  避免 `/统计`、`/juhkff` 被当成闲聊抢答。判断直接复用框架置好的
  `event.is_at_or_wake_command`，再用「真 @ / 引用回复」把它和本次要接的话区分开；
- 上下文：`useContext` 打开时，把最近 `maxHistoryLength` 条群消息作为
  `contexts` 结构化传给 `llm_generate`（别人的是 user、自己的是 assistant）；
- 历史存储：只存 Redis（LIST，key 为 `前缀:umo`，只留最近 64 条），内存里
  不保留副本，每次要上下文时现拉；和上游 `loadContext()` 每次从 Redis 读
  的做法一致。每条写入都刷新 `history_ttl_hours`（默认 12 小时，和上游
  `EX 12*60*60` 对齐），0 表示不过期。`clear_history_on_start`（默认开）在
  AstrBot 进程真正重启时清空该前缀下的所有会话；同一进程里的插件热重载
  （比如在 WebUI 保存配置）不会清，靠 Redis 里的 `__owner` 标记区分；
  连不上 Redis 时不记录历史（只按当前消息回复），不会再退回内存；
- 人设：`chatPrompts` 是 `{name, prompt}` 列表，`chatPromptApply` 选择用哪条；
- 模型：直接用 AstrBot 里配置的对话 provider（`provider` 留空则取当前会话的
  对话模型）；插件不再自己管理 Key / 模型名 / 接口地址；
- `attachUrlAnalysis`：先把消息里的链接抓下来正文前若干字，附在这条 user 消息里；
- 图片：当前回合的图片经 `image_urls` 交给模型看；历史里的图会以
  `ImageURLPart` 跟着上下文一起传（只存 http(s) 地址，不落 base64），
  所以后续几轮模型还能回看这些图；本地临时文件不进历史，图链过期取不到时
  会退回纯文本。`max_history_length` 窗口决定带多少张；
  正文里用同一套 `[图片1] [图片2]` 占位（本回合与历史一致）。
- 模型不支持图片时（`modalities` 里没有 `image`），按 AstrBot 核心
  `_ensure_img_caption()` 的做法，调用配置里的「图片转述模型」
  （provider_settings.default_image_caption_provider_id）把图转成文字；
  转述模型没配或失败，就退纯文本。带图调用报错时也会剥掉上下文里的图重试一次。
- `debugMode`：把 system 与本条 user 消息打进日志。

不再有：随机短句模式、冷却间隔、关键词触发、群白名单。这些是之前版本
自己加的，上游没有，已删除。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import random
import re
import time
from datetime import datetime
from typing import Any, Awaitable, cast
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp

try:  # 没装 redis 客户端时只是不记录历史，不影响插件加载
    import redis.asyncio as redis_asyncio
except Exception:  # pragma: no cover - 依赖缺失时才走到
    redis_asyncio = None  # type: ignore[assignment]

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Reply
from astrbot.api.platform import MessageType
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    ImageURLPart,
    Message,
    TextPart,
    UserMessageSegment,
)

from .common import BaseModule, PluginLike, truncate

DEFAULT_PROMPT = (
    "你在一个QQ群聊里，扮演一名普通群员参与聊天。\n"
    "只输出一条要发到群里的消息：不要用括号写动作或旁白，不要换行，不要以「回复xx」开头，句尾不加标点；不要解释思路，也不要带日期和昵称。\n"
    "你在群里的昵称是{{botName}}。\n"
    "\n"
    "请像普通群员那样接着最新这条说一句话，只输出要发的内容本身。\n"
    "更早的消息只是聊天背景：不要回复它们，不要续写它们，也不要复述里面的"
    "「当时」之类标记。"
)

HISTORY_MAX = 64
DEFAULT_PREFIX = "juhkff:active_chat"

_URL_RE = re.compile(r"https?://[^\s，。！？、）)]+")
_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.S | re.I)


class ActiveChatModule(BaseModule):
    key = "active_chat"
    title = "主动群聊"

    def __init__(self, plugin: PluginLike) -> None:
        super().__init__(plugin)
        # 历史只存在 Redis，内存里不留副本（对齐上游 loadContext 的做法）。
        # 没有 Redis 时也照常回复，只是没有上下文。
        self._redis: Any = None
        # 从配置直接取前缀，这样即使没有 start()（例如热重载后只读历史）key 也一致
        self._prefix = str(self.cfg.get("redis_key_prefix") or DEFAULT_PREFIX)
        # 机器人昵称缓存：self_id -> 昵称（取不到就存 QQ 号）
        self._bot_names: dict[str, str] = {}

    def help_entries(self):
        return [
            ("（自动）", "群聊里按概率接话；@机器人时按人格回复"),
            ("/juhkff", "查看各模块开关与运行状态"),
        ]

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        await self._init_redis()
        logger.info(
            f"[juhkff] 主动群聊已就绪：默认概率 "
            f"{self.cfg.get('default_chat_rate', 0.1)}，"
            f"分群覆盖 {len(self.cfg.get('group_chat_rate') or [])} 条，"
            f"历史后端 {'Redis' if self._redis else '未连接 Redis（不记录历史）'}"
        )

    async def stop(self) -> None:
        # 退出前断开连接；不断开的话 AstrBot 热重载会留下半死连接
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                self.debug("[juhkff] 关闭 Redis 连接时出错", exc_info=True)
            self._redis = None

    # ------------------------------------------------------------ 历史存储

    def _key(self, umo: str) -> str:
        """会话在 Redis 里的 key。"""
        return f"{self._prefix}:{umo}"

    async def _init_redis(self) -> None:
        """连 Redis；连不上就不记录历史，不影响插件其它功能。

        `redis_url` 为空、没装 redis 客户端、服务没起，都会走「无历史」模式，
        只是日志里会提示一句。
        """
        url = str(self.cfg.get("redis_url") or "").strip()
        if not url:
            return
        if redis_asyncio is None:
            logger.warning(
                "[juhkff] 没安装 redis 客户端，主动群聊历史将不会记录"
            )
            return
        try:
            client = redis_asyncio.from_url(url, decode_responses=True)
            await client.ping()
        except Exception as exc:
            logger.warning(
                f"[juhkff] 连接 Redis 失败（{exc}），主动群聊历史将不会记录"
            )
            return
        self._redis = client
        self._prefix = str(self.cfg.get("redis_key_prefix") or DEFAULT_PREFIX)
        if bool(self.cfg.get("clear_history_on_start", True)):
            await self._clear_history_if_new_process(client)
        logger.info(f"[juhkff] 主动群聊历史已启用 Redis：{self._mask_url(url)}")

    async def _clear_history_if_new_process(self, client: Any) -> None:
        """只在「AstrBot 换了进程」时清空历史。

        WebUI 保存插件配置会让 AstrBot 热重载插件（`stop()` + `start()`），
        如果每次 `start()` 都清，用户改个概率就把聊天记录洗没了。用一个
        `__owner` 键记录上次启动的 pid：pid 相同说明只是热重载，保留历史；
        pid 不同（重启、崩溃拉起）才清，语义和配置项描述一致。
        """
        owner_key = f"{self._prefix}:__owner"
        try:
            owner = await client.get(owner_key)
        except Exception:
            owner = None
        if owner is not None and str(owner) == str(os.getpid()):
            logger.info("[juhkff] 同一进程热重载，保留 Redis 历史")
            return
        deleted = 0
        try:
            async for key in client.scan_iter(match=f"{self._prefix}:*"):
                deleted += int(await client.delete(key) or 0)
            await client.set(owner_key, str(os.getpid()))
        except Exception:
            logger.warning("[juhkff] 清空 Redis 历史失败", exc_info=True)
            return
        logger.info(f"[juhkff] 首次启动，已清空 Redis 历史 {deleted} 个会话")

    def _ttl_seconds(self) -> int:
        """单条历史的过期时间（秒）。0 表示不过期。"""
        try:
            hours = float(self.cfg.get("history_ttl_hours", 12) or 0)
        except (TypeError, ValueError):
            hours = 12.0
        return max(0, int(hours * 3600))

    async def _fetch_history(self, umo: str) -> list[dict[str, Any]]:
        """从 Redis 读回该会话的历史（按写入顺序，旧的在前）。

        内存里不保留副本，每轮需要上下文时现拉；Redis 不可用时返回空列表，
        调用方按「没有上下文」处理。
        """
        if self._redis is None:
            return []
        try:
            raw = await self._redis.lrange(self._key(umo), 0, -1)
        except Exception:
            logger.warning("[juhkff] 读取 Redis 历史失败", exc_info=True)
            return []
        history: list[dict[str, Any]] = []
        for item in raw:
            try:
                entry = json.loads(item)
            except Exception:
                continue
            if isinstance(entry, dict):
                history.append(entry)
        return history

    async def _append(self, umo: str, entry: dict[str, Any]) -> None:
        """把一条历史追加进 Redis 列表。

        RPUSH + LTRIM 保证只留最近 HISTORY_MAX 条，顺手刷新整个 key 的
        TTL（`history_ttl_hours`）——和上游每条消息 `EX 12*60*60` 的效果
        一致：最后一条消息之后过 12 小时，这段历史自然过期。
        写失败只记日志，本回合照常回复，只是这段上下文不会持久化。
        """
        if self._redis is None:
            return
        key = self._key(umo)
        try:
            pipe = self._redis.pipeline()
            pipe.rpush(key, json.dumps(entry, ensure_ascii=False))
            pipe.ltrim(key, -HISTORY_MAX, -1)
            ttl = self._ttl_seconds()
            if ttl > 0:
                pipe.expire(key, ttl)
            await pipe.execute()
        except Exception:
            logger.warning("[juhkff] 写入 Redis 历史失败", exc_info=True)

    # ------------------------------------------------------------ 消息

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        # 只处理群聊：私聊不主动接话
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return None

        umo = event.unified_msg_origin
        if not umo:
            return None
        # 不记录、不回复自己发的消息（否则会自问自答）
        if self._is_self(event):
            return None

        text = (event.message_str or "").strip()
        sender = _sender(event)
        called = self._called(event)
        # 被唤醒、但并不是在「叫」机器人（唤醒前缀 / @全体 之类）的消息是指令：
        # 不参与主动群聊，也不写进聊天历史。框架已经把这个判断好了
        # （`WakingCheckStage` 剥前缀后置位 is_at_or_wake_command），直接复用即可。
        if event.is_at_or_wake_command and not called:
            return None
        # 插件自己的 `#xxx` 指令（#帮助 / #日报 …）同理，交给对应模块处理
        if text.startswith("#"):
            return None

        # 先把历史取出来，再写本回合这条消息。
        # 不能反过来「先写、再从列表尾部丢掉最后一条」：Redis 列表是所有消息
        # 共享的，两条消息并发处理时丢掉的可能是别人刚写进去的那条。
        history = await self._recent_history(umo)

        # 记进历史（无论本回合是否回复）。纯文本只记文本；带图的补 `[图片N]`
        # 占位（AstrBot 自己的 get_message_outline 对图片也是这个占位）。
        images = self._images(event)
        # 历史里只存 http(s) 地址：本地临时文件会被清理，下次看不到；
        # 需要下载的（file:// 之类）等真正要回复时再解析（见 `_reply_image_urls`）。
        image_refs = [
            ref
            for comp in images
            if (ref := self._http_ref(comp))
        ]
        text_part = truncate(text, 300).strip() if text else ""
        if image_refs:
            # 编号只给真正存进历史、能再看到的图，保证 [图片N] 和 entry["images"]
            # 一一对应；本地图/下载失败的图不占编号
            marks = " ".join(f"[图片{i}]" for i in range(1, len(image_refs) + 1))
            entry_text = f"{text_part} {marks}".strip()
        elif images:
            entry_text = f"{text_part} [图片]".strip()
        else:
            # 只有 @/唤醒词、没有文字也没有图，也要留个能看懂的痕迹
            entry_text = text_part or "[空白消息]"
        # @ 段在适配器那一层就被从 message_str 里剥掉了，历史里补一个标记，
        # 让后续回合能看出「这条是在叫我」，否则所有消息看起来都像普通闲聊
        if self._at_me(event):
            entry_text += "（当时@了你）"
        elif self._replied_to_me(event):
            entry_text += "（当时引用了你）"
        entry: dict[str, Any] = {
            "name": sender,
            "text": entry_text,
            "ts": time.time(),
            "self": False,
        }
        if image_refs:
            entry["images"] = image_refs
        await self._append(umo, entry)

        # 掷骰子决定这次要不要接话；不接就静默返回，等后续消息
        if not self._should_speak(umo, called):
            return None

        # 真要回复时才解析本回合的图（可能要下载到临时文件），避免每条带图
        # 的群消息都被白白下载一遍
        current_images = await self._reply_image_urls(images) if images else []
        line = await self._generate(
            umo, text, sender, called, event, current_images, history
        )
        if not line:
            # 已经掷到「要回」但生成失败（没配模型/请求报错）：结束事件，
            # 否则框架的默认对话会补一句，而且那句不会进我们的历史
            event.stop_event()
            return None

        # 先发出去，发送成功之后才把「我」写进历史：否则群里没出现的话
        # 会进下一轮上下文，模型会以为自己说过。
        chain = MessageChain().message(line)
        try:
            await event.send(chain)
        except Exception:
            logger.warning("[juhkff] 主动群聊：发送失败，这句话不写进历史", exc_info=True)
            event.stop_event()
            return None
        event.stop_event()
        await self._append(
            umo, {"name": "我", "text": line, "ts": time.time(), "self": True}
        )
        return None

    # ------------------------------------------------------------ 判定

    def _should_speak(self, umo: str, called: bool) -> bool:
        """被 @/引用 且允许时必回；否则按概率随机决定。"""
        rate, reply_at_bot = self._rates(umo)
        if called and reply_at_bot:
            return True
        return random.random() < rate

    def _rates(self, umo: str) -> tuple[float, bool]:
        rate = float(self.cfg.get("default_chat_rate", 0.1) or 0)
        reply_at_bot = bool(self.cfg.get("default_reply_at_bot", True))
        # 分群覆盖：配置里按群号或完整 umo 匹配，命中第一条即生效
        for rule in self.cfg.get("group_chat_rate") or []:
            groups = [str(x) for x in (rule.get("group_list") or [])]
            if umo in groups or _group_id(umo) in groups:
                if rule.get("chat_rate") is not None:
                    rate = float(rule["chat_rate"])
                if rule.get("reply_at_bot") is not None:
                    reply_at_bot = bool(rule["reply_at_bot"])
                break
        return rate, reply_at_bot

    def _tzinfo(self) -> ZoneInfo | None:
        """插件配置里的时区（`global.timezone`），无效时返回 None 用本地时区。

        `self.tz_name` 是 `BaseModule` 的属性，读的才是 `config["global"]["timezone"]`；
        直接读配置根上的 `timezone` 是不存在的。
        """
        try:
            return ZoneInfo(self.tz_name)
        except Exception:
            return None

    @staticmethod
    def _mask_url(url: str) -> str:
        """日志里隐藏 Redis URL 里的密码。"""
        try:
            parts = urlsplit(url)
            if parts.password is None:
                return url
            host = parts.hostname or ""
            netloc = f"{parts.username}:***@{host}"
            if parts.port:
                netloc += f":{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        except Exception:
            return "redis://***"

    def _format_ts(self, ts: Any) -> str:
        """消息时间戳 → `HH:MM`（当天）或 `MM-DD HH:MM`（跨天）。

        上游 `formatDateDetail()` 用完整的 `YYYY/MM/DD HH:mm:ss`；这里保留「能看出
        新旧」的信息，又不必每行都塞一长串日期。时间按插件配置的 `global.timezone`
        解释，和 AstrBot 定时任务用的时区保持一致，而不是宿主机的本地时区。
        """
        tz = self._tzinfo()
        try:
            moment = datetime.fromtimestamp(float(ts), tz) if tz else datetime.fromtimestamp(float(ts))
        except (TypeError, ValueError, OSError):
            return ""
        now = datetime.now(tz) if tz else datetime.now()
        if moment.date() == now.date():
            return moment.strftime("%H:%M")
        return moment.strftime("%m-%d %H:%M")

    @staticmethod
    def _at_me(event: AstrMessageEvent) -> bool:
        """消息里有没有 `@机器人` 这一段（不含唤醒词、不含 @全体）。"""
        self_id = str(event.get_self_id())
        return any(
            isinstance(seg, At) and str(seg.qq) == self_id
            for seg in (event.message_obj.message or [])
        )

    @staticmethod
    def _replied_to_me(event: AstrMessageEvent) -> bool:
        """是不是引用回复了机器人某条消息。"""
        self_id = str(event.get_self_id())
        return any(
            isinstance(seg, Reply) and str(getattr(seg, "sender_id", "")) == self_id
            for seg in (event.message_obj.message or [])
        )

    @classmethod
    def _called(cls, event: AstrMessageEvent) -> bool:
        """这一条是不是在「叫」机器人：真 @ 或被引用回复。

        不能用 `event.is_at_or_wake_command`：`/统计`、`/juhkff` 这类唤醒前缀
        指令也会让它为真，`default_reply_at_bot` 打开时就会去抢指令消息。
        """
        return cls._at_me(event) or cls._replied_to_me(event)

    @staticmethod
    def _is_self(event: AstrMessageEvent) -> bool:
        return event.get_self_id() == event.get_sender_id()

    # ------------------------------------------------------------ 图片

    @staticmethod
    def _images(event: AstrMessageEvent) -> list[Image]:
        """只取这条消息自带的图片，引用消息里的不算。"""
        return [
            seg for seg in (event.message_obj.message or []) if isinstance(seg, Image)
        ]

    @staticmethod
    def _http_ref(comp: Image) -> str:
        """取这条图片的 http(s) 地址（`Image.fromURL()` 把地址放在 `file` 里）。

        本地路径/临时文件不返回：它们会被清理，存进历史也对不上。
        """
        for candidate in (str(comp.url or ""), str(comp.file or "")):
            if candidate.startswith(("http://", "https://")):
                return candidate
        return ""

    @staticmethod
    async def _single_image_source(comp: Image) -> str:
        """单张图的可传地址：优先 http url，其次本地路径。"""
        # 适配器一般把 http 地址放在 url；也有只填 file 的实现
        for candidate in (str(comp.url or ""), str(comp.file or "")):
            if candidate.startswith(("http://", "https://")):
                return candidate
        try:
            return await comp.convert_to_file_path()
        except Exception:
            return ""

    @classmethod
    async def _reply_image_urls(cls, images: list[Image]) -> list[str]:
        """本回合要直接给模型看的图片地址（有几张传几张，不截断）。

        解析失败的单张图会被跳过，不影响其余图片。
        """
        urls: list[str] = []
        for comp in images:
            url = await cls._single_image_source(comp)
            if url:
                urls.append(url)
        return urls

    # ------------------------------------------------------------ 生成

    async def _system_prompt(self, event: AstrMessageEvent) -> str:
        """选出人设提示词，并把 {{botName}} 换成机器人在群里的昵称（拿不到用 QQ 号）。"""
        prompts = self.cfg.get("chat_prompts") or []
        selected = None
        apply_name = str(self.cfg.get("chat_prompt_apply") or "").strip()
        for item in prompts:
            if not isinstance(item, dict):
                continue
            if apply_name and item.get("name") == apply_name:
                selected = item
                break
        if selected is None and prompts and isinstance(prompts[0], dict):
            selected = prompts[0]
        text = str((selected or {}).get("prompt") or DEFAULT_PROMPT)

        bot_name = await self._bot_name(event)
        # 行为约束（只回最新一条等）直接写在人设里，由配置决定，代码不再额外拼接
        return text.replace("{{botName}}", bot_name or "机器人").rstrip()

    async def _bot_name(self, event: AstrMessageEvent) -> str:
        """机器人在当前平台的昵称。

        AstrBot 的 `AstrMessageEvent` 没有统一的「机器人昵称」接口，只有
        `get_self_id()`；OneBot 平台可以问 `get_login_info`。所以按
        `get_self_name()`（以后版本可能有）→ OneBot 接口 → QQ 号 的顺序取，
        结果按 self_id 缓存，避免每个回合都多一次请求。
        """
        self_id = str(event.get_self_id() or "")
        cached = self._bot_names.get(self_id)
        if cached:
            return cached
        name = ""
        getter = getattr(event, "get_self_name", None)
        if callable(getter):
            try:
                name = str(getter() or "").strip()
            except Exception:
                name = ""
        if not name:
            bot = getattr(event, "bot", None)
            call = getattr(bot, "call_action", None)
            if callable(call):
                try:
                    info = await asyncio.wait_for(
                        cast(Awaitable[Any], call("get_login_info")), timeout=3.0
                    )
                    if isinstance(info, dict):
                        name = str(
                            info.get("nickname") or info.get("nick") or ""
                        ).strip()
                except Exception:
                    name = ""
        if not name:
            name = self_id
        if name:
            self._bot_names[self_id] = name
        return name

    async def _recent_history(self, umo: str) -> list[dict[str, Any]]:
        """取要喂给模型的最近几条聊天（不含本回合这条）。"""
        if not bool(self.cfg.get("use_context", True)):
            return []
        limit = max(1, int(self.cfg.get("max_history_length", 5) or 5))
        return (await self._fetch_history(umo))[-limit:]

    def _build_contexts(
        self, history: list[dict[str, Any]], *, with_images: bool = True
    ) -> list[Message]:
        """把历史整理成 AstrBot 的上下文消息，直接传给 llm_generate(contexts=...)。

        别人的消息用 user 角色、前缀发言人昵称；自己的回复用 assistant 角色。
        这样模型能区分「谁在说话」，比拼成一段字符串更清晰。
        """
        contexts: list[Message] = []
        for item in history:
            text = str(item.get("text") or "")
            if not text:
                continue
            if item.get("self"):
                # 自己的话也带时间，时间只用于喂模型，不影响真正发出去的消息
                stamp = self._format_ts(item.get("ts"))
                said = f"{stamp} {text}" if stamp else text
                contexts.append(
                    AssistantMessageSegment(content=[TextPart(text=said)])
                )
                continue
            stamp = self._format_ts(item.get("ts"))
            head = f"{stamp} {item.get('name')}：{text}" if stamp else f"{item.get('name')}：{text}"
            parts: list[Any] = [TextPart(text=head)]
            # 历史图跟在文字后面，顺序与发送时一致；模型支持图片时能看到它们
            if with_images:
                for ref in item.get("images") or []:
                    parts.append(
                        ImageURLPart(image_url=ImageURLPart.ImageURL(url=str(ref)))
                    )
            contexts.append(UserMessageSegment(content=parts))
        return contexts

    def _build_user_message(
        self,
        sender: str,
        latest: str,
        called: bool,
        image_count: int = 0,
        at_me: bool = False,
    ) -> str:
        """拼这一条的 user 消息：`时间 发言人：内容 [图片1] [图片2]（提示）`。

        只放「这一条消息本身」；人设和规则在 system 里（见 `_system_prompt()`），
        不要把两者混在一起。
        图片编号和写历史用的是同一套 `[图片N]` 占位（见 `handle()`）。
        """
        text = (latest or "").strip()
        marks = " ".join(f"[图片{i}]" for i in range(1, image_count + 1))
        if text and marks:
            text = f"{text} {marks}"
        elif marks:
            text = marks
        elif not text:
            text = "[图片]" if image_count else "[空白消息]"
        # @ 段/引用段在进插件前就被剥掉了，模型看不到，这里把「在叫你」
        # 这件事明确写出来，否则它会把这句当成普通群聊
        if at_me:
            hint = "（这条是在@你，必须优先回复它）"
        elif called:
            hint = "（这条是在引用回复你，必须优先回复它）"
        else:
            hint = ""
        stamp = self._format_ts(time.time())
        return f"{stamp} {sender}：{text}{hint}" if stamp else f"{sender}：{text}{hint}"

    async def _generate(
        self,
        umo: str,
        latest: str,
        sender: str,
        called: bool,
        event: AstrMessageEvent,
        image_refs: list[str],
        history: list[dict[str, Any]],
    ) -> str | None:
        contexts = self._build_contexts(history)
        # 本回合消息里带图就直接交给模型看（主流模型都支持多模态）；
        # 图片 refs 在 handle() 里已经解析好，这里复用，避免重复下载
        image_urls = list(image_refs)
        # 图片编号写进正文，和历史的 `[图片1]` 格式保持一致
        user_message = self._build_user_message(
            sender, latest, called, len(image_urls), at_me=self._at_me(event)
        )
        url_extra = ""
        if bool(self.cfg.get("attach_url_analysis", False)):
            url_extra = await self._analyze_urls(latest)
            if url_extra:
                user_message += "\n\n参考链接内容：\n" + url_extra
        system_prompt = await self._system_prompt(event)

        if self.debug_enabled:
            # 分三段打印：system（人设与规则）、user（本轮这条消息本身）、历史概况。
            logger.info(
                f"[juhkff] 主动群聊请求\n"
                f"--- system ---\n{system_prompt}\n"
                f"--- user ---\n{user_message}\n"
                f"--- 历史 {len(contexts)} 条，附图 {len(image_urls)} 张 ---"
            )

        # 模型一律走 AstrBot 里配置的对话 provider：没指定就用当前会话的，
        # 再不行取第一个可用的。插件自己不再维护 Key/模型/地址。
        provider_id = str(self.cfg.get("provider") or "")
        if not provider_id:
            try:
                provider_id = (
                    await self.plugin.context.get_current_chat_provider_id(umo=umo)
                    or ""
                )
            except Exception:
                provider_id = self._first_provider()
        if not provider_id:
            self.debug("[juhkff] 主动群聊：没有可用的对话模型")
            return None

        # 模型能力里明确没写 image 就别传图，免得每回合都靠报错重试；
        # 没配置 modalities 的视为未知，仍然传图、失败再退回纯文本。
        if image_urls and self._provider_image_capability(provider_id) is False:
            # 主模型不支持图片：按 AstrBot 核心的 `_ensure_img_caption` 做法，
            # 用配置里的「图片转述模型」把图变成一句描述再当文本喂进去
            caption = await self._caption_images(umo, image_urls)
            self.debug(
                f"[juhkff] 模型 {provider_id} 不支持图片，改用图片转述：{bool(caption)}"
            )
            # 图不发了，正文里的编号也一起撤掉，避免模型找不到图
            user_message = self._build_user_message(
                sender, latest, called, 0, at_me=self._at_me(event)
            )
            if url_extra:
                user_message += "\n\n参考链接内容：\n" + url_extra
            if caption:
                user_message += f"\n\n（本条消息图片内容转述：{caption}）"
            image_urls = []
            # 历史里的图同样要撤掉，不然照样会因为不支持而报错
            contexts = self._text_only_contexts(contexts)

        try:
            response = await self.plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=user_message,  # AstrBot 的形参就叫 prompt，语义是「这一条 user 消息」
                system_prompt=system_prompt,
                contexts=contexts or None,
                image_urls=image_urls or None,
            )
        except Exception:
            if not image_urls:
                logger.exception("[juhkff] 主动群聊：调用模型失败")
                return None
            # 模型不支持图片输入时会报错，退回纯文本再试一次。
            # 正文里的 `[图片N]` 也要一起撤掉，否则模型会去找并不存在的图。
            logger.warning("[juhkff] 带图调用失败，改用纯文本重试")
            user_message = self._build_user_message(
                sender, latest, called, 0, at_me=self._at_me(event)
            )
            if url_extra:
                user_message += "\n\n参考链接内容：\n" + url_extra
            try:
                response = await self.plugin.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=user_message,  # AstrBot 的形参就叫 prompt，语义是「这一条 user 消息」
                    system_prompt=system_prompt,
                    contexts=self._text_only_contexts(contexts) or None,
                )
            except Exception:
                logger.exception("[juhkff] 主动群聊：调用模型失败")
                return None
        text = (getattr(response, "completion_text", "") or "").strip()
        return _clean_reply(text)

    # ------------------------------------------------------------ 辅助能力

    @staticmethod
    async def _is_public_url(url: str) -> bool:
        """URL 是否指向公网地址（挡住 localhost / 内网 / 保留地址）。"""
        try:
            host = urlsplit(url).hostname or ""
        except Exception:
            return False
        if not host:
            return False
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except Exception:
            return False
        for info in infos:
            addr = str(info[4][0]).split("%", 1)[0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                return False
            if not ip.is_global:
                return False
        return True

    async def _fetch_public_page(
        self, session: aiohttp.ClientSession, url: str
    ) -> str:
        """抓一个公网页面的正文；重定向逐跳检查，绝不向内网发请求。

        aiohttp 默认自动跟随 302，等拿到 `resp.url` 再检查时请求已经发出去了。
        这里关掉自动重定向、自己跟：每一跳先过 `_is_public_url` 再发请求，
        所以公网地址 302 到 127.0.0.1 时不会真的打到内网。
        """
        current = url
        for _ in range(5):
            if not await self._is_public_url(current):
                return ""
            async with session.get(current, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if not location:
                        return ""
                    current = urljoin(current, location)
                    continue
                if resp.status != 200:
                    return ""
                # 只读前 300KB：有的页面是几百 MB 的视频页/长文，
                # 全文读进内存再截 800 字很浪费
                raw = await resp.content.read(300_000)
                return raw.decode(resp.charset or "utf-8", errors="ignore")
        return ""

    async def _analyze_urls(self, text: str) -> str:
        """抓消息里前两个链接的正文，压成纯文本供模型参考。"""
        urls = _URL_RE.findall(text or "")[:2]
        if not urls:
            return ""
        chunks: list[str] = []
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in urls:
                try:
                    html = await self._fetch_public_page(session, url)
                except Exception:
                    continue
                if not html:
                    continue
                plain = _TAG_RE.sub(" ", html)
                plain = re.sub(r"\s+", " ", plain).strip()
                if plain:
                    chunks.append(f"【{url}】\n{truncate(plain, 800)}")
        return "\n\n".join(chunks)

    @staticmethod
    def _text_only_contexts(contexts: list[Message]) -> list[Message]:
        """剥掉上下文里的图片，只留文本。

        模型不支持图片、或带图调用失败时用它兜底，避免上下文里残留
        ImageURLPart 让请求继续报错。
        """
        stripped: list[Message] = []
        for msg in contexts:
            text = "".join(
                part.text
                for part in (msg.content or [])
                if isinstance(part, TextPart)
            )
            if not text:
                continue
            segment = (
                AssistantMessageSegment
                if msg.role == "assistant"
                else UserMessageSegment
            )
            stripped.append(segment(content=[TextPart(text=text)]))
        return stripped

    def _caption_config(self, umo: str) -> tuple[str, str]:
        """读 AstrBot 全局配置里的「图片转述模型」与提示词。

        对应 WebUI 配置：provider_settings.default_image_caption_provider_id
        与 provider_settings.image_caption_prompt（留空表示不用转述）。
        """
        try:
            cfg = self.plugin.context.get_config(umo=umo) or {}
        except Exception:
            return "", ""
        settings = cfg.get("provider_settings") or {}
        if not isinstance(settings, dict):
            return "", ""
        return (
            str(settings.get("default_image_caption_provider_id") or ""),
            str(
                settings.get("image_caption_prompt")
                or "Please describe the image using Chinese."
            ),
        )

    async def _caption_images(self, umo: str, image_urls: list[str]) -> str:
        """调用图片转述模型，把本回合的图压成一句文字描述。

        这是对齐 AstrBot 核心 `_ensure_img_caption()` 的回退路径：主模型
        不支持图片时，让转述模型看图并只回文本，主模型照样能"知道"图里
        有什么。转述模型没配置或调用失败就返回空串，退回纯文本。
        """
        provider_id, caption_prompt = self._caption_config(umo)
        if not provider_id or not image_urls:
            return ""
        try:
            response = await asyncio.wait_for(
                self.plugin.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=caption_prompt,
                    image_urls=list(image_urls),
                ),
                timeout=30,
            )
        except Exception:
            logger.warning("[juhkff] 图片转述失败，改按纯文本继续")
            return ""
        return (getattr(response, "completion_text", "") or "").strip()

    def _provider_image_capability(self, provider_id: str) -> bool | None:
        """读 provider 配置里的 modalities，判断它是否支持图片输入。

        返回 True/False 表示明确支持/不支持；None 表示配置里没写、无法判断。
        AstrBot 自己的 `_provider_supports_modality` 也是读同一个字段。
        """
        try:
            provider = self.plugin.context.get_provider_by_id(provider_id)
        except Exception:
            return None
        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return None
        modalities = config.get("modalities")
        if not isinstance(modalities, list) or not modalities:
            return None
        return "image" in modalities

    def _first_provider(self) -> str:
        """没有指定模型时，取 AstrBot 里第一个可用的对话 provider。"""
        try:
            providers = self.plugin.context.get_all_providers()
        except Exception:
            return ""
        for prov in providers:
            try:
                return str(prov.meta().id)
            except Exception:
                continue
        return ""


# ---------------------------------------------------------------- 工具

def _clean_reply(text: str) -> str | None:
    """模型经常带引号、换行或说太长，这里统一压成一行、去掉包裹引号。"""
    text = (text or "").strip()
    if not text:
        return None
    text = text.replace("\n", " ").strip()
    text = text.strip('"\'“”「」').strip()
    return text[:120]


def _sender(event: AstrMessageEvent) -> str:
    return str(event.get_sender_name() or event.get_sender_id() or "群友")


def _group_id(umo: str) -> str:
    """umo 形如 `aiocqhttp:GroupMessage:123456`，取第三段群号；异常格式原样返回。"""
    parts = umo.split(":")
    return parts[2] if len(parts) >= 3 else umo
