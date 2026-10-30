"""JSON 卡片解析：哔哩哔哩、小黑盒，以及通用兜底。

对应原插件 apps/shareAnalyse.ts。

工作方式：监听群消息，发现 Json 组件 → 判断平台 → 提取标题/简介/链接/封面 →
生成一条图文消息回复。不依赖任何第三方短链解析服务。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Json, Plain

from .common import BaseModule, PluginLike, truncate

# 卡片里的链接形如 https://b23.tv/xxxx，或普通 http(s) 链接
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _iter_json_cards(event: AstrMessageEvent) -> Iterator[dict[str, Any]]:
    """遍历消息里的 Json 卡片。

    QQ 的分享卡片在消息链里是一个 `Json` 组件，`data` 可能是 dict，
    也可能是 JSON 字符串（不同适配器行为不同），这里统一转成 dict。
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
    """深度遍历 dict/list，产出 (key, value)。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _first_url(card: dict[str, Any]) -> str | None:
    """按优先级找卡片里的链接；找不到已知字段就正则搜第一个 URL。"""
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
    for key in keys:
        for k, value in _walk(card):
            if k == key and isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _detect_platform(card: dict[str, Any]) -> str:
    """用整张卡片的 JSON 文本判断来源平台，简单粗暴但够用。"""
    blob = json.dumps(card, ensure_ascii=False)
    if "哔哩哔哩" in blob or "bilibili" in blob.lower() or "b23.tv" in blob:
        return "bilibili"
    if "小黑盒" in blob or "xiaoheihe" in blob.lower():
        return "xiaoheihe"
    return "generic"


class ShareParseModule(BaseModule):
    key = "share_parse"
    title = "卡片解析"

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        # 忽略机器人自己发出的消息（避免解析自己转发的卡片）
        if event.get_self_id() == event.get_sender_id():
            return None

        cards = list(_iter_json_cards(event))
        if not cards:
            return None

        # 一条消息可能带多张卡片，逐张判断平台开关，命中第一张能解析的就回复
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


    # ------------------------------------------------------------ 组装

    async def _build_reply(
        self, event: AstrMessageEvent, card: dict[str, Any], platform: str
    ) -> MessageChain | None:
        url = _first_url(card)
        if not url:
            return None

        title = _first_str(card, ("title", "desc")) or "分享内容"
        desc = _first_str(card, ("desc", "description", "summary"))
        # preview 字段在部分卡片里就是封面图 URL，能直接用
        preview = _first_str(card, ("preview", "pic", "thumb", "cover"))

        cover: str | None = None
        if self.cfg.get("fetch_cover", True):
            try:
                cover = await self._resolve_cover(url, preview, platform)
            except Exception as exc:
                self.debug(f"[juhkff] 封面解析失败: {exc}")

        # 组装顺序：@群友（可选）→ 封面图 → 标题 → 简介 → 原始链接
        parts: list[Any] = []
        if self.cfg.get("at_sender", False):
            try:
                parts.append(At(qq=event.get_sender_id()))
            except Exception:
                pass
        if cover:
            parts.append(Image.fromURL(cover))
        parts.append(Plain(f"\n{truncate(title, 80)}"))
        if desc and desc != title:
            parts.append(Plain(f"\n{truncate(desc, 120)}"))
        parts.append(Plain(f"\n{url}"))
        return event.chain_result(parts)

    # ------------------------------------------------------------ 封面

    async def _resolve_cover(self, url: str, preview: str, platform: str) -> str | None:
        """B 站走官方接口取高清封面；其它平台用卡片自带的 preview。"""
        if platform == "bilibili":
            return await self._bilibili_cover(url)
        if preview.startswith("http"):
            return preview
        return None

    async def _bilibili_cover(self, url: str) -> str | None:
        """b23.tv 短链 → BV 号 → 官方接口取封面。"""
        timeout = aiohttp.ClientTimeout(total=float(self.cfg.get("cover_timeout", 8)))
        proxy = self.proxy or None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            bvid = None
            if "b23.tv" in url:
                try:
                    async with session.get(
                        url, allow_redirects=True, proxy=proxy
                    ) as resp:
                        real = str(resp.url)
                        bvid = self._extract_bvid(real)
                except Exception as exc:
                    self.debug(f"[juhkff] b23 短链解析失败: {exc}")
            if not bvid:
                bvid = self._extract_bvid(url)
            if not bvid:
                return None

            api = "https://api.bilibili.com/x/web-interface/view"
            try:
                async with session.get(api, params={"bvid": bvid}, proxy=proxy) as resp:
                    payload = await resp.json(content_type=None)
            except Exception as exc:
                self.debug(f"[juhkff] bilibili 接口失败: {exc}")
                return None
            if (payload or {}).get("code") != 0:
                return None
            pic = ((payload.get("data") or {}).get("pic")) or ""
            return pic if pic.startswith("http") else None

    @staticmethod
    def _extract_bvid(text: str) -> str | None:
        """从链接里抠出 BV 号或 av 号，官方接口两种都认。"""
        match = re.search(r"(BV[0-9A-Za-z]{10})", text or "")
        if match:
            return match.group(1)
        match = re.search(r"av(\d+)", text or "")
        if match:
            return "av" + match.group(1)
        return None
