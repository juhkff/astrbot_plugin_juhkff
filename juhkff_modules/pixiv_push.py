"""Pixiv 订阅推送。

对应原插件 apps 下的 Pixiv 订阅模块。

流程：
1. 每 `check_minutes` 分钟拉一次 Pixiv 日榜（公开接口，无需登录）；
2. 拿榜单里的 illust id 和 `seen.json` 比对，只处理没推过的；
3. 逐张请求详情接口，下载原图（必须带 Referer，否则 403；失败则换
   i.pixiv.re 镜像重试）；
4. 超过 `max_image_mb` 的图用 Pillow 压到合适大小；压缩失败就只发链接；
5. 按 `targets` 依次推送，文字信息可选让模型翻译。

注意：Pixiv 对游客有频率限制，`check_minutes` 不建议低于 30，单次推送
上限由 `max_push_per_run` 控制，不要配太大。
"""

from __future__ import annotations

import asyncio
import io
import random
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

from .common import (
    BaseModule,
    PluginLike,
    data_dir,
    fetch_bytes,
    fetch_json,
    load_json,
    save_json,
    subdir,
    truncate,
)

RANKING_URL = "https://www.pixiv.net/ranking.php"
DETAIL_URL = "https://www.pixiv.net/ajax/illust/{illust_id}"
PAGE_URL = "https://www.pixiv.net/artworks/{illust_id}"
MIRROR = "https://i.pixiv.re"

_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.pixiv.net/",
}


class PixivPushModule(BaseModule):
    key = "pixiv_push"
    title = "Pixiv 订阅"

    def __init__(self, plugin: PluginLike) -> None:
        super().__init__(plugin)
        self._seen_path: Path | None = None
        self._seen: set[str] = set()
        self._latest: list[dict[str, Any]] = []
        self._running = asyncio.Lock()

    def help_entries(self):
        return [("/pixiv", "立即抓取一次 Pixiv 榜单新图并推送")]


    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """加载已推送记录，注册定时检查（最短 5 分钟，防封）。"""
        self._seen_path = data_dir() / "pixiv_seen.json"
        raw = load_json(self._seen_path, []) or []
        self._seen = {str(x) for x in raw if x}
        subdir("pixiv")
        minutes = max(5.0, float(self.cfg.get("check_minutes", 60)))
        await self.scheduler.every_minutes(minutes, self.check, "pixiv-check")
        logger.info(
            f"[juhkff] Pixiv 订阅已就绪：每 {minutes:g} 分钟检查一次，"
            f"已推送 {len(self._seen)} 张"
        )

    # ------------------------------------------------------------ 消息

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        """`/pixiv`：手动抓一次并立刻推最新一张。"""
        text = event.message_str or ""
        if text.strip() not in ("/pixiv", "/pixiv 最新"):
            return None
        if not self._latest:
            await self.check()
        if not self._latest:
            return event.plain_result("暂时没抓到新的 Pixiv 作品，稍后再试。")
        item = self._latest[0]
        chain = await self._compose(item)
        return event.chain_result(list(chain.chain))

    # ------------------------------------------------------------ 主流程

    async def check(self) -> None:
        """一轮抓取：榜单 → 过滤已推 → 取详情 → 下图 → 推送。"""
        if self._running.locked():  # 上一轮还没跑完就跳过，避免叠请求
            return
        async with self._running:
            try:
                candidates = await self._fetch_ranking()
            except Exception:
                logger.exception("[juhkff] Pixiv 榜单拉取失败")
                return
            fresh = [item for item in candidates if str(item["id"]) not in self._seen]
            # 每次最多推 N 张（随机挑），避免一次刷屏
            limit = max(1, int(self.cfg.get("max_push_per_run", 2)))
            if len(fresh) > limit:
                fresh = random.sample(fresh, limit)
            if not fresh:
                self.debug("[juhkff] Pixiv：这轮没有新作品")
                return

            pushed: list[dict[str, Any]] = []
            for item in fresh:
                detail = await self._fetch_detail(str(item["id"]))
                if not detail:
                    continue
                payload = {**item, **detail}
                payload["image"] = await self._download(payload)
                self._seen.add(str(item["id"]))
                pushed.append(payload)
                # 间隔 2 秒，降低 Pixiv 游客频率限制触发概率
                await asyncio.sleep(2.0)
                if len(pushed) >= limit:
                    break

            if pushed:
                self._latest = pushed
                await self._persist_seen()
                await self._broadcast(pushed)

    async def _fetch_ranking(self) -> list[dict[str, Any]]:
        """公开日榜接口，游客可用；兼容 illust_id / id 两种字段。"""
        mode = str(self.cfg.get("ranking_mode", "day"))
        data = await fetch_json(
            RANKING_URL,
            params={"mode": mode, "content": "all", "format": "json", "p": 1},
            headers=_BASE_HEADERS,
            proxy=self.proxy or None,
            timeout=25.0,
        )
        contents = (data or {}).get("contents") or []
        result = []
        for row in contents:
            illust_id = row.get("illust_id") or row.get("id")
            if not illust_id:
                continue
            result.append(
                {
                    "id": str(illust_id),
                    "title": str(row.get("title") or ""),
                    "author": str(row.get("user_name") or ""),
                    "page": str(row.get("url") or ""),
                }
            )
        return result

    async def _fetch_detail(self, illust_id: str) -> dict[str, Any]:
        """作品详情：标题、作者、标签、原图地址。"""
        try:
            data = await fetch_json(
                DETAIL_URL.format(illust_id=illust_id),
                headers=_BASE_HEADERS,
                proxy=self.proxy or None,
                timeout=25.0,
            )
        except Exception as exc:
            logger.warning(f"[juhkff] Pixiv 详情失败 {illust_id}: {exc}")
            return {}
        body = (data or {}).get("body") or {}
        if not body:
            return {}
        urls = body.get("urls") or {}
        return {
            "title": str(body.get("title") or ""),
            "author": str(body.get("userName") or ""),
            "tags": [
                str(t.get("tag", "")) for t in (body.get("tags") or {}).get("tags", [])
            ][:6]
            if isinstance(body.get("tags"), dict)
            else [],
            "original": str(urls.get("original") or urls.get("regular") or ""),
            "regular": str(urls.get("regular") or urls.get("small") or ""),
            "page_url": PAGE_URL.format(illust_id=illust_id),
        }

    async def _download(self, item: dict[str, Any]) -> str:
        """下载并落地图片，返回本地路径；全部失败返回空串。"""
        url = item.get("original") or item.get("regular")
        if not url:
            return ""
        folder = subdir("pixiv")
        target = folder / f"{item['id']}_{_slug(item.get('title', ''))}.jpg"
        if target.exists():
            return str(target)

        payload = await self._try_download(url)
        if not payload:
            # 原图挂了：先试 regular 尺寸，再试 i.pixiv.re 镜像
            for candidate in (item.get("regular"), _mirror(url)):
                if not candidate or candidate == url:
                    continue
                payload = await self._try_download(candidate)
                if payload:
                    break
        if not payload:
            logger.warning(f"[juhkff] Pixiv 图片下载失败 {item['id']}")
            return ""

        # 图太大就压到 limit 以内（Pillow，跑在线程池里不阻塞）
        limit = float(self.cfg.get("max_image_mb", 8.0)) * 1024 * 1024
        if len(payload) > limit:
            payload = await asyncio.to_thread(_compress, payload, int(limit))
        try:
            await asyncio.to_thread(target.write_bytes, payload)
        except OSError as exc:
            logger.warning(f"[juhkff] Pixiv 图片落盘失败: {exc}")
            return ""
        return str(target)

    async def _try_download(self, url: str) -> bytes:
        """Pixiv 图片有防盗链，必须带 Referer，否则 403。"""
        try:
            return await fetch_bytes(
                url,
                headers={**_BASE_HEADERS, "Referer": "https://www.pixiv.net/"},
                proxy=self.proxy or None,
                timeout=40.0,
            )
        except Exception as exc:
            self.debug(f"[juhkff] Pixiv 下载失败 {truncate(url, 80)}: {exc}")
            return b""

    # ------------------------------------------------------------ 推送

    async def _broadcast(self, items: list[dict[str, Any]]) -> None:
        """把新作品推给所有 targets（群号或完整 umo）。"""
        targets = [str(x) for x in (self.cfg.get("targets") or []) if x]
        if not targets:
            logger.warning(
                "[juhkff] Pixiv 订阅未配置推送目标（targets），"
                f"已抓到 {len(items)} 张但无处可发"
            )
            return
        for item in items:
            chain = await self._compose(item)
            for umo in targets:
                try:
                    await self.plugin.context.send_message(umo, chain)
                    await asyncio.sleep(1.5)
                except Exception:
                    logger.exception(f"[juhkff] Pixiv 推送失败: {umo}")

    async def _compose(self, item: dict[str, Any]) -> MessageChain:
        """一条消息 = 文字说明 + 图片；图没下载成功就退化成文字 + 作品链接。"""
        caption = self._caption(item)
        chain = MessageChain().message(caption)
        path = str(item.get("image") or "")
        if path and Path(path).exists():
            chain = chain.file_image(path)
        else:
            link = item.get("page_url") or item.get("page") or ""
            if link:
                chain = chain.message(f"\n作品页：{link}")
        return chain

    def _caption(self, item: dict[str, Any]) -> str:
        lines = ["🎨 Pixiv 推荐"]
        if item.get("title"):
            lines.append(f"标题：{item['title']}")
        if item.get("author"):
            lines.append(f"作者：{item['author']}")
        tags = item.get("tags") or []
        if tags:
            lines.append("标签：" + "、".join(tags))
        if item.get("page_url"):
            lines.append(f"Pixiv：{item['page_url']}")
        return "\n".join(lines)

    async def _persist_seen(self) -> None:
        """只保留最近 2000 个 id，防止 seen 文件无限变大。"""
        if not self._seen_path:
            return
        recent = list(self._seen)[-2000:]
        self._seen = set(recent)
        await asyncio.to_thread(save_json, self._seen_path, recent)


# ---------------------------------------------------------------- 工具


def _mirror(url: str) -> str:
    """把 i.pximg.net 的地址换成 i.pixiv.re 镜像（同样路径）。"""
    match = re.match(r"https?://i\.pximg\.net/(.*)$", url or "")
    if not match:
        return ""
    return f"{MIRROR}/{match.group(1)}"


def _slug(text: str) -> str:
    """标题转成安全的文件名片段：只留字母数字、中文和连字符。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", text or "")[:30].strip("_")
    return cleaned or "illust"


def _compress(payload: bytes, limit: int) -> bytes:
    """把过大的图片压到 limit 以内；Pillow 不可用时原样返回。"""
    try:
        from PIL import Image as PILImage
    except Exception:
        return payload

    try:
        image = PILImage.open(io.BytesIO(payload))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        budget = limit * 0.9
        quality = 88
        buffer = io.BytesIO()
        while True:
            buffer.seek(0)
            buffer.truncate()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            if buffer.tell() <= budget or quality <= 45:
                break
            quality -= 8
        return buffer.getvalue()
    except Exception:
        return payload
