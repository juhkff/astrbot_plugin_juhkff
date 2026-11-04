"""Collect repeated group images and send them with one configured probability.

Images are identified by SHA-256 within each group. The defaults are a 72-hour
repeat window, a 2 MB image limit (1 MB = 1024**2 bytes), and 30 saved images per
group. Model judging is optional and requires an explicitly configured provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image

from .common import BaseModule, PluginLike, data_dir, load_json, save_json, subdir

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Ask for a yes/no verdict without additional explanation.
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
        # Group ID -> SHA-256 -> saved gallery metadata.
        self._index: dict[str, dict[str, dict[str, Any]]] = {}
        # Group ID -> SHA-256 -> first-seen timestamp and optional verdict.
        self._prints: dict[str, dict[str, dict[str, float]]] = {}
        self._lock = asyncio.Lock()
        self._rng = random.SystemRandom()
        # Cache scans of the configured local gallery.
        self._gallery: list[Path] | None = None
        self._gallery_at = 0.0

    def help_entries(self):
        return [("（自动）", "同一张图被反复发送时自动收藏，并按配置概率随机发出")]

    # ------------------------------------------------------------ Lifecycle

    async def start(self) -> None:
        """Load saved state and prune expired fingerprints and missing files."""
        self._root = subdir("emoji")
        self._index_path = data_dir() / "emoji_index.json"
        self._prints_path = data_dir() / "fingerprints.json"
        self._index = load_json(self._index_path, {}) or {}
        self._prints = load_json(self._prints_path, {}) or {}
        await asyncio.to_thread(self._prune)
        logger.info(
            f"[juhkff] Emoji collection ready: {len(self._index)} groups, "
            f"{sum(len(v) for v in self._index.values())} images"
        )

    async def stop(self) -> None:
        await asyncio.to_thread(self._save)

    def _save(self) -> None:
        if self._index_path is not None:
            save_json(self._index_path, self._index)
        if self._prints_path is not None:
            save_json(self._prints_path, self._prints)

    # ------------------------------------------------------------ Configuration

    @property
    def _expire(self) -> float:
        """Return the repeat window in seconds; zero means no expiration."""
        if "expire_hours" in self.cfg:
            return max(0.0, float(self.cfg.get("expire_hours") or 0)) * 3600
        # Preserve the legacy seconds setting; the default remains three days.
        return float(self.cfg.get("expire_sec", 259200) or 259200)

    @property
    def _size_limit(self) -> int:
        """Return the image size limit in bytes, defaulting to 2 MB."""
        return int(float(self.cfg.get("size_limit_mb", 2) or 2) * 1024**2)

    @property
    def _max_per_group(self) -> int:
        return max(1, int(self.cfg.get("max_per_group", 30) or 30))

    @property
    def _judge_enabled(self) -> bool:
        """Return whether judging has both permission and an explicit provider."""
        return bool(self.cfg.get("judge_emoji", True)) and bool(
            str(self.cfg.get("judge_provider") or "").strip()
        )

    def _rates(self, group_key: str, umo: str) -> float:
        """Return the emoji probability, honoring a group ID or origin override.

        Args:
            group_key: Group ID used by dictionary or template-list rules.
            umo: Full message origin accepted as an alternative rule key.

        Returns:
            The configured emoji probability for either gallery source.
        """
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
            if rule.get("emoji_rate") is not None:
                emoji = float(rule["emoji_rate"])
        return emoji

    # ------------------------------------------------------------ Messages

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        umo = event.unified_msg_origin or ""
        if not isinstance(umo, str):
            return None
        # Validate the raw identity without rewriting probability or index keys.
        group_key = self._group_key(umo)
        if (
            group_key == umo
            or not self._is_group(event)
            or self._gallery_dir(group_key) is None
        ):
            return None

        emoji_rate = self._rates(group_key, umo)

        # Collect before checking whether to send, including at probability zero.
        try:
            await self._collect(event, group_key, umo)
        except Exception:
            logger.exception("[juhkff] Emoji collection failed")

        # One probability check governs both stolen and local images.
        if self._rng.random() >= emoji_rate:
            return None
        path: Path | None = None
        source = ""
        stolen = self._gallery_files(group_key)
        if stolen:
            path, source = self._rng.choice(stolen), "collected gallery"
        if path is None:
            local = self._local_gallery()
            if not local:
                return None
            path, source = self._rng.choice(local), "local gallery"
        await asyncio.sleep(self._rng.uniform(0.0, 1.5))
        logger.info(f"[juhkff] Sending image from {source}: {path}")
        return MessageChain().file_image(str(path))

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        try:
            return event.get_message_type().value == "GroupMessage"
        except Exception:
            return False

    def _group_key(self, umo: str) -> str:
        parts = umo.split(":", 2)
        return parts[2] if len(parts) == 3 and parts[0] and parts[1] else umo

    def _gallery_dir(self, group_key: str) -> Path | None:
        """Validate a group component and its resolved storage boundary.

        Args:
            group_key: Raw group ID, also used unchanged by rules and metadata.

        Returns:
            An absolute group directory, or None for unsafe IDs or symlinks.
        """
        if (
            not isinstance(group_key, str)
            or group_key in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.@-]+", group_key)
        ):
            return None
        try:
            root = self._root or data_dir() / "emoji"
            if root.is_symlink():
                return None
            root = root.resolve()
            directory = root / group_key
            if directory.is_symlink() or directory.resolve().parent != root:
                return None
            return directory
        except (OSError, RuntimeError, ValueError):
            return None

    def _gallery_path(self, group_key: str, filename: Any) -> Path | None:
        """Validate an image filename within one group's storage boundary.

        Args:
            group_key: Raw group ID identifying the containing directory.
            filename: Persisted or generated single-component filename.

        Returns:
            A contained absolute image path, or None for unsafe paths or symlinks.
        """
        directory = self._gallery_dir(group_key)
        if (
            directory is None
            or not isinstance(filename, str)
            or filename in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.@-]+", filename)
        ):
            return None
        try:
            path = directory / filename
            if path.is_symlink() or path.resolve().parent != directory:
                return None
            return path
        except (OSError, RuntimeError, ValueError):
            return None

    def _local_gallery(self) -> list[Path]:
        """Scan the local gallery, caching results for 60 seconds.

        Returns:
            Image paths under the configured absolute path or data-relative path.
            A missing gallery directory is created automatically.
        """
        raw = str(self.cfg.get("gallery_path") or "").strip()
        if not raw:
            return []
        now = time.time()
        if self._gallery is not None and now - self._gallery_at < 60:
            return self._gallery
        base = Path(raw) if Path(raw).is_absolute() else data_dir() / raw
        files: list[Path] = []
        if base.is_dir():
            files = [
                p
                for p in base.rglob("*")
                if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
            ]
        else:
            try:
                base.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning(
                    f"[juhkff] Cannot create local gallery directory: {base}"
                )
        self._gallery = files
        self._gallery_at = now
        return files

    def _gallery_files(self, group_key: str) -> list[Path]:
        index = self._index.get(group_key) or {}
        if not isinstance(index, dict):
            return []
        files = []
        for info in index.values():
            if not isinstance(info, dict):
                continue
            path = self._gallery_path(group_key, info.get("file"))
            if path is not None and path.is_file():
                files.append(path)
        return files

    # ------------------------------------------------------------ Collection

    async def _collect(self, event: AstrMessageEvent, group_key: str, umo: str) -> None:
        """Collect an image on its second sighting within the repeat window.

        Args:
            event: Incoming group event containing image components.
            group_key: Group ID used to isolate saved images and fingerprints.
            umo: Full message origin recorded with saved images.
        """
        if self._gallery_dir(group_key) is None:
            return
        segments = self._image_segments(event)
        if not segments:
            return

        now = time.time()
        changed = False
        for seg in segments:
            try:
                # Conversion is asynchronous and handles URLs, local files, and Base64.
                local = await seg.convert_to_file_path()
            except Exception as exc:
                self.debug(f"[juhkff] Emoji image conversion failed: {exc}")
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

            # Skip saved images without another model call.
            async with self._lock:
                if digest in (self._index.get(group_key) or {}):
                    continue
                record = (self._prints.get(group_key) or {}).get(digest)

            # Reuse positive verdicts within the repeat window; judge outside the lock.
            if (
                record
                and self._expire > 0
                and now - float(record.get("first", 0)) > self._expire
            ):
                record = None
            judged: bool | None = None
            if self._judge_enabled:
                if not (record and record.get("emoji")):
                    judged = await self._judge(path, umo)
                if judged is False:
                    # Reject this sighting and clear any existing repeat count.
                    async with self._lock:
                        prints = self._prints.get(group_key) or {}
                        if prints.pop(digest, None) is not None:
                            changed = True
                            logger.info(
                                f"[juhkff] Image rejected as emoji; cleared {digest[:8]}"
                            )
                    continue

            async with self._lock:
                group_index = self._index.setdefault(group_key, {})
                # Another event may have saved this image concurrently.
                if digest in group_index:
                    continue

                prints = self._prints.setdefault(group_key, {})
                record = prints.get(digest)
                if record is None or (
                    self._expire > 0
                    and now - float(record.get("first", 0)) > self._expire
                ):
                    entry: dict[str, Any] = {"first": now}
                    if judged is True:
                        # Reuse a positive verdict on the next sighting.
                        entry["emoji"] = True
                    prints[digest] = entry
                    changed = True
                    continue

                # Save the second sighting within the repeat window.
                directory = self._gallery_dir(group_key)
                if directory is None:
                    continue
                directory.mkdir(parents=True, exist_ok=True)
                target = self._gallery_path(
                    group_key, f"{digest[:16]}.{_guess_ext(data)}"
                )
                if target is None:
                    continue
                try:
                    await asyncio.to_thread(target.write_bytes, data)
                except OSError as exc:
                    logger.warning(f"[juhkff] Saving emoji image failed: {exc}")
                    continue
                group_index[digest] = {
                    "file": target.name,
                    "saved_at": now,
                    "umo": umo,
                }
                prints.pop(digest, None)
                changed = True
                logger.info(f"[juhkff] Collected emoji {target} ({size} bytes)")
                self._trim_group(group_key)

        if changed:
            await asyncio.to_thread(self._save)

    async def _judge(self, path: Path, umo: str) -> bool | None:
        """Judge an image using only the explicitly configured provider.

        Args:
            path: Local image path to pass to the provider.
            umo: Origin of the incoming event.

        Returns:
            A boolean verdict, or None when disabled, unconfigured, or unavailable.
            Errors and timeouts preserve ordinary repeat-based collection.
        """
        if not self._judge_enabled:
            return None
        ctx = self.plugin.context
        provider_id = str(self.cfg.get("judge_provider") or "").strip()

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
            self.debug(f"[juhkff] Emoji judging failed; using repeat detection: {exc}")
            return None

        text = str(getattr(resp, "completion_text", "") or "").strip()
        verdict = unicodedata.normalize("NFKC", text).casefold().strip(" \t\r\n。.!?")
        if verdict in {"是", "yes", "true"}:
            return True
        if verdict in {"否", "no", "false"}:
            return False
        self.debug(f"[juhkff] Unrecognized emoji verdict: {text[:30]}")
        return None

    @staticmethod
    def _image_segments(event: AstrMessageEvent) -> list[Image]:
        try:
            chain = event.message_obj.message or []
        except Exception:
            return []
        return [seg for seg in chain if isinstance(seg, Image)]

    def _trim_group(self, group_key: str) -> None:
        """Remove the oldest safe gallery files above the per-group limit.

        Args:
            group_key: Raw group ID whose saved image count is being limited.
        """
        index = self._index.get(group_key) or {}
        if self._gallery_dir(group_key) is None or not isinstance(index, dict):
            self._index.pop(group_key, None)
            return
        ordered = []
        for digest, info in list(index.items()):
            if (
                not isinstance(info, dict)
                or self._gallery_path(group_key, info.get("file")) is None
            ):
                index.pop(digest, None)
                continue
            try:
                ordered.append((float(info.get("saved_at", 0)), digest, info))
            except (TypeError, ValueError):
                index.pop(digest, None)
        ordered.sort(key=lambda item: item[0])
        for _, digest, info in ordered[: max(0, len(index) - self._max_per_group)]:
            path = self._gallery_path(group_key, info.get("file"))
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
            index.pop(digest, None)

    # ------------------------------------------------------------ Maintenance

    def _prune(self) -> None:
        """Prune invalid metadata, expired fingerprints, and missing files."""
        now = time.time()
        if not isinstance(self._prints, dict):
            self._prints = {}
        if not isinstance(self._index, dict):
            self._index = {}
        for group_key, prints in list(self._prints.items()):
            if self._gallery_dir(group_key) is None or not isinstance(prints, dict):
                self._prints.pop(group_key, None)
                continue
            for digest, record in list(prints.items()):
                if not isinstance(record, dict):
                    prints.pop(digest, None)
                    continue
                try:
                    expired = (
                        self._expire > 0
                        and now - float(record.get("first", 0)) > self._expire
                    )
                except (AttributeError, TypeError, ValueError):
                    expired = True
                if expired:
                    prints.pop(digest, None)
            if not prints:
                self._prints.pop(group_key, None)

        for group_key, index in list(self._index.items()):
            if self._gallery_dir(group_key) is None or not isinstance(index, dict):
                self._index.pop(group_key, None)
                continue
            for digest, info in list(index.items()):
                path = self._gallery_path(
                    group_key, info.get("file") if isinstance(info, dict) else None
                )
                if path is None or not path.is_file():
                    index.pop(digest, None)
            if not index:
                self._index.pop(group_key, None)
