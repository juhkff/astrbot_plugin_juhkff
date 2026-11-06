"""Offline regressions for Pixiv request safety and delivery checkpoints."""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, patch

from juhkff_modules import pixiv_push as pixiv
from PIL import Image


class PixivPushTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        patcher = patch.object(pixiv, "data_dir", return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.groups = ["test:GroupMessage:1", "test:GroupMessage:2"]
        self.plugin: Any = SimpleNamespace(
            global_enabled=True,
            config={
                "global": {"enabled": True, "proxy": "http://127.0.0.1:7890"},
                "pixiv_push": {
                    "enable": True,
                    "subscriptions": [{"user_id": "7", "groups": self.groups}],
                    "check_minutes": 1,
                    "retry_interval_minutes": 2,
                    "max_image_mb": 0,
                },
            },
            context=SimpleNamespace(send_message=AsyncMock(return_value=True)),
            scheduler=SimpleNamespace(every_minutes=AsyncMock()),
        )
        self.module = pixiv.PixivPushModule(self.plugin)
        self.modules = [self.module]
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
        self.payload = buffer.getvalue()
        self.image = self.folder / "fixture.png"
        self.image.write_bytes(self.payload)

    async def asyncTearDown(self):
        for module in self.modules:
            await module.stop()

    @staticmethod
    def item(illust_id: str = "2", pages: int = 1) -> pixiv.Illustration:
        return {
            "id": illust_id,
            "title": f"Work {illust_id}",
            "author": "Artist",
            "page_count": pages,
            "original": f"https://i.pximg.net/original/{illust_id}_p0.png",
            "regular": f"https://i.pximg.net/regular/{illust_id}_p0.png",
        }

    async def test_profile_excludes_manga_and_sorts_numeric_ids(self):
        response = {
            "error": False,
            "body": {"illusts": {"10": None, "2": None}, "manga": {"99": None}},
        }
        with patch.object(
            pixiv, "fetch_json", AsyncMock(return_value=response)
        ) as fetch:
            self.assertEqual(await self.module._fetch_ids("7"), ["2", "10"])
        assert fetch.await_args is not None
        self.assertEqual(fetch.await_args.kwargs["allowed_hosts"], {"www.pixiv.net"})

    async def test_detail_and_pages_reject_foreign_or_private_image_urls(self):
        for url in (
            "https://evil.example/image.png",
            "https://i.pximg.net.evil.example/x",
            "http://127.0.0.1/x",
        ):
            with self.subTest(url=url):
                response = {
                    "error": False,
                    "body": {
                        "id": "2",
                        "title": "Title",
                        "userName": "Artist",
                        "pageCount": 1,
                        "urls": {"original": url},
                    },
                }
                with patch.object(
                    pixiv, "fetch_json", AsyncMock(return_value=response)
                ):
                    with self.assertRaises(ValueError):
                        await self.module._fetch_detail("2")
                response = {
                    "error": False,
                    "body": [{"urls": {"original": url}}, {"urls": {"original": url}}],
                }
                with patch.object(
                    pixiv, "fetch_json", AsyncMock(return_value=response)
                ):
                    with self.assertRaises(ValueError):
                        await self.module._fetch_pages(self.item(pages=2))

    async def test_original_bytes_and_download_bound_survive_cache_reuse(self):
        item = self.item()
        with patch.object(
            pixiv, "fetch_bytes", AsyncMock(return_value=self.payload)
        ) as fetch:
            path = await self.module._download(item, item, 0, "7")
            assert path is not None
            assert fetch.await_args is not None
            self.assertEqual(path.read_bytes(), self.payload)
            self.assertEqual(fetch.await_args.args[0], item["original"])
            self.assertEqual(fetch.await_args.kwargs["max_bytes"], 64 * 1024 * 1024)
            self.assertEqual(
                fetch.await_args.kwargs["allowed_hosts"], {"i.pximg.net", "i.pixiv.re"}
            )
            self.assertEqual(fetch.await_args.kwargs["proxy"], "http://127.0.0.1:7890")
            self.assertEqual(await self.module._download(item, item, 0, "7"), path)
            fetch.assert_awaited_once()

    async def test_rate_limit_stops_url_fallback_and_does_not_advance(self):
        self.module._artists["7"] = {"last_id": "1", "delivered": {}}
        self.module._fetch_ids = AsyncMock(return_value=["1", "2"])
        self.module._fetch_detail = AsyncMock(return_value=self.item())
        with patch.object(
            pixiv,
            "fetch_bytes",
            AsyncMock(side_effect=RuntimeError("HTTP 429 https://i.pximg.net/x")),
        ) as fetch:
            self.assertFalse(await self.module._check_artist("7"))
        fetch.assert_awaited_once()
        self.assertEqual(self.module._artists["7"]["last_id"], "1")
        self.plugin.context.send_message.assert_not_awaited()

    async def test_all_works_and_pages_have_independent_group_watermarks(self):
        self.module._artists["7"] = {"last_id": "1", "delivered": {}}
        self.module._fetch_ids = AsyncMock(return_value=["1", "2", "3"])
        self.module._fetch_detail = AsyncMock(
            side_effect=lambda iid: self.item(iid, 2 if iid == "2" else 1)
        )
        self.module._fetch_pages = AsyncMock(
            side_effect=lambda item: [item] * item["page_count"]
        )
        self.module._download = AsyncMock(return_value=self.image)
        self.module._get_bot_id = AsyncMock(return_value="100")
        self.plugin.context.send_message.side_effect = [True, False]
        with patch.object(pixiv.logger, "exception"):
            self.assertFalse(await self.module._check_artist("7"))
        first_calls = self.plugin.context.send_message.await_args_list
        self.assertEqual(
            [len(call.args[1].chain[0].nodes) for call in first_calls], [3, 3]
        )
        self.assertEqual(
            self.module._artists["7"],
            {"last_id": "1", "delivered": {self.groups[0]: "3"}},
        )
        self.plugin.context.send_message.reset_mock(side_effect=True)
        self.plugin.context.send_message.return_value = True
        self.module._fetch_ids.return_value = ["1", "2", "3", "4"]
        self.assertTrue(await self.module._check_artist("7"))
        calls = self.plugin.context.send_message.await_args_list
        self.assertEqual([len(call.args[1].chain[0].nodes) for call in calls], [1, 4])
        self.assertIn("Work 4", calls[0].args[1].chain[0].nodes[0].content[0].text)
        self.assertEqual(self.module._artists["7"], {"last_id": "4", "delivered": {}})

    async def test_cold_restart_rebaselines_without_deleting_subscriptions(self):
        self.module._artists["7"] = {"last_id": "1", "delivered": {}}
        self.module._persist_state()
        fresh = pixiv.PixivPushModule(self.plugin)
        fresh._owner = "different-process-owner"
        fresh._fetch_ids = AsyncMock(return_value=["1", "2", "9"])
        self.modules.append(fresh)
        await fresh.start()
        assert fresh._baseline_task is not None
        await fresh._baseline_task
        self.assertEqual(fresh._artists["7"], {"last_id": "9", "delivered": {}})
        self.assertEqual(fresh._subscriptions()["7"], self.groups)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_hot_reload_preserves_partial_delivery_and_backoff(self):
        self.module._artists["7"] = {"last_id": "1", "delivered": {self.groups[0]: "2"}}
        self.module._due["7"] = time.monotonic() + 3600
        self.module._failures["7"] = 3
        self.module._persist_state()
        reloaded = pixiv.PixivPushModule(self.plugin)
        reloaded._fetch_ids = AsyncMock()
        self.modules.append(reloaded)
        await reloaded.start()
        assert reloaded._baseline_task is not None
        await reloaded._baseline_task
        self.assertEqual(reloaded._artists, self.module._artists)
        self.assertEqual(reloaded._due, self.module._due)
        self.assertEqual(reloaded._failures, {"7": 3})
        reloaded._fetch_ids.assert_not_awaited()

    async def test_linear_retry_intervals_are_configurable_and_success_resets(self):
        for base in (2, 3):
            self.module.cfg["retry_interval_minutes"] = base
            self.module._failures.clear()
            self.module._due.clear()
            self.module._check_artist = AsyncMock(return_value=False)
            clock = 1000.0
            with (
                patch.object(pixiv.time, "monotonic", side_effect=lambda: clock),
                patch.object(pixiv.time, "time", return_value=1200),
            ):
                for attempt in (1, 2, 3):
                    await self.module.check()
                    self.assertEqual(self.module._due["7"] - clock, base * attempt * 60)
                    clock = self.module._due["7"]
                self.module._check_artist.return_value = True
                await self.module.check()
                self.assertNotIn("7", self.module._failures)
                self.assertEqual(self.module._due["7"], clock + 60)

    async def test_cancel_during_download_cannot_send_or_restore_progress(self):
        self.module._artists["7"] = {"last_id": "1", "delivered": {}}
        self.module._fetch_ids = AsyncMock(return_value=["1", "2"])
        self.module._fetch_detail = AsyncMock(return_value=self.item())
        entered = asyncio.Event()
        release = asyncio.Event()

        async def download(*args):
            entered.set()
            await release.wait()
            return self.image

        with patch.object(self.module, "_download", new=download):
            task = asyncio.create_task(self.module._check_artist("7"))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.module.cfg["subscriptions"] = []
                self.module._artists.clear()
            finally:
                release.set()
                await task
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(self.module._artists, {})


if __name__ == "__main__":
    main()
