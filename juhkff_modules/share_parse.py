"""Parse Bilibili, Xiaoheihe, and generic JSON share cards with local covers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from io import BytesIO
from typing import Any
from urllib.parse import urlencode, urlsplit

from PIL import Image as PILImage

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Json, Plain

from .common import BaseModule, truncate
from .http_utils import public_request, validate_url

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_B23_HOSTS = {"b23.tv"}
_MAX_COVER_BYTES = 10 * 1024 * 1024


def _iter_json_cards(event: AstrMessageEvent) -> Iterator[dict[str, Any]]:
    """Yield dictionary cards, accepting both string and dictionary Json data.

    Args:
        event: Incoming message containing optional Json components.

    Yields:
        Parsed share cards.
    """
    for seg in event.message_obj.message or []:
        if not isinstance(seg, Json):
            continue
        data = seg.data
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                continue
        if isinstance(data, dict):
            yield data


def _walk(obj: Any) -> Iterator[tuple[str, Any]]:
    """Walk nested card data.

    Args:
        obj: Dictionary, list, or scalar from a card.

    Yields:
        Dictionary keys and their values at every nesting level.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _first_url(card: dict[str, Any]) -> str | None:
    """Find a share URL, preferring known card fields.

    Args:
        card: Parsed share card.

    Returns:
        The first matching URL, or None.
    """
    for key in ("qqdocurl", "jumpUrl", "url", "source_url"):
        for k, value in _walk(card):
            if k == key and isinstance(value, str) and value.startswith("http"):
                return value
    for _key, value in _walk(card):
        if isinstance(value, str):
            match = _URL_RE.search(value)
            if match:
                return match.group(0)
    return None


def _first_str(card: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Find a nonempty string in preferred fields.

    Args:
        card: Parsed share card.
        keys: Field names in priority order.

    Returns:
        The first matching value, or an empty string.
    """
    for key in keys:
        for k, value in _walk(card):
            if k == key and isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _detect_platform(card: dict[str, Any]) -> str:
    """Route Bilibili cards using only an exact, validated URL hostname.

    Args:
        card: Parsed share card.

    Returns:
        The Bilibili, Xiaoheihe, or generic platform key.
    """
    url = _first_url(card)
    if url:
        try:
            host = urlsplit(validate_url(url)).hostname
        except ValueError:
            host = None
        if host in _BILIBILI_HOSTS | _B23_HOSTS:
            return "bilibili"
    blob = json.dumps(card, ensure_ascii=False)
    if "小黑盒" in blob or "xiaoheihe" in blob.lower():
        return "xiaoheihe"
    return "generic"


class ShareParseModule(BaseModule):
    key = "share_parse"
    title = "卡片解析"

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        # Ignore the bot's own cards to prevent reply loops.
        if event.get_self_id() == event.get_sender_id():
            return None

        cards = list(_iter_json_cards(event))
        if not cards:
            return None

        for card in cards:
            platform = _detect_platform(card)
            if platform == "bilibili" and not self.cfg.get("parse_bilibili", True):
                continue
            if platform == "xiaoheihe" and not self.cfg.get("parse_xiaoheihe", True):
                continue
            if platform == "generic" and not self.cfg.get("parse_generic", True):
                continue

            reply = await self._build_reply(event, card, platform)
            if reply is not None:
                return reply
        return None

    def help_entries(self):
        return [("分享卡片", "自动解析 B 站 / 小黑盒分享卡片，并补上封面图")]

    async def _build_reply(
        self, event: AstrMessageEvent, card: dict[str, Any], platform: str
    ) -> MessageChain | None:
        url = _first_url(card)
        if not url:
            return None

        title = _first_str(card, ("title", "desc")) or "分享内容"
        desc = _first_str(card, ("desc", "description", "summary"))
        preview = _first_str(card, ("preview", "pic", "thumb", "cover"))

        cover: bytes | None = None
        if self.cfg.get("fetch_cover", True):
            try:
                cover = await self._resolve_cover(url, preview, platform)
            except Exception as exc:
                self.debug(f"[juhkff] Share cover failed: {exc}")

        parts: list[Any] = []
        if self.cfg.get("at_sender", False):
            try:
                parts.append(At(qq=event.get_sender_id()))
            except Exception:
                pass
        if cover:
            parts.append(Image.fromBytes(cover))
        parts.append(Plain(f"\n{truncate(title, 80)}"))
        if desc and desc != title:
            parts.append(Plain(f"\n{truncate(desc, 120)}"))
        parts.append(Plain(f"\n{url}"))
        return event.chain_result(parts)

    async def _resolve_cover(
        self, url: str, preview: str, platform: str
    ) -> bytes | None:
        """Download and decode the cover before constructing a message image.

        Args:
            url: Logical share URL.
            preview: Optional card-supplied image URL.
            platform: Selected card platform.

        Returns:
            Verified image bytes, or None when no cover is available.

        Raises:
            ValueError: The URL or decoded image is unsafe or invalid.
            RuntimeError: The public request failed or exceeded its byte limit.
        """
        cover_url = (
            await self._bilibili_cover(url) if platform == "bilibili" else preview
        )
        if not cover_url:
            return None
        payload, _final_url = await public_request(
            cover_url,
            proxy=self.proxy or None,
            timeout=float(self.cfg.get("cover_timeout", 8)),
            max_bytes=_MAX_COVER_BYTES,
        )
        with PILImage.open(BytesIO(payload)) as image:
            if image.width * image.height > 20_000_000:
                raise ValueError("Share cover has too many pixels")
            image.verify()
        # verify() checks the container; load() also rejects corrupt pixel data.
        with PILImage.open(BytesIO(payload)) as image:
            image.load()
        return payload

    async def _bilibili_cover(self, url: str) -> str | None:
        """Resolve a trusted Bilibili video URL and query its official cover API.

        Args:
            url: Bilibili video URL or b23.tv short link.

        Returns:
            The API cover URL, or None if the video cannot be resolved.
        """
        timeout = float(self.cfg.get("cover_timeout", 8))
        proxy = self.proxy or None
        try:
            real = validate_url(url, allowed_hosts=_BILIBILI_HOSTS | _B23_HOSTS)
            if urlsplit(real).hostname in _B23_HOSTS:
                # Request a tiny representation when the final server supports
                # Range. Even when ignored, public_request bounds the body.
                _body, real = await public_request(
                    real,
                    proxy=proxy,
                    timeout=timeout,
                    headers={"Range": "bytes=0-0"},
                    allowed_hosts=_BILIBILI_HOSTS | _B23_HOSTS,
                )
            if urlsplit(real).hostname not in _BILIBILI_HOSTS:
                return None
            video_id = self._extract_bvid(real)
            if not video_id:
                return None
            params = (
                {"aid": video_id[2:]}
                if video_id.startswith("av")
                else {"bvid": video_id}
            )
            api = "https://api.bilibili.com/x/web-interface/view?" + urlencode(params)
            body, _final_url = await public_request(
                api,
                proxy=proxy,
                timeout=timeout,
                max_bytes=1024 * 1024,
                allowed_hosts={"api.bilibili.com"},
            )
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("code") != 0:
                return None
            data = payload.get("data")
            pic = data.get("pic") if isinstance(data, dict) else None
            return pic if isinstance(pic, str) and pic else None
        except Exception as exc:
            self.debug(f"[juhkff] Bilibili cover lookup failed: {exc}")
            return None

    @staticmethod
    def _extract_bvid(text: str) -> str | None:
        """Extract a BV or av identifier from a Bilibili video path.

        Args:
            text: Logical video URL.

        Returns:
            A BV identifier or av-prefixed numeric identifier, or None.
        """
        match = re.search(
            r"/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/|$)", urlsplit(text).path
        )
        return match.group(1) if match else None
