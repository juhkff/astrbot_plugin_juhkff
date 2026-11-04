"""Deliver subscribed artists' new illustrations and every page as merged forwards.

Subscriptions live only in AstrBotConfig. Version 2 checkpoints contain the
process owner, artist baselines, per-group watermarks, due times and failure counts.
Hot reloads preserve progress; a new process establishes fresh baselines without
sending downtime backlog. An absent checkpoint needs a baseline, while -1 denotes
an empty profile. Artist progress advances only after all current groups receive
all new works, and successful groups never receive the same work again on retry.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import math
import os
import re
import time
from pathlib import Path
from typing import TypedDict

import psutil
from aiohttp import ClientConnectionError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.platform import MessageType

from .common import (
    BaseModule,
    PluginLike,
    data_dir,
    fetch_bytes,
    fetch_json,
    load_json,
    resolve_umo,
    save_json,
)
from .http_utils import validate_url

PROFILE_URL = "https://www.pixiv.net/ajax/user/{user_id}/profile/all?lang=en"
DETAIL_URL = "https://www.pixiv.net/ajax/illust/{illust_id}"
PAGES_URL = "https://www.pixiv.net/ajax/illust/{illust_id}/pages?lang=en"
MIRROR = "https://i.pixiv.re"
_API_HOSTS = {"www.pixiv.net"}
_IMAGE_HOSTS = {"i.pximg.net", "i.pixiv.re"}
_MAX_IMAGE_BYTES = 64 * 1024 * 1024

_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.pixiv.net/",
}


class ArtistState(TypedDict):
    """Store a baseline and successful deliveries awaiting author confirmation."""

    last_id: str
    delivered: dict[str, str]


class ImagePage(TypedDict):
    """Store the real source URLs of one illustration page."""

    regular: str
    original: str


class Illustration(ImagePage):
    """Describe validated illustration metadata and its first-page source URLs."""

    id: str
    title: str
    author: str
    page_count: int


def _positive_id(value: object) -> str | None:
    """Normalize a positive ASCII numeric ID.

    Args:
        value: An API, configuration, or command ID.

    Returns:
        The canonical ID, or None for invalid input.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text):
        return None
    return text.lstrip("0") or None


class PixivPushModule(BaseModule):
    key = "pixiv_push"
    title = "Pixiv 订阅"

    def __init__(self, plugin: PluginLike) -> None:
        super().__init__(plugin)
        self._state_path: Path | None = None
        self._artists: dict[str, ArtistState] = {}
        self._due: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._owner = f"{os.getpid()}:{psutil.Process().create_time()}"
        self._bot_ids: dict[str, str] = {}
        self._running = asyncio.Lock()
        self._stopped = False
        self._baseline_task: asyncio.Task[None] | None = None
        self._checks: set[asyncio.Task[None]] = set()

    def help_entries(self) -> list[tuple[str, str]]:
        return [
            ("#pixiv订阅 UID", "在本群订阅画师的新插画"),
            ("#pixiv取消订阅 UID", "取消本群对画师的订阅"),
            ("（自动）", "补齐新插画及全部分页，合并转发；进程重启后重新建立基线"),
        ]

    def _subscriptions(self) -> dict[str, list[str]]:
        """Read a fresh normalized subscription snapshot from configuration.

        Returns:
            Artist IDs mapped to distinct, fully qualified group origins.
        """
        result: dict[str, list[str]] = {}
        rows = self.cfg.get("subscriptions", [])
        if not isinstance(rows, list):
            return result
        for row in rows:
            if not isinstance(row, dict):
                continue
            user_id = _positive_id(row.get("user_id"))
            groups = row.get("groups")
            if user_id is None or not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, str):
                    continue
                group = group.strip()
                parts = group.split(":", 2)
                if (
                    len(parts) == 3
                    and parts[0]
                    and parts[1] == "GroupMessage"
                    and parts[2]
                    and group not in result.get(user_id, [])
                ):
                    result.setdefault(user_id, []).append(group)
        return result

    def _groups(self, user_id: str) -> list[str]:
        """Recheck enablement and membership after asynchronous work.

        Args:
            user_id: Artist whose current subscribers are needed.

        Returns:
            Current group origins, or an empty list when sending must stop.
        """
        if self._stopped or not self.enabled:
            return []
        return self._subscriptions().get(user_id, [])

    def _persist_state(self) -> None:
        """Atomically save checkpoints without yielding during a mutation."""
        if self._state_path is None:
            self._state_path = data_dir() / "pixiv_subscriptions.json"
        save_json(
            self._state_path,
            {
                "version": 2,
                "owner": self._owner,
                "artists": self._artists,
                "due": self._due,
                "failures": self._failures,
            },
        )

    async def start(self) -> None:
        """Restore this process's progress or establish fresh restart baselines."""
        self._state_path = data_dir() / "pixiv_subscriptions.json"
        raw = load_json(self._state_path, {})
        self._artists = {}
        self._due.clear()
        self._failures.clear()
        if (
            isinstance(raw, dict)
            and raw.get("version") == 2
            and raw.get("owner") == self._owner
        ):
            artists = raw.get("artists")
            if isinstance(artists, dict):
                for uid, row in artists.items():
                    user_id = _positive_id(uid)
                    if user_id is None or not isinstance(row, dict):
                        continue
                    last_id = row.get("last_id")
                    last_id = "-1" if last_id == "-1" else _positive_id(last_id)
                    delivered = row.get("delivered", {})
                    if last_id is None or not isinstance(delivered, dict):
                        continue
                    self._artists[user_id] = {
                        "last_id": last_id,
                        "delivered": {
                            group: parsed
                            for group, value in delivered.items()
                            if isinstance(group, str)
                            and (parsed := _positive_id(value)) is not None
                        },
                    }
            due = raw.get("due", {})
            failures = raw.get("failures", {})
            if isinstance(due, dict):
                self._due = {
                    uid: float(value)
                    for uid, value in due.items()
                    if _positive_id(uid) is not None
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value >= 0
                }
            if isinstance(failures, dict):
                self._failures = {
                    uid: value
                    for uid, value in failures.items()
                    if _positive_id(uid) is not None
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                }
        # Persist the new owner before any network await, including empty configs.
        self._persist_state()
        self._stopped = False
        self._baseline_task = asyncio.create_task(
            self.check(), name="juhkff-pixiv-baselines"
        )
        await self.scheduler.every_minutes(
            1, self.check, "pixiv-check", tz_name=self.tz_name
        )
        logger.info("[juhkff] Pixiv artist subscriptions ready")

    async def stop(self) -> None:
        """Cancel and await startup work and any in-flight scheduled checks."""
        self._stopped = True
        tasks = set(self._checks)
        if self._baseline_task is not None:
            tasks.add(self._baseline_task)
        current = asyncio.current_task()
        if current is not None:
            tasks.discard(current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._checks.clear()
        self._baseline_task = None

    def intercepts(self, event: AstrMessageEvent) -> bool:
        """Reserve valid and malformed subscription commands before passive handlers.

        Args:
            event: Incoming message, possibly with its wake prefix removed.

        Returns:
            Whether this module owns the command, including missing arguments.
        """
        text = (event.message_str or "").strip()
        if text.startswith(("#", "/")):
            text = text[1:]
        elif not event.is_at_or_wake_command:
            return False
        return text.startswith(("pixiv订阅", "pixiv取消订阅"))

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        """Persist the current group's subscription changes before replying.

        Args:
            event: Subscription or cancellation command.

        Returns:
            A concise command response, or None for unrelated messages.
        """
        if not self.intercepts(event):
            return None
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return event.plain_result("Pixiv 订阅仅限群聊使用。")
        text = (event.message_str or "").strip()
        if text.startswith(("#", "/")):
            text = text[1:]
        match = re.fullmatch(r"pixiv(订阅|取消订阅)\s+([0-9]+)", text)
        user_id = _positive_id(match.group(2)) if match else None
        if match is None or user_id is None:
            return event.plain_result(
                "用法：#pixiv订阅 UID 或 #pixiv取消订阅 UID（UID 为正整数）"
            )
        if self._stopped or not self.enabled:
            return event.plain_result("Pixiv 订阅已停用。")
        config = self.plugin.config
        if not isinstance(config, AstrBotConfig):
            logger.error(
                "[juhkff] Pixiv subscriptions require AstrBotConfig persistence"
            )
            return event.plain_result("订阅保存失败，请检查配置。")

        # No awaits occur between reading, changing, and saving configuration.
        # Cancellation remains responsive while a polling task awaits the network.
        subscriptions = self._subscriptions()
        group = event.unified_msg_origin
        groups = subscriptions.get(user_id, [])
        subscribe = match.group(1) == "订阅"
        if subscribe:
            if group in groups:
                return event.plain_result("本群已订阅该画师。")
            subscriptions[user_id] = [*groups, group]
        else:
            if group not in groups:
                return event.plain_result("本群未订阅该画师。")
            groups.remove(group)
            if not groups:
                subscriptions.pop(user_id)
        previous = config.get(self.key)
        config[self.key] = {
            **self.cfg,
            "subscriptions": [
                {"__template_key": "artist", "user_id": uid, "groups": targets}
                for uid, targets in subscriptions.items()
            ],
        }
        try:
            config.save_config()
        except Exception:
            config[self.key] = previous
            logger.exception("[juhkff] Failed to save Pixiv subscriptions")
            return event.plain_result("订阅保存失败，请稍后重试。")

        if not subscribe:
            state = self._artists.get(user_id)
            if not groups:
                self._artists.pop(user_id, None)
                self._due.pop(user_id, None)
                self._failures.pop(user_id, None)
            elif state is not None:
                state["delivered"].pop(group, None)
            try:
                self._persist_state()
            except Exception:
                logger.exception(
                    "[juhkff] Failed to save Pixiv cancellation checkpoint"
                )
            return event.plain_result(f"已取消本群对画师 {user_id} 的订阅。")

        if user_id not in self._artists:
            self._due.pop(user_id, None)
            if self._baseline_task is None or self._baseline_task.done():
                self._baseline_task = asyncio.create_task(
                    self.check(), name="juhkff-pixiv-baselines"
                )
        return event.plain_result(f"已订阅画师 {user_id}，首次不补推旧作。")

    async def check(self) -> None:
        """Poll due artists without overlap and apply independent linear backoff."""
        task = asyncio.current_task()
        if task is not None:
            self._checks.add(task)
        try:
            if self._stopped or not self.enabled or self._running.locked():
                return
            async with self._running:
                subscriptions = self._subscriptions()
                stale = (
                    set(self._artists) | set(self._due) | set(self._failures)
                ) - subscriptions.keys()
                for user_id in stale:
                    self._artists.pop(user_id, None)
                    self._due.pop(user_id, None)
                    self._failures.pop(user_id, None)
                if stale:
                    self._persist_state()
                try:
                    interval = float(self.cfg.get("check_minutes", 60))
                    if not math.isfinite(interval):
                        raise ValueError("Non-finite Pixiv polling interval")
                    interval = max(1.0, interval) * 60
                except (ValueError, TypeError, OverflowError):
                    interval = 3600.0
                try:
                    retry_base = float(self.cfg.get("retry_interval_minutes", 2))
                    if not math.isfinite(retry_base):
                        raise ValueError("Non-finite Pixiv retry interval")
                    retry_base = max(1.0, retry_base) * 60
                except (ValueError, TypeError, OverflowError):
                    retry_base = 120.0
                processed: set[str] = set()
                while not self._stopped and self.enabled:
                    # Commands added during an await can establish their baseline
                    # in this round instead of waiting for the next minute tick.
                    pending = self._subscriptions().keys() - processed
                    if not pending:
                        break
                    user_id = next(
                        uid for uid in self._subscriptions() if uid in pending
                    )
                    processed.add(user_id)
                    started = time.monotonic()
                    if started < self._due.get(user_id, 0):
                        continue
                    started_wall = time.time()
                    success = False
                    try:
                        success = await self._check_artist(user_id)
                    except Exception:
                        logger.exception(
                            "[juhkff] Pixiv artist check failed: %s", user_id
                        )
                    if not self._groups(user_id):
                        continue
                    if success:
                        self._failures.pop(user_id, None)
                        # Align to the starting minute so request duration and
                        # cron jitter cannot turn a one-minute interval into two.
                        self._due[user_id] = started + interval - started_wall % 60
                    else:
                        failures = self._failures.get(user_id, 0) + 1
                        self._failures[user_id] = failures
                        delay = retry_base * failures
                        self._due[user_id] = time.monotonic() + delay
                        logger.warning(
                            "[juhkff] Pixiv artist %s failed %s times; retry in %s minutes",
                            user_id,
                            failures,
                            delay / 60,
                        )
                    self._persist_state()
        finally:
            if task is not None:
                self._checks.discard(task)

    async def _check_artist(self, user_id: str) -> bool:
        """Establish a baseline or deliver all newer works and all of their pages.

        Args:
            user_id: Artist to query.

        Returns:
            Whether the artist can wait for the normal polling interval.
        """
        ids = await self._fetch_ids(user_id)
        if not self._groups(user_id):
            return True
        latest = ids[-1] if ids else "-1"
        state = self._artists.get(user_id)
        if state is None:
            self._artists[user_id] = {"last_id": latest, "delivered": {}}
            try:
                self._persist_state()
            except Exception:
                self._artists.pop(user_id)
                raise
            return True
        last_id = state["last_id"]
        new_ids = [illust_id for illust_id in ids if _after(illust_id, last_id)]
        if not new_ids:
            return True

        works: list[tuple[Illustration, list[Path]]] = []
        for illust_id in new_ids:
            item = await self._fetch_detail(illust_id)
            if not self._groups(user_id) or self._artists.get(user_id) is not state:
                return True
            pages = await self._fetch_pages(item)
            if not self._groups(user_id) or self._artists.get(user_id) is not state:
                return True
            images: list[Path] = []
            for index, page in enumerate(pages):
                image = await self._download(item, page, index, user_id)
                if not self._groups(user_id) or self._artists.get(user_id) is not state:
                    return True
                if image is None:
                    return False
                images.append(image)
            works.append((item, images))

        for group in self._groups(user_id):
            if not self._groups(user_id) or self._artists.get(user_id) is not state:
                return True
            if group not in self._groups(user_id):
                continue
            watermark = state["delivered"].get(group, last_id)
            if not _after(latest, watermark):
                continue
            pending = [
                (item, images)
                for item, images in works
                if _after(item["id"], watermark)
            ]
            try:
                umo = resolve_umo(self.plugin.context, group)
                bot_id = await self._get_bot_id(umo)
                if (
                    group not in self._groups(user_id)
                    or self._artists.get(user_id) is not state
                ):
                    continue
                sent = await self.plugin.context.send_message(
                    umo, self._compose(pending, bot_id)
                )
                if sent is False:
                    raise RuntimeError("Pixiv delivery found no matching platform")
                if self._artists.get(
                    user_id
                ) is state and group in self._subscriptions().get(user_id, []):
                    state["delivered"][group] = latest
                    self._persist_state()
            except Exception:
                logger.exception(
                    "[juhkff] Pixiv merged-forward delivery failed: %s", group
                )
        groups = self._groups(user_id)
        if not groups or self._artists.get(user_id) is not state:
            return True
        if not all(
            not _after(latest, state["delivered"].get(group, last_id))
            for group in groups
        ):
            return False
        delivered = state["delivered"]
        state["last_id"] = latest
        state["delivered"] = {}
        try:
            self._persist_state()
        except Exception:
            state["last_id"] = last_id
            state["delivered"] = delivered
            raise
        return True

    async def _fetch_ids(self, user_id: str) -> list[str]:
        """Read all illustration IDs in numeric order, excluding manga.

        Args:
            user_id: Artist's positive numeric ID.

        Returns:
            All illustration IDs from oldest to newest, or an empty list.

        Raises:
            ValueError: The API returned an error or malformed illustration data.
        """
        data = await fetch_json(
            PROFILE_URL.format(user_id=user_id),
            headers=_BASE_HEADERS,
            proxy=self.proxy or None,
            timeout=25.0,
            allowed_hosts=_API_HOSTS,
        )
        if not isinstance(data, dict) or data.get("error") is not False:
            raise ValueError("Pixiv profile API returned an error or invalid response")
        body = data.get("body")
        if not isinstance(body, dict):
            raise ValueError("Pixiv profile API returned an invalid body")
        illusts = body.get("illusts")
        if isinstance(illusts, list) and not illusts:
            return []
        if not isinstance(illusts, dict):
            raise ValueError("Pixiv profile API omitted the illustration collection")
        ids = [_positive_id(value) for value in illusts]
        if any(value is None for value in ids):
            raise ValueError("Pixiv profile API returned a nonnumeric illustration ID")
        return sorted(
            {value for value in ids if value is not None},
            key=lambda value: (len(value), value),
        )

    async def _fetch_detail(self, illust_id: str) -> Illustration:
        """Validate first-page URLs, author, title, and the total page count.

        Args:
            illust_id: Newest illustration ID from the artist's profile.

        Returns:
            The validated illustration metadata.

        Raises:
            ValueError: Pixiv returned an error or incomplete illustration details.
        """
        data = await fetch_json(
            DETAIL_URL.format(illust_id=illust_id),
            headers=_BASE_HEADERS,
            proxy=self.proxy or None,
            timeout=25.0,
            allowed_hosts=_API_HOSTS,
        )
        if not isinstance(data, dict) or data.get("error") is not False:
            raise ValueError("Pixiv illustration API returned an error")
        body = data.get("body")
        if not isinstance(body, dict):
            raise ValueError("Pixiv illustration API returned an invalid body")
        ids = [body[key] for key in ("id", "illustId") if key in body]
        if not ids or any(_positive_id(value) != illust_id for value in ids):
            raise ValueError("Pixiv illustration API returned an invalid ID")
        urls = body.get("urls")
        page_count = body.get("pageCount")
        title, author = body.get("title"), body.get("userName")
        if (
            not isinstance(urls, dict)
            or not isinstance(title, str)
            or not isinstance(author, str)
            or isinstance(page_count, bool)
            or not isinstance(page_count, int)
            or page_count < 1
        ):
            raise ValueError("Pixiv illustration API returned incomplete metadata")
        regular, original = urls.get("regular", ""), urls.get("original", "")
        regular = regular if isinstance(regular, str) else ""
        original = original if isinstance(original, str) else ""
        regular = validate_url(regular, allowed_hosts=_IMAGE_HOSTS) if regular else ""
        original = (
            validate_url(original, allowed_hosts=_IMAGE_HOSTS) if original else ""
        )
        if not regular and not original:
            raise ValueError("Pixiv illustration API returned no image URL")
        return {
            "id": illust_id,
            "title": title,
            "author": author,
            "page_count": page_count,
            "regular": regular,
            "original": original,
        }

    async def _fetch_pages(self, item: Illustration) -> list[ImagePage]:
        """Fetch each page's real URLs without deriving filenames from page zero.

        Args:
            item: Validated illustration metadata.

        Returns:
            Source URLs for every page in the API's page order.

        Raises:
            ValueError: The page collection is incomplete or malformed.
        """
        if item["page_count"] == 1:
            return [{"original": item["original"], "regular": item["regular"]}]
        data = await fetch_json(
            PAGES_URL.format(illust_id=item["id"]),
            headers=_BASE_HEADERS,
            proxy=self.proxy or None,
            timeout=25.0,
            allowed_hosts=_API_HOSTS,
        )
        if not isinstance(data, dict) or data.get("error") is not False:
            raise ValueError("Pixiv pages API returned an error")
        body = data.get("body")
        if not isinstance(body, list) or len(body) != item["page_count"]:
            raise ValueError("Pixiv pages API returned an incomplete page collection")
        pages: list[ImagePage] = []
        for row in body:
            urls = row.get("urls") if isinstance(row, dict) else None
            if not isinstance(urls, dict):
                raise ValueError("Pixiv pages API omitted page URLs")
            original, regular = urls.get("original", ""), urls.get("regular", "")
            original = original if isinstance(original, str) else ""
            regular = regular if isinstance(regular, str) else ""
            original = (
                validate_url(original, allowed_hosts=_IMAGE_HOSTS) if original else ""
            )
            regular = (
                validate_url(regular, allowed_hosts=_IMAGE_HOSTS) if regular else ""
            )
            if not original and not regular:
                raise ValueError("Pixiv pages API returned no valid image URL")
            pages.append({"original": original, "regular": regular})
        return pages

    async def _download(
        self, item: Illustration, page: ImagePage, index: int, user_id: str
    ) -> Path | None:
        """Cache a page's original bytes and optionally derive a compressed copy.

        Args:
            item: Validated illustration metadata.
            page: Source URLs returned for this page.
            index: Zero-based page index.
            user_id: Artist whose subscription must remain active.

        Returns:
            A verified original or explicitly compressed image, or None on failure.
        """
        try:
            limit_mb = float(self.cfg.get("max_image_mb", 0))
            if not math.isfinite(limit_mb) or limit_mb < 0:
                raise ValueError("Invalid Pixiv compression threshold")
            limit = int(limit_mb * 1024 * 1024)
            folder = data_dir() / "pixiv" / "images" / f"{item['id']}_p{index}"
            folder.mkdir(parents=True, exist_ok=True)
            # Source bytes stay separate from regular previews, old flat caches,
            # and size-dependent derivatives, including after configuration changes.
            for kind in ("original", "regular"):
                url = page[kind]
                candidates: list[str | Path] = [
                    path
                    for path in folder.glob(f"{kind}.*")
                    if path.is_file() and path.suffix != ".tmp"
                ]
                candidates.extend(
                    dict.fromkeys(
                        candidate
                        for candidate in (url, _mirror(url))
                        if candidate.startswith(("https://", "http://"))
                    )
                )
                for candidate in candidates:
                    if not self._groups(user_id):
                        return None
                    try:
                        if (
                            isinstance(candidate, Path)
                            and candidate.stat().st_size > _MAX_IMAGE_BYTES
                        ):
                            raise ValueError(
                                "Cached Pixiv image exceeds the download limit"
                            )
                        payload = (
                            candidate.read_bytes()
                            if isinstance(candidate, Path)
                            else await fetch_bytes(
                                candidate,
                                headers=_BASE_HEADERS,
                                proxy=self.proxy or None,
                                timeout=40.0,
                                max_bytes=_MAX_IMAGE_BYTES,
                                allowed_hosts=_IMAGE_HOSTS,
                            )
                        )
                        if not self._groups(user_id):
                            return None
                        raw = await asyncio.to_thread(_prepare_image, payload, 0)
                        if not self._groups(user_id):
                            return None
                        if raw is None:
                            continue
                        raw_bytes, suffix = raw
                        target = folder / f"{kind}{suffix}"
                        if not isinstance(candidate, Path):
                            temporary = target.with_suffix(suffix + ".tmp")
                            temporary.write_bytes(raw_bytes)
                            temporary.replace(target)
                        if limit == 0 or len(raw_bytes) <= limit:
                            return target
                        prepared = await asyncio.to_thread(
                            _prepare_image, raw_bytes, limit
                        )
                        if not self._groups(user_id):
                            return None
                        if prepared is None:
                            continue
                        compressed, suffix = prepared
                        target = folder / f"{kind}_{limit}{suffix}"
                        temporary = target.with_suffix(suffix + ".tmp")
                        temporary.write_bytes(compressed)
                        temporary.replace(target)
                        return target
                    except Exception as exc:
                        logger.warning(
                            "[juhkff] Pixiv page attempt failed for %s p%s: %s",
                            item["id"],
                            index,
                            exc,
                        )
                        if isinstance(
                            exc, (ClientConnectionError, ConnectionResetError)
                        ) or str(exc).startswith("HTTP 429 "):
                            return None
            logger.warning(
                "[juhkff] No valid Pixiv page downloaded: %s p%s", item["id"], index
            )
        except (OSError, ValueError, TypeError, OverflowError):
            logger.exception(
                "[juhkff] Failed to cache Pixiv page: %s p%s", item["id"], index
            )
        return None

    async def _get_bot_id(self, umo: str) -> str:
        """Resolve and cache the sender's account ID through the platform client.

        Args:
            umo: Fully qualified group origin.

        Returns:
            The platform client's positive numeric account ID.

        Raises:
            RuntimeError: The platform or login information is unavailable.
        """
        platform_id = umo.split(":", 1)[0]
        if platform_id in self._bot_ids:
            return self._bot_ids[platform_id]
        platform = self.plugin.context.get_platform_inst(platform_id)
        if platform is None:
            raise RuntimeError("Pixiv forward platform is unavailable")
        client = platform.get_client()
        # CQHttp resolves unknown attributes into API calls: client.self_id is
        # not an account field. Use the read-only OneBot login-info action.
        action = getattr(client, "call_action", None)
        if not callable(action):
            raise RuntimeError("Pixiv merged forwarding requires a OneBot client")
        pending = action("get_login_info")
        if not inspect.isawaitable(pending):
            raise RuntimeError("Pixiv forward login action must be asynchronous")
        info = await pending
        bot_id = _positive_id(info.get("user_id")) if isinstance(info, dict) else None
        if bot_id is None:
            raise RuntimeError("Pixiv forward sender ID is unavailable")
        self._bot_ids[platform_id] = bot_id
        return bot_id

    def _compose(
        self, works: list[tuple[Illustration, list[Path]]], bot_id: str
    ) -> MessageChain:
        """Compose one native merged forward containing every work and page.

        Args:
            works: Illustrations in ID order with their page images in page order.
            bot_id: Real account ID used as the forward-node sender.

        Returns:
            One Nodes component, with one captioned image node per page.

        Raises:
            ValueError: No complete illustration is available to send.
        """
        nodes: list[Node] = []
        for item, images in works:
            if len(images) != item["page_count"]:
                raise ValueError("Pixiv forward omitted an illustration page")
            for index, image in enumerate(images):
                caption = (
                    f"{item['author']}\n{item['title']}\n"
                    f"https://www.pixiv.net/artworks/{item['id']}\n"
                    f"第 {index + 1}/{item['page_count']} 张"
                )
                nodes.append(
                    Node(
                        name="Pixiv 订阅",
                        uin=bot_id,
                        content=[Plain(caption), Image.fromFileSystem(str(image))],
                    )
                )
        if not nodes:
            raise ValueError("Pixiv forward has no pages")
        return MessageChain([Nodes(nodes)])


def _after(illust_id: str, checkpoint: str) -> bool:
    """Compare canonical numeric IDs without converting arbitrarily large strings."""
    return checkpoint == "-1" or (len(illust_id), illust_id) > (
        len(checkpoint),
        checkpoint,
    )


def _mirror(url: str) -> str:
    """Replace the official image host with the existing fallback mirror.

    Args:
        url: Official Pixiv image URL.

    Returns:
        The mirror URL, or an empty string for another host.
    """
    match = re.match(r"https?://i\.pximg\.net/(.*)$", url)
    return f"{MIRROR}/{match.group(1)}" if match else ""


def _prepare_image(payload: bytes, limit: int) -> tuple[bytes, str] | None:
    """Validate original bytes, compressing only for an explicit positive limit.

    Args:
        payload: Downloaded or cached image bytes.
        limit: Maximum encoded size in bytes, or zero to preserve original bytes.

    Returns:
        Validated bytes and their file suffix, or None when validation/sizing fails.
    """
    if not payload or limit < 0:
        return None
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(payload)) as source:
            source.load()
            suffix = {
                "JPEG": ".jpg",
                "PNG": ".png",
                "WEBP": ".webp",
                "GIF": ".gif",
                "AVIF": ".avif",
                "BMP": ".bmp",
                "TIFF": ".tiff",
            }.get(source.format or "")
            if limit == 0:
                return (payload, suffix) if suffix is not None else None
            if suffix is not None and len(payload) <= limit:
                return payload, suffix
            image = source.convert("RGB")
        while True:
            for quality in (88, 78, 68, 58, 48, 38):
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                if buffer.tell() <= limit:
                    return buffer.getvalue(), ".jpg"
            width, height = image.size
            if width == height == 1:
                return None
            image = image.resize(
                (max(1, width * 3 // 4), max(1, height * 3 // 4)),
                PILImage.Resampling.LANCZOS,
            )
    except Exception:
        return None
