"""Offline checks for shared HTTP integration and daily report caching."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, patch

import aiohttp
from juhkff_modules import active_chat, common, daily_report, report_sources


class HTTPIntegrationTests(IsolatedAsyncioTestCase):
    async def test_json_query_keeps_existing_values_and_encodes_new_parameters(self):
        with patch.object(
            common,
            "public_request",
            AsyncMock(return_value=(b'{"ok": true}', "https://example.com")),
        ) as request:
            result = await common.fetch_json(
                "https://example.com/api?existing=1",
                params={"text": "a b&c", "tag": ["x", "y"]},
                allowed_hosts={"example.com"},
            )
        assert request.await_args is not None
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            request.await_args.args[0],
            "https://example.com/api?existing=1&text=a+b%26c&tag=x&tag=y",
        )
        self.assertEqual(request.await_args.kwargs["allowed_hosts"], {"example.com"})

    async def test_download_keeps_original_bytes_and_a_bounded_response(self):
        payload = b"original\x00binary\xff"
        with patch.object(
            common,
            "public_request",
            AsyncMock(return_value=(payload, "https://example.com/a")),
        ) as request:
            self.assertIs(
                await common.download_image_bytes("https://example.com/a"), payload
            )
        assert request.await_args is not None
        self.assertEqual(request.await_args.kwargs["max_bytes"], 64 * 1024 * 1024)

    async def test_active_tool_rejects_private_ports_and_schemes_before_dns(self):
        plugin: Any = SimpleNamespace(
            config={"global": {"enabled": True}, "active_chat": {"enable": True}}
        )
        module = active_chat.ActiveChatModule(plugin)
        with patch("socket.getaddrinfo", side_effect=AssertionError("No DNS expected")):
            for url in (
                "http://127.0.0.1/",
                "https://example.com:6185/",
                "file:///etc/passwd",
                "https://user:password@example.com",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    await module._fetch_public_page(url)

    async def test_active_tool_uses_global_proxy_and_bounded_original_text(self):
        plugin: Any = SimpleNamespace(
            config={
                "global": {"enabled": True, "proxy": "http://127.0.0.1:7890"},
                "active_chat": {"enable": True},
            }
        )
        module = active_chat.ActiveChatModule(plugin)
        with patch.object(
            active_chat,
            "public_request",
            AsyncMock(return_value=("中文正文".encode(), "https://example.com/")),
        ) as request:
            self.assertEqual(
                await module._fetch_public_page("https://example.com/"), "中文正文"
            )
        assert request.await_args is not None
        self.assertEqual(request.await_args.kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(request.await_args.kwargs["max_bytes"], 300000)

    async def test_report_retains_environment_proxy_and_existing_error_fallbacks(self):
        session: Any = SimpleNamespace(headers={"User-Agent": "test"}, trust_env=True)
        with patch.object(
            report_sources,
            "public_request",
            AsyncMock(side_effect=RuntimeError("HTTP 503 https://example.com/")),
        ) as request:
            with self.assertRaises(aiohttp.ClientError):
                await report_sources._request(
                    session,
                    "https://example.com/",
                    None,
                    7,
                    image=True,
                    referer="https://article.example/",
                )
        assert request.await_args is not None
        self.assertTrue(request.await_args.kwargs["trust_env"])
        self.assertEqual(request.await_args.kwargs["max_bytes"], 10 * 1024 * 1024)
        self.assertEqual(
            request.await_args.kwargs["headers"]["Referer"], "https://article.example/"
        )


class DailyReportTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        patcher = patch.object(daily_report, "data_dir", return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        plugin: Any = SimpleNamespace(
            config={
                "global": {"enabled": True},
                "daily_report": {
                    "enable": True,
                    "style": "crosspoint",
                    "pre_handle_retry_sec": 30,
                },
            }
        )
        self.module = daily_report.DailyReportModule(plugin)
        self.day = date(2026, 11, 4)
        self.module._today = lambda: self.day
        self.cache = self.folder / "daily_report" / self.day.isoformat()
        self.cache.mkdir(parents=True)
        self.sections = {key: [] for key in daily_report._SECTION_KEYS}
        self.sections[daily_report._SECTION_KEYS[0]] = [
            {
                "title": "A real headline",
                "source": "Publisher",
                "url": "https://example.com/article",
                "published": "2026-11-04",
                "image_path": None,
                "image_source": None,
            }
        ]

    async def test_cached_image_does_not_wait_for_another_styles_generation(self):
        image = self.cache / "crosspoint.png"
        image.write_bytes(b"cached image")
        self.module._collect = AsyncMock()
        await self.module._lock.acquire()
        try:
            self.assertEqual(
                await asyncio.wait_for(self.module._generate(), 0.2), image
            )
        finally:
            self.module._lock.release()
        self.module._collect.assert_not_awaited()

    async def test_concurrent_cache_misses_only_collect_and_render_once(self):
        self.module._collect = AsyncMock(return_value=self.sections)
        with (
            patch.object(daily_report, "build_report", return_value="html"),
            patch.object(
                daily_report.renderer, "render", AsyncMock(return_value=b"PNG")
            ) as render,
        ):
            results = await asyncio.gather(
                self.module._generate(), self.module._generate()
            )
        self.assertEqual(results[0], results[1])
        assert results[0] is not None
        self.assertEqual(results[0].read_bytes(), b"PNG")
        self.module._collect.assert_awaited_once()
        render.assert_awaited_once()

    async def test_valid_snapshot_avoids_network_and_invalid_snapshot_refreshes(self):
        snapshot = self.cache / "news.json"
        common.save_json(
            snapshot, {"date": self.day.isoformat(), "sections": self.sections}
        )
        with patch.object(
            daily_report, "collect_sections", AsyncMock(return_value=self.sections)
        ) as collect:
            self.assertEqual(
                await self.module._collect(self.day, self.cache), self.sections
            )
            collect.assert_not_awaited()
            invalid = dict(self.sections)
            invalid[daily_report._SECTION_KEYS[-1]] = [{"title": "missing fields"}]
            common.save_json(
                snapshot, {"date": self.day.isoformat(), "sections": invalid}
            )
            self.assertEqual(
                await self.module._collect(self.day, self.cache), self.sections
            )
            collect.assert_awaited_once()

    async def test_prewarm_retry_waits_outside_lock_and_only_retries_five_times(self):
        async def retry_wait(delay):
            self.assertFalse(self.module._lock.locked())
            self.assertEqual(delay, 30)

        self.module._generate = AsyncMock(return_value=None)
        with patch.object(
            daily_report.asyncio, "sleep", AsyncMock(side_effect=retry_wait)
        ) as sleep:
            await self.module._refresh_cache()
        self.assertEqual(sleep.await_count, 5)
        self.assertEqual(self.module._generate.await_count, 6)
        self.assertIsNone(self.module._refresh_task)


if __name__ == "__main__":
    main()
