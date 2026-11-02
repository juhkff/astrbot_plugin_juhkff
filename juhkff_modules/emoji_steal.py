"""偷表情：行为对齐上游 `src/apps/emojiSave.ts`。

原则和上游一致：不判断「这张图像不像表情」，而是看群友怎么用——同一张图
在 `expire_hours`（默认 72 小时）内被同一个群发过第二次，就认为它被当成表情在
用，落盘收藏。

与之前的实现相比的差异：

- 图库按群隔离（`emoji/<群号>/`），上游也是按 `group_id` 建目录；
- 用 sha256 而不是文件名判定同一张图，改名重发也能认出来；
- 单张上限从 2MB 收紧到上游的 100KB（表情一般都很小）；
- 每组最多 50 张，超出删最旧的（上游是随机删一张）；
- 每次群消息都会按 `default_emoji_rate` / 分群配置掷骰子，决定要不要随机
  发一张已收藏的表情（上游是先在 reply_rate 命中后再按 emoji_rate 抽）；
- 可选「启用表情包判断」：交给对话模型判断这张图是不是表情包，不是就不参与
  计数（已有的计数一并清掉），避免把截图、照片误收进图库。
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image

from .common import BaseModule, PluginLike, data_dir, load_json, save_json, subdir

# 「是不是表情包」的判断提示词：要求模型只回一个「是」或「否」
_JUDGE_PROMPT = "这张图片是不是表情包（贴纸 / sticker，用于聊天中表达语气或玩梗）？只回答「是」或「否」，不要加标点或解释。"

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),
)


def _guess_ext(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return "img"


class EmojiStealModule(BaseModule):
    key = "emoji_steal"
    title = "偷表情"

    def __init__(self, plugin: PluginLike) -> None:
        super().__init__(plugin)
        self._root: Path | None = None
        self._index_path: Path | None = None
        self._prints_path: Path | None = None
        # 群号 -> {sha256: {"file", "saved_at", "umo"}}，即「已收藏」图库
        self._index: dict[str, dict[str, dict[str, Any]]] = {}
        # 群号 -> {sha256: {"first": ts}}，指「第一次见到这张图」的时间，
        # 用来判断它是否在 expire_hours 窗口内又被发了一次
        self._prints: dict[str, dict[str, dict[str, float]]] = {}
        self._lock = asyncio.Lock()
        self._rng = random.SystemRandom()

    def help_entries(self):
        return [("（自动）", "同一张图被反复发送时自动收藏，并按配置概率随机发出")]

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """加载图库索引与指纹表，并清掉过期 / 文件已丢的条目。"""
        self._root = subdir("emoji")
        self._index_path = data_dir() / "emoji_index.json"
        self._prints_path = data_dir() / "fingerprints.json"
        self._index = load_json(self._index_path, {}) or {}
        self._prints = load_json(self._prints_path, {}) or {}
        await asyncio.to_thread(self._prune)
        logger.info(
            f"[juhkff] 偷表情已就绪：{len(self._index)} 个群、"
            f"共 {sum(len(v) for v in self._index.values())} 张"
        )

    async def stop(self) -> None:
        await asyncio.to_thread(self._save)

    def _save(self) -> None:
        if self._index_path is not None:
            save_json(self._index_path, self._index)
        if self._prints_path is not None:
            save_json(self._prints_path, self._prints)

    # ------------------------------------------------------------ 配置

    @property
    def _expire(self) -> float:
        """「同一张图」的认定窗口，单位秒；配置按小时填，0 表示永不过期。"""
        if "expire_hours" in self.cfg:
            return max(0.0, float(self.cfg.get("expire_hours") or 0)) * 3600
        # 兼容旧配置：expire_sec（秒），默认 3 天
        return float(self.cfg.get("expire_sec", 259200) or 259200)

    @property
    def _size_limit(self) -> int:
        """单张图大小上限（字节），默认 100KB，超过直接忽略。"""
        return int(float(self.cfg.get("size_limit_kb", 100) or 100) * 1024)

    @property
    def _max_per_group(self) -> int:
        return max(1, int(self.cfg.get("max_per_group", 50) or 50))

    @property
    def _judge_enabled(self) -> bool:
        """是否让模型判断「这张图是不是表情包」。"""
        return bool(self.cfg.get("judge_emoji", False))

    def _rates(self, group_key: str, umo: str) -> tuple[float, float]:
        """返回 (接话概率, 接话时发表情概率)。

        分群配置支持两种写法：dict `{群号: {reply_rate, emoji_rate}}`，
        或 template_list `[{groups: [...], reply_rate, emoji_rate}]`。
        """
        reply = float(self.cfg.get("default_reply_rate", 0.1) or 0)
        emoji = float(self.cfg.get("default_emoji_rate", 0.2) or 0)

        rules = self.cfg.get("group_rates") or {}
        rule = None
        if isinstance(rules, dict):
            rule = rules.get(group_key) or rules.get(umo)
        else:
            for item in rules:
                groups = [str(x) for x in (item.get("groups") or [])]
                if group_key in groups or umo in groups:
                    rule = item
                    break
        if isinstance(rule, dict):
            if rule.get("reply_rate") is not None:
                reply = float(rule["reply_rate"])
            if rule.get("emoji_rate") is not None:
                emoji = float(rule["emoji_rate"])
        return reply, emoji

    # ------------------------------------------------------------ 消息

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        umo = event.unified_msg_origin or ""
        # umo 不是 `平台:类型:群号` 格式（比如私聊）就跳过
        if self._group_key(umo) == umo:
            return None
        if not self._is_group(event):
            return None

        group_key = self._group_key(umo)
        reply_rate, emoji_rate = self._rates(group_key, umo)

        # 先做收藏（不管本回合说不说话，图都可能被收藏）
        try:
            await self._collect(event, group_key, umo)
        except Exception:
            logger.exception("[juhkff] 偷表情：收藏失败")

        # 先掷「要不要说话」，再掷「说表情还是不说话」，和上游一致
        if self._rng.random() >= reply_rate:
            return None
        files = self._gallery_files(group_key)
        if not files or self._rng.random() >= emoji_rate:
            return None
        await asyncio.sleep(self._rng.uniform(0.0, 1.5))
        path = self._rng.choice(files)
        logger.info(f"[juhkff] 发送偷到的表情：{path}")
        return MessageChain().file_image(str(path))

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        try:
            return event.get_message_type().value == "GroupMessage"
        except Exception:
            return True

    def _group_key(self, umo: str) -> str:
        parts = umo.split(":")
        return parts[2] if len(parts) >= 3 else umo

    def _gallery_dir(self, group_key: str) -> Path:
        return (self._root or data_dir() / "emoji") / group_key

    def _gallery_files(self, group_key: str) -> list[Path]:
        index = self._index.get(group_key) or {}
        files = []
        for info in index.values():
            path = self._gallery_dir(group_key) / str(info.get("file", ""))
            if path.exists():
                files.append(path)
        return files

    # ------------------------------------------------------------ 收藏

    async def _collect(self, event: AstrMessageEvent, group_key: str, umo: str) -> None:
        """核心：同一张图在窗口内第二次出现才收藏。

        流程：取图片段 → 转本地文件 → 过滤超大图 → sha256 指纹 →
        首次见到只记时间；窗口内再次见到就落盘并加入图库索引。
        """
        segments = self._image_segments(event)
        if not segments:
            return

        now = time.time()
        changed = False
        for seg in segments:
            try:
                # 注意：convert_to_file_path 是 async 方法（内部自己判断 URL/本地/Base64
                # 并下载），不能丢进 to_thread，否则拿到的是未 await 的协程对象
                local = await seg.convert_to_file_path()
            except Exception as exc:
                self.debug(f"[juhkff] 表情图片转存失败: {exc}")
                continue
            path = Path(str(local))
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0 or size > self._size_limit:
                continue
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except OSError:
                continue

            digest = hashlib.sha256(data).hexdigest()

            # 已收藏的直接跳过，不浪费一次模型调用
            async with self._lock:
                if digest in (self._index.get(group_key) or {}):
                    continue
                record = (self._prints.get(group_key) or {}).get(digest)

            # 第一次见到这张图时，可选地让模型判断是不是表情包。
            # 判断放在锁外：网络请求很慢，不能堵住别的图片处理。
            judged: bool | None = None
            if self._judge_enabled and not (record and record.get("emoji")):
                judged = await self._judge(path, umo)
                if judged is False:
                    # 不是表情包：不处理；如果之前已经计过数，把计数清掉
                    async with self._lock:
                        prints = self._prints.get(group_key) or {}
                        if prints.pop(digest, None) is not None:
                            changed = True
                            logger.info(
                                f"[juhkff] 判为表情包：否，清除 {digest[:8]} 的计数"
                            )
                    continue

            async with self._lock:
                group_index = self._index.setdefault(group_key, {})
                if digest in group_index:  # 并发下可能已被别的消息收藏
                    continue

                prints = self._prints.setdefault(group_key, {})
                record = prints.get(digest)
                if record is None or now - float(record.get("first", 0)) > self._expire:
                    entry: dict[str, Any] = {"first": now}
                    if judged is True:
                        # 记下来，第二次出现时不用再调一次模型
                        entry["emoji"] = True
                    prints[digest] = entry
                    changed = True
                    continue

                # 窗口内第二次出现 → 收藏
                directory = self._gallery_dir(group_key)
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f"{digest[:16]}.{_guess_ext(data)}"
                try:
                    await asyncio.to_thread(target.write_bytes, data)
                except OSError as exc:
                    logger.warning(f"[juhkff] 表情落盘失败: {exc}")
                    continue
                group_index[digest] = {
                    "file": target.name,
                    "saved_at": now,
                    "umo": umo,
                }
                prints.pop(digest, None)
                changed = True
                logger.info(f"[juhkff] 收藏表情 {target}（{size} 字节）")
                self._trim_group(group_key)

        if changed:
            await asyncio.to_thread(self._save)

    async def _judge(self, path: Path, umo: str) -> bool | None:
        """让模型判断一张图是不是表情包。

        返回 True（是）/ False（不是）/ None（判断不了，按未判断处理，
        继续走原本的「重复出现即收藏」逻辑，避免模型故障时功能整体失效）。
        """
        ctx = self.plugin.context
        provider_id = str(self.cfg.get("judge_provider") or "")
        if not provider_id:
            try:
                provider_id = await ctx.get_current_chat_provider_id(umo=umo)
            except Exception as exc:
                self.debug(f"[juhkff] 表情判断：取当前模型失败: {exc}")
                provider_id = ""
        if not provider_id:
            self.debug("[juhkff] 表情判断：没有可用模型，跳过判断")
            return None

        try:
            resp = await asyncio.wait_for(
                ctx.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=_JUDGE_PROMPT,
                    image_urls=[str(path)],
                ),
                timeout=20.0,
            )
        except Exception as exc:
            self.debug(f"[juhkff] 表情判断失败（按未判断处理）: {exc}")
            return None

        text = str(getattr(resp, "completion_text", "") or "").strip()
        lowered = text.lower()
        if text.startswith("是") or lowered.startswith(("yes", "true", "y")):
            return True
        if text.startswith(("否", "不", "没")) or lowered.startswith(("no", "false", "n")):
            return False
        self.debug(f"[juhkff] 表情判断返回无法识别的内容: {text[:30]}")
        return None

    @staticmethod
    def _image_segments(event: AstrMessageEvent) -> list[Image]:
        try:
            chain = event.message_obj.message or []
        except Exception:
            return []
        return [seg for seg in chain if isinstance(seg, Image)]

    def _trim_group(self, group_key: str) -> None:
        """超过每群上限时，按收藏时间从旧到新删。"""
        index = self._index.get(group_key) or {}
        if len(index) <= self._max_per_group:
            return
        ordered = sorted(index.items(), key=lambda kv: float(kv[1].get("saved_at", 0)))
        for digest, info in ordered[: len(index) - self._max_per_group]:
            path = self._gallery_dir(group_key) / str(info.get("file", ""))
            try:
                path.unlink()
            except OSError:
                pass
            index.pop(digest, None)

    # ------------------------------------------------------------ 维护

    def _prune(self) -> None:
        """启动时清理：过期指纹、文件已丢失的索引项。"""
        now = time.time()
        for group_key, prints in list(self._prints.items()):
            for digest, record in list(prints.items()):
                if now - float(record.get("first", 0)) > self._expire:
                    prints.pop(digest, None)
            if not prints:
                self._prints.pop(group_key, None)

        for group_key, index in list(self._index.items()):
            for digest, info in list(index.items()):
                path = self._gallery_dir(group_key) / str(info.get("file", ""))
                if not path.exists():
                    index.pop(digest, None)
            if not index:
                self._index.pop(group_key, None)
