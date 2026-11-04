"""Render self-contained HTML locally with a shared Chromium instance.

Callers may choose a viewport and scale; existing help images keep the default
608px viewport. Rendering failures never invoke a remote rendering service.
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

# Keep existing callers' viewport and pixel density unless explicitly overridden.
VIEWPORT: dict[str, int] = {"width": 608, "height": 1100}
DEVICE_SCALE_FACTOR = 5


class LocalHtmlRenderer:
    """Lazily start one browser and serialize rendering and shutdown."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None

    @staticmethod
    def available() -> bool:
        """Check whether Playwright can be imported.

        Returns:
            Whether the local rendering dependency is installed.
        """
        try:
            import playwright  # noqa: F401
        except Exception:
            return False
        return True

    async def _cleanup(self, page: Any = None, *, close_browser: bool = False) -> None:
        """Finish resource cleanup while the caller holds the render lock.

        Args:
            page: Page to close, if one was created.
            close_browser: Whether to discard the shared browser and driver too.
        """

        async def release() -> None:
            nonlocal close_browser
            if page is not None:
                try:
                    await page.close()
                except (Exception, asyncio.CancelledError):
                    logger.exception("[juhkff] Closing the render page failed")
                    close_browser = True
            if close_browser:
                browser, playwright = self._browser, self._playwright
                self._browser = self._playwright = None
                if browser is not None:
                    try:
                        await browser.close()
                    except (Exception, asyncio.CancelledError):
                        logger.exception("[juhkff] Closing the local browser failed")
                if playwright is not None:
                    try:
                        await playwright.stop()
                    except (Exception, asyncio.CancelledError):
                        logger.exception("[juhkff] Stopping Playwright failed")

        # Keep the lock until cleanup finishes, including repeated cancellation.
        cleanup = asyncio.create_task(release())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            if not close_browser:
                await self._cleanup(close_browser=True)
            raise asyncio.CancelledError

    async def _ensure_browser(self) -> Any:
        """Reuse a connected browser or start a fresh local instance.

        Returns:
            The connected browser, with its driver owned by this renderer.
        """
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        await self._cleanup(close_browser=True)
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars"]
            )
        except BaseException:
            await self._cleanup(close_browser=True)
            raise
        logger.info("[juhkff] Local renderer started (Chromium headless)")
        return self._browser

    async def render(
        self,
        html: str,
        *,
        timeout: float = 120,
        scale: float = DEVICE_SCALE_FACTOR,
        viewport: dict[str, int] | None = None,
    ) -> bytes:
        """Render a page locally and capture its full content.

        Args:
            html: Self-contained HTML document to render.
            timeout: Maximum page loading time in seconds.
            scale: Device pixel density used for the screenshot.
            viewport: Optional viewport dimensions in CSS pixels.

        Returns:
            The rendered PNG bytes.
        """
        async with self._lock:
            page = None
            failed = True
            try:
                browser = await self._ensure_browser()
                page = await browser.new_page(
                    viewport=viewport or VIEWPORT, device_scale_factor=scale
                )
                try:
                    await page.set_content(
                        html, wait_until="load", timeout=int(timeout * 1000)
                    )
                except Exception:
                    # A slow image must not prevent capturing the existing DOM.
                    logger.warning(
                        "[juhkff] Local page loading failed; capturing current content"
                    )
                png = await page.screenshot(full_page=True, type="png")
                failed = False
                return bytes(png)
            finally:
                await self._cleanup(page, close_browser=failed)

    async def close(self) -> None:
        """Wait for active rendering and release the browser and driver."""
        async with self._lock:
            await self._cleanup(close_browser=True)


# Share one browser process across the plugin.
renderer = LocalHtmlRenderer()


async def render_template(
    template: str,
    payload: dict[str, Any],
    *,
    timeout: float = 120,
    scale: float = DEVICE_SCALE_FACTOR,
    viewport: dict[str, int] | None = None,
) -> bytes | None:
    """Render a Jinja template with the local Chromium instance.

    Args:
        template: Jinja template containing the complete HTML document.
        payload: Template variables.
        timeout: Maximum page loading time in seconds.
        scale: Device pixel density used for the screenshot.
        viewport: Optional viewport dimensions in CSS pixels.

    Returns:
        PNG bytes, or None if local rendering is unavailable or fails.
    """
    if not LocalHtmlRenderer.available():
        return None
    try:
        from jinja2 import Environment

        html = Environment(autoescape=False).from_string(template).render(**payload)
        return await renderer.render(
            html, timeout=timeout, scale=scale, viewport=viewport
        )
    except Exception:
        logger.exception(
            "[juhkff] Local rendering failed; check playwright install chromium"
        )
        return None
