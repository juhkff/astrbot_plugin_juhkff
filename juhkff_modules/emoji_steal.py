"""偷表情：行为对齐上游 `src/apps/emojiSave.ts`。

原则和上游一致：不判断「这张图像不像表情」，而是看群友怎么用——同一张图
在 `expire_sec`（默认 3 天）内被同一个群发过第二次，就认为它被当成表情在
用，落盘收藏。

与之前的实现相比的差异：

- 图库按群隔离（`emoji/<群号>/`），上游也是按 `group_id` 建目录；
- 用 sha256 而不是文件名判定同一张图，改名重发也能认出来；
- 单张上限从 2MB 收紧到上游的 100KB（表情一般都很小）；
- 每组最多 50 张，超出删最旧的（上游是随机删一张）；
- 每次群消息都会按 `default_emoji_rate` / 分群配置掷骰子，决定要不要随机
  发一张已收藏的表情（上游是先在 reply_rate 命中后再按 emoji_rate 抽）。
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
        # 用来判断它是否在 expire_sec 窗口内又被发了一次
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
        """「同一张图」的认定窗口，默认 3 天。"""
        return float(self.cfg.get("expire_sec", 259200) or 259200)

    @property
    def _size_limit(self) -> int:
        """单张图大小上限（字节），默认 100KB，超过直接忽略。"""
        return int(float(self.cfg.get("size_limit_kb", 100) or 100) * 1024)

    @property
    def _max_per_group(self) -> int:
        return max(1, int(self.cfg.get("max_per_group", 50) or 50))

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
                local = await asyncio.to_thread(seg.convert_to_file_path)
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
            async with self._lock:
                group_index = self._index.setdefault(group_key, {})
                if digest in group_index:
                    continue

                prints = self._prints.setdefault(group_key, {})
                record = prints.get(digest)
                if record is None or now - float(record.get("first", 0)) > self._expire:
                    prints[digest] = {"first": now}
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
