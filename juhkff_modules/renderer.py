"""本地 HTML 渲染：用 playwright 驱动本机 Chromium 截图。

旧插件（Yunzai）是在机器人所在机器上用 puppeteer 渲染的：视口 578×1885、
deviceScaleFactor 5，图片也由本机下载。这里照搬同样的参数，日报就不再依赖
远端 t2i 服务，HTML 与聊天相关数据也不会离开这台机器。

只做一件事：拿一段完整 HTML（CSS/图片已经内联好），返回 PNG 字节。
没有 playwright 或 Chromium 时由调用方退回纯文本。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

# 与上游 page.ts:92-96 完全一致：578×1885 的画面，5 倍像素密度 → 2890px 宽
VIEWPORT: dict[str, int] = {"width": 608, "height": 1100}
DEVICE_SCALE_FACTOR = 5


class LocalHtmlRenderer:
    """单例式封装：懒启动浏览器，串行渲染，close() 时关掉。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None

    @staticmethod
    def available() -> bool:
        """playwright 装了没（Chromium 是否下好由首次渲染时暴露）。"""
        try:
            import playwright  # noqa: F401
        except Exception:
            return False
        return True

    async def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars"]
        )
        logger.info("[juhkff] 本地渲染器已启动（Chromium headless）")
        return self._browser

    async def render(
        self, html: str, *, timeout: float = 120, scale: int = DEVICE_SCALE_FACTOR
    ) -> bytes:
        """渲染整页并截图，返回 PNG 字节。

        `wait_until="load"` 而不是 networkidle：封面/图标偶尔连不上时不会
        把整张图拖到超时，页面主体结构已就绪即可截图。
        """
        async with self._lock:
            browser = await self._ensure_browser()
            page = await browser.new_page(
                viewport=VIEWPORT, device_scale_factor=scale
            )
            try:
                try:
                    await page.set_content(
                        html, wait_until="load", timeout=int(timeout * 1000)
                    )
                except Exception:
                    # 有图片卡住时 set_content 会超时，但 DOM 已经在了，照样截
                    logger.warning("[juhkff] 本地渲染：页面加载超时，按当前内容截图")
                png = await page.screenshot(full_page=True, type="png")
            finally:
                await page.close()
            return bytes(png)

    async def close(self) -> None:
        """插件卸载/重载时调用，避免留下孤儿 Chromium 进程。"""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.exception("[juhkff] 关闭本地浏览器失败")
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.exception("[juhkff] 停止 playwright 失败")
            self._playwright = None


# 插件内共用一个浏览器进程
renderer = LocalHtmlRenderer()


async def render_template(
    template: str, payload: dict, *, timeout: float = 120, scale: int = DEVICE_SCALE_FACTOR
) -> bytes | None:
    """Jinja2 渲染模板 → 本地 Chromium 截图。不可用或失败时返回 None。"""
    if not LocalHtmlRenderer.available():
        return None
    try:
        from jinja2 import Environment

        html = Environment(autoescape=False).from_string(template).render(**payload)
        return await renderer.render(html, timeout=timeout, scale=scale)
    except Exception:
        logger.exception("[juhkff] 本地渲染失败（检查是否执行过 playwright install chromium）")
        return None
