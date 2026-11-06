"""Deterministic local renderer lifecycle regressions without Chromium."""

import asyncio
import sys
import unittest
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from juhkff_modules import renderer


def make_driver():
    """Build a mock driver, connected browser, and page.

    Returns:
        A tuple containing the Playwright driver, browser, and page mocks.
    """
    page = SimpleNamespace(
        set_content=AsyncMock(),
        screenshot=AsyncMock(return_value=b"png"),
        close=AsyncMock(),
    )
    browser = SimpleNamespace(
        is_connected=Mock(return_value=True),
        new_page=AsyncMock(return_value=page),
        close=AsyncMock(),
    )
    driver = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
        stop=AsyncMock(),
    )
    return driver, browser, page


class RendererTests(unittest.IsolatedAsyncioTestCase):
    """Exercise browser recovery, serialized shutdown, and cancellation."""

    def setUp(self):
        self.renderer = renderer.LocalHtmlRenderer()
        self.start = AsyncMock()
        self.factory = Mock(return_value=SimpleNamespace(start=self.start))
        api: Any = ModuleType("playwright.async_api")
        api.async_playwright = self.factory
        playwright: Any = ModuleType("playwright")
        playwright.async_api = api
        self.modules = patch.dict(
            sys.modules, {"playwright": playwright, "playwright.async_api": api}
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.logs = patch.object(renderer, "logger")
        self.logs.start()
        self.addCleanup(self.logs.stop)
        self.addAsyncCleanup(self.renderer.close)

    async def test_connected_browser_is_reused_with_existing_defaults(self):
        driver, browser, page = make_driver()
        self.start.return_value = driver
        self.assertEqual(await self.renderer.render("first"), b"png")
        self.assertEqual(await self.renderer.render("second"), b"png")
        self.start.assert_awaited_once()
        driver.chromium.launch.assert_awaited_once()
        self.assertEqual(page.close.await_count, 2)
        browser.new_page.assert_awaited_with(
            viewport={"width": 608, "height": 1100}, device_scale_factor=5
        )
        driver.stop.assert_not_awaited()

    async def test_disconnected_browser_and_driver_are_released_then_relaunched(self):
        old_driver, old_browser, _ = make_driver()
        new_driver, _, _ = make_driver()
        self.start.side_effect = [old_driver, new_driver]
        await self.renderer.render("before crash")
        old_browser.is_connected.return_value = False
        old_browser.close.side_effect = RuntimeError("browser already crashed")
        self.assertEqual(await self.renderer.render("after crash"), b"png")
        old_browser.close.assert_awaited_once()
        old_driver.stop.assert_awaited_once()
        self.assertEqual(self.start.await_count, 2)

    async def test_screenshot_crash_cleans_resources_and_next_render_restarts(self):
        old_driver, old_browser, old_page = make_driver()
        new_driver, _, _ = make_driver()
        old_page.screenshot.side_effect = RuntimeError("target closed")
        self.start.side_effect = [old_driver, new_driver]
        with self.assertRaisesRegex(RuntimeError, "target closed"):
            await self.renderer.render("crash")
        old_page.close.assert_awaited_once()
        old_browser.close.assert_awaited_once()
        old_driver.stop.assert_awaited_once()
        self.assertIsNone(self.renderer._browser)
        self.assertIsNone(self.renderer._playwright)
        self.assertEqual(await self.renderer.render("recovered"), b"png")

    async def test_launch_failure_stops_initialized_driver_and_can_retry(self):
        old_driver, _, _ = make_driver()
        new_driver, _, _ = make_driver()
        old_driver.chromium.launch.side_effect = RuntimeError("launch failed")
        self.start.side_effect = [old_driver, new_driver]
        with self.assertRaisesRegex(RuntimeError, "launch failed"):
            await self.renderer.render("first launch")
        old_driver.stop.assert_awaited_once()
        self.assertIsNone(self.renderer._browser)
        self.assertIsNone(self.renderer._playwright)
        self.assertEqual(await self.renderer.render("retry"), b"png")

    async def test_close_waits_for_render_without_deadlock(self):
        driver, browser, page = make_driver()
        self.start.return_value = driver
        started, resume = asyncio.Event(), asyncio.Event()

        async def screenshot(**kwargs):
            started.set()
            await resume.wait()
            return b"finished"

        page.screenshot.side_effect = screenshot
        render_task = asyncio.create_task(self.renderer.render("active"))
        await asyncio.wait_for(started.wait(), timeout=2)
        close_task = asyncio.create_task(self.renderer.close())
        await asyncio.sleep(0)
        browser.close.assert_not_awaited()
        driver.stop.assert_not_awaited()
        self.assertFalse(close_task.done())
        resume.set()
        result, _ = await asyncio.wait_for(
            asyncio.gather(render_task, close_task), timeout=2
        )
        self.assertEqual(result, b"finished")
        page.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        driver.stop.assert_awaited_once()
        await self.renderer.close()
        self.assertEqual(browser.close.await_count, 1)

    async def test_cancelled_render_closes_page_browser_and_driver(self):
        driver, browser, page = make_driver()
        self.start.return_value = driver
        started = asyncio.Event()

        async def screenshot(**kwargs):
            started.set()
            await asyncio.Event().wait()

        page.screenshot.side_effect = screenshot
        task = asyncio.create_task(self.renderer.render("cancelled"))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        page.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        driver.stop.assert_awaited_once()
        self.assertIsNone(self.renderer._browser)
        self.assertIsNone(self.renderer._playwright)
        self.assertFalse(self.renderer._lock.locked())
        new_driver, _, _ = make_driver()
        self.start.return_value = new_driver
        self.assertEqual(await self.renderer.render("after cancellation"), b"png")

    async def test_cancellation_during_page_cleanup_discards_browser_too(self):
        driver, browser, page = make_driver()
        self.start.return_value = driver
        started, resume = asyncio.Event(), asyncio.Event()

        async def close():
            started.set()
            await resume.wait()

        page.close.side_effect = close
        task = asyncio.create_task(self.renderer.render("already captured"))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.renderer._lock.locked())
        resume.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        page.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        driver.stop.assert_awaited_once()
        self.assertIsNone(self.renderer._browser)
        self.assertIsNone(self.renderer._playwright)
        self.assertFalse(self.renderer._lock.locked())

    async def test_cancelled_shutdown_still_finishes_all_cleanup(self):
        driver, browser, _ = make_driver()
        self.start.return_value = driver
        await self.renderer.render("ready")
        started, resume = asyncio.Event(), asyncio.Event()

        async def close():
            started.set()
            await resume.wait()
            raise RuntimeError("close failed")

        browser.close.side_effect = close
        driver.stop.side_effect = RuntimeError("stop failed")
        task = asyncio.create_task(self.renderer.close())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.renderer._lock.locked())
        resume.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        driver.stop.assert_awaited_once()
        self.assertIsNone(self.renderer._browser)
        self.assertIsNone(self.renderer._playwright)
        self.assertFalse(self.renderer._lock.locked())

    async def test_page_close_failure_also_releases_browser_and_driver(self):
        driver, browser, page = make_driver()
        self.start.return_value = driver
        page.close.side_effect = RuntimeError("page close failed")
        browser.close.side_effect = RuntimeError("browser close failed")
        self.assertEqual(await self.renderer.render("rendered"), b"png")
        driver.stop.assert_awaited_once()
        self.assertIsNone(self.renderer._browser)
        self.assertIsNone(self.renderer._playwright)

    async def test_loading_failure_keeps_screenshot_and_viewport_override(self):
        driver, browser, page = make_driver()
        self.start.return_value = driver
        page.set_content.side_effect = TimeoutError("image load timeout")
        viewport = {"width": 880, "height": 900}
        self.assertEqual(
            await self.renderer.render("partial DOM", scale=2, viewport=viewport),
            b"png",
        )
        browser.new_page.assert_awaited_once_with(
            viewport=viewport, device_scale_factor=2
        )
        page.screenshot.assert_awaited_once_with(full_page=True, type="png")

    async def test_template_failure_remains_local_and_returns_none(self):
        with (
            patch.object(renderer.LocalHtmlRenderer, "available", return_value=True),
            patch.object(
                renderer.renderer,
                "render",
                new=AsyncMock(side_effect=RuntimeError("bad")),
            ) as render,
        ):
            self.assertIsNone(
                await renderer.render_template("{{ value }}", {"value": "x"})
            )
            render.assert_awaited_once()
        with (
            patch.object(renderer.LocalHtmlRenderer, "available", return_value=False),
            patch.object(renderer.renderer, "render", new=AsyncMock()) as render,
        ):
            self.assertIsNone(await renderer.render_template("unused", {}))
            render.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
