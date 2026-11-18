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
from unittest.mock import AsyncMock, Mock, patch

from juhkff_modules import pixiv_push as pixiv
from PIL import Image
from astrbot.api.message_components import Image as MessageImage


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
            "x_restrict": 0,
            "original": f"https://i.pximg.net/original/{illust_id}_p0.png",
            "regular": f"https://i.pximg.net/regular/{illust_id}_p0.png",
        }

    async def test_subscription_aliases_share_validation_and_dynamic_help(self):
        for prefix in ("", "#", "/"):
            for verb in ("订阅", "取消订阅"):
                e = SimpleNamespace(
                    message_str=prefix + verb + " 7",
                    is_at_or_wake_command=False,
                    get_message_type=lambda: pixiv.MessageType.GROUP_MESSAGE,
                    plain_result=lambda text: pixiv.MessageChain().message(text),
                )
                self.assertTrue(self.module.intercepts(e))
                # Dict configuration cannot persist; reaching this error proves parsing succeeded.
                result = await self.module.handle(e)
                self.assertIn("订阅保存失败", result.chain[0].text)
                self.module.cfg["allow_prefixless"] = False
                self.assertFalse(self.module.intercepts(e))
                self.assertIsNone(await self.module.handle(e))
                self.module.cfg["allow_prefixless"] = True
        entries = self.module.help_entries()
        text = str(entries)
        for command in (
            "#订阅 UID",
            "#取消订阅 UID",
            "#日榜",
            "#周榜",
            "#月榜",
            "#随机 [关键词]",
            "#作品 ID或链接 [第N张]",
        ):
            self.assertIn(command, text)
        self.module.cfg["allow_prefixless"] = False
        self.assertNotIn("简写", str(self.module.help_entries()))
        self.assertIn("#pixiv作品", str(self.module.help_entries()))

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
        self.assertTrue(fetch.await_args.kwargs["proxy_resolve"])

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
            self.assertTrue(fetch.await_args.kwargs["proxy_resolve"])
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
        self.assertEqual(fetch.await_count, 2)
        self.assertTrue(
            all(
                call.args[0] == self.item()["original"]
                for call in fetch.await_args_list
            )
        )
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
        with patch.object(self.module.log, "exception"):
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
        self.assertEqual(len(calls[1].args[1].chain[0].nodes), 4)
        self.assertIn("Work 4", calls[0].args[1].chain[0].text)
        self.assertEqual(
            sum(isinstance(c, MessageImage) for c in calls[0].args[1].chain), 1
        )
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

        async def download(*args, **kwargs):
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

    async def test_subscription_guard_stops_remaining_pages_after_membership_changes(
        self,
    ):
        for change in ("cancel", "switch_group", "resubscribe"):
            with self.subTest(change=change):
                self.module.cfg["subscriptions"] = [
                    {"user_id": "7", "groups": [self.groups[0]]}
                ]
                self.module._artists["7"] = {"last_id": "1", "delivered": {}}
                self.module._fetch_ids = AsyncMock(return_value=["1", "2"])
                self.module._fetch_detail = AsyncMock(return_value=self.item(pages=3))
                self.module._fetch_pages = AsyncMock(
                    return_value=[self.item(pages=3)] * 3
                )
                self.module._get_bot_id = AsyncMock(return_value="100")
                self.plugin.context.send_message.reset_mock()

                async def download(*args, **kwargs):
                    if change == "cancel":
                        self.module.cfg["subscriptions"] = []
                        self.module._artists.clear()
                    elif change == "switch_group":
                        self.module.cfg["subscriptions"] = [
                            {"user_id": "7", "groups": [self.groups[1]]}
                        ]
                    else:
                        self.module._artists["7"] = {"last_id": "2", "delivered": {}}
                    self.assertFalse(kwargs["delivery_active"]())
                    return self.image

                self.module._download = AsyncMock(side_effect=download)
                with patch.object(self.module.log, "exception"):
                    await self.module._check_artist("7")
                self.module._download.assert_awaited_once()
                self.module._get_bot_id.assert_not_awaited()
                self.plugin.context.send_message.assert_not_awaited()

    async def test_download_guard_stops_before_validation_or_fallback(self):
        active = True

        async def fetch(*args, **kwargs):
            nonlocal active
            active = False
            return self.payload

        with (
            patch.object(pixiv, "fetch_bytes", AsyncMock(side_effect=fetch)) as request,
            patch.object(pixiv, "_prepare_image") as prepare,
        ):
            result = await self.module._download(
                self.item(), self.item(), 0, delivery_active=lambda: active
            )
        self.assertIsNone(result)
        request.assert_awaited_once()
        prepare.assert_not_called()

    async def test_group_policy_first_match_and_bare_group_ids(self):
        self.module.cfg.update(
            {
                "filter_r18": True,
                "r18_display": "link",
                "group_content_rules": [
                    {"groups": [], "filter_r18": False},
                    {"groups": ["1"], "filter_r18": False, "r18_display": "image"},
                    {"groups": [self.groups[0]], "filter_r18": True},
                ],
            }
        )
        self.assertEqual(self.module._content_policy(self.groups[0]), (False, "image"))
        self.assertEqual(self.module._content_policy(self.groups[1]), (True, "link"))

    async def test_filtered_subscription_advances_without_download_or_send(self):
        self.module._artists["7"] = {"last_id": "1", "delivered": {}}
        self.module._fetch_ids = AsyncMock(return_value=["1", "2"])
        item = {**self.item(), "x_restrict": 1}
        self.module._fetch_detail = AsyncMock(return_value=item)
        self.module._download = AsyncMock()
        self.assertTrue(await self.module._check_artist("7"))
        self.module._download.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(self.module._artists["7"]["last_id"], "2")

    async def test_group_subscription_override_delivers_source_link_only(self):
        self.module.cfg["group_content_rules"] = [
            {"groups": [self.groups[1]], "filter_r18": False, "r18_display": "link"}
        ]
        self.module._artists["7"] = {"last_id": "1", "delivered": {}}
        self.module._fetch_ids = AsyncMock(return_value=["1", "2"])
        item = {**self.item(), "x_restrict": 1}
        self.module._fetch_detail = AsyncMock(return_value=item)
        self.module._download = AsyncMock()
        self.assertTrue(await self.module._check_artist("7"))
        self.module._download.assert_not_awaited()
        self.plugin.context.send_message.assert_awaited_once()
        args = self.plugin.context.send_message.await_args.args
        self.assertEqual(args[0], self.groups[1])
        self.assertIn(item["original"], "\n".join(c.text for c in args[1].chain))

    async def test_login_check_classifies_results_without_logging_credentials(self):
        cases = [
            ({"error": False, "body": {"following": 3}}, None, "valid", True),
            (
                None,
                RuntimeError("HTTP 401 https://www.pixiv.net/ajax/user/extra"),
                "Cookie invalid",
                False,
            ),
            (
                {"error": True, "message": "Please log in"},
                None,
                "Cookie invalid",
                False,
            ),
            (
                None,
                pixiv.ClientConnectionError("fixture-secret"),
                "network error; status undetermined",
                False,
            ),
            (
                None,
                asyncio.TimeoutError("fixture-secret"),
                "network error; status undetermined",
                False,
            ),
            (
                None,
                RuntimeError("HTTP 403 fixture-secret"),
                "unexpected server response; status undetermined",
                False,
            ),
            (
                None,
                RuntimeError("HTTP 500 fixture-secret"),
                "unexpected server response; status undetermined",
                False,
            ),
            (
                {"error": True, "message": "Server unavailable"},
                None,
                "unexpected server response; status undetermined",
                False,
            ),
            (
                {"error": False},
                None,
                "unexpected server response; status undetermined",
                False,
            ),
            (
                "unexpected",
                None,
                "unexpected server response; status undetermined",
                False,
            ),
            (
                None,
                ValueError("fixture-secret"),
                "unexpected server response; status undetermined",
                False,
            ),
        ]
        self.module.cfg["cookie"] = "fixture-secret"
        for result, error, expected, valid in cases:
            with self.subTest(expected=expected, result=result, error=type(error)):
                fetch = AsyncMock(return_value=result, side_effect=error)
                with (
                    patch.object(pixiv, "fetch_json", fetch),
                    patch.object(self.module, "log") as log,
                ):
                    await self.module._check_login()
                fetch.assert_awaited_once()
                self.assertEqual(
                    fetch.await_args.args[0], "https://www.pixiv.net/ajax/user/extra"
                )
                self.assertEqual(
                    fetch.await_args.kwargs["headers"]["Cookie"],
                    "PHPSESSID=fixture-secret",
                )
                self.assertEqual(fetch.await_args.kwargs["proxy"], self.module.proxy)
                self.assertTrue(fetch.await_args.kwargs["proxy_resolve"])
                self.assertEqual(
                    fetch.await_args.kwargs["allowed_hosts"], {"www.pixiv.net"}
                )
                (log.info if valid else log.warning).assert_called_once_with(
                    f"[juhkff] Pixiv login: {expected}"
                )
                self.assertNotIn("fixture-secret", str(log.mock_calls))
                self.assertEqual(len(log.mock_calls), 1)

    async def test_login_check_skips_empty_invalid_disabled_and_logging_off(self):
        for cookie, enabled, print_log, expected in [
            ("", True, True, "anonymous"),
            ("PHPSESSID=", True, True, "Cookie invalid"),
            ("other=value", True, True, "Cookie invalid"),
            ("PHPSESSID=secret\r\nother: secret", True, True, "Cookie invalid"),
            ("secret", True, False, None),
            ("secret", False, True, None),
        ]:
            with self.subTest(cookie=cookie, enabled=enabled, print_log=print_log):
                self.module.cfg.update(
                    cookie=cookie, enable=enabled, print_log=print_log
                )
                with (
                    patch.object(pixiv, "fetch_json", AsyncMock()) as fetch,
                    patch.object(self.module, "log") as log,
                ):
                    await self.module._check_login()
                fetch.assert_not_awaited()
                if expected is None:
                    self.assertEqual(log.mock_calls, [])
                else:
                    self.assertIn(expected, str(log.mock_calls))
                    self.assertEqual(len(log.mock_calls), 1)
                    self.assertNotIn("secret", str(log.mock_calls))

    async def test_login_check_task_runs_once_per_start_and_stop_cancels_it(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.module.cfg["cookie"] = "fixture-secret"
        self.module.check = AsyncMock()
        self.module.start_browse = AsyncMock()
        with (
            patch.object(pixiv, "fetch_json", AsyncMock(side_effect=pending)) as fetch,
            patch.object(self.module, "log") as log,
        ):
            await self.module.start()
            await asyncio.wait_for(started.wait(), 1)
            task = self.module._login_task
            await self.module.stop()
            self.assertTrue(cancelled.is_set())
            self.assertTrue(task.cancelled())
            self.assertIsNone(self.module._login_task)
            log.warning.assert_not_called()
            fetch.side_effect = None
            fetch.return_value = {"error": False, "body": {}}
            await self.module.start()
            await asyncio.wait_for(self.module._login_task, 1)
            self.assertEqual(fetch.await_count, 2)
            log.info.assert_any_call("[juhkff] Pixiv login: valid")

    async def test_login_check_discards_result_if_cookie_changes_during_request(self):
        self.module.cfg["cookie"] = "old-secret"

        async def changed(*args, **kwargs):
            self.module.cfg["cookie"] = "new-secret"
            return {"error": False, "body": {}}

        with (
            patch.object(pixiv, "fetch_json", AsyncMock(side_effect=changed)),
            patch.object(self.module, "log", Mock()) as log,
        ):
            await self.module._check_login()
        self.assertEqual(log.mock_calls, [])

    def test_session_value_and_existing_cookie_headers(self):
        cases = [
            ("fixture-secret", "PHPSESSID=fixture-secret"),
            ("  fixture-secret  ", "PHPSESSID=fixture-secret"),
            ("PHPSESSID=fixture-secret", "PHPSESSID=fixture-secret"),
            (
                "PHPSESSID=fixture-secret; other=value",
                "PHPSESSID=fixture-secret; other=value",
            ),
            ("", None),
            ("   ", None),
            (None, None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.module.cfg["cookie"] = value
                self.assertEqual(self.module._api_headers().get("Cookie"), expected)
                self.assertEqual(self.module.cfg["cookie"], value)

    async def test_source_only_ignores_compression_and_never_sends_cookie_to_images(
        self,
    ):
        self.module.cfg.update(
            {"cookie": "PHPSESSID=fixture-secret", "max_image_mb": 0.000001}
        )
        item = self.item()
        self.assertEqual(
            self.module._api_headers()["Cookie"], "PHPSESSID=fixture-secret"
        )
        with patch.object(
            pixiv, "fetch_bytes", AsyncMock(return_value=self.payload)
        ) as fetch:
            path = await self.module._download(item, item, 0, source_only=True)
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertNotIn("Cookie", fetch.await_args.kwargs["headers"])


if __name__ == "__main__":
    main()
