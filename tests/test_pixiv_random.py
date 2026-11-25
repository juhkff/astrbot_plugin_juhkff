"""Offline coverage of random discovery delivery and revocation guards."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, Mock, patch

from juhkff_modules import pixiv_random as random_push

ORIGIN = "test:GroupMessage:1"
OTHER = "test:GroupMessage:2"


class RandomHost(random_push.PixivRandomMixin):
    def __init__(self):
        self.cfg: dict[str, Any] = {
            "random_push": {"enable": True, "groups": [ORIGIN], "min_views": 10000}
        }
        self._stopped = False
        self.enabled = True
        self.proxy = ""
        self.tz_name = "Asia/Shanghai"
        self.log = Mock()
        self.log.scope.side_effect = nullcontext
        self._push_history_lock = asyncio.Lock()
        self._random_push_recent: dict[str, list[str]] = {}
        self._random_push_history_path = "unused-history.json"
        self._api_headers = Mock(return_value={})
        self._content_policy = Mock(return_value=(True, "link"))
        self._fetch_detail = AsyncMock(
            side_effect=lambda iid: {"id": iid, "view_count": 10000, "x_restrict": 0}
        )
        self._render_items = AsyncMock(return_value="rendered")
        self.plugin = SimpleNamespace(
            context=SimpleNamespace(send_message=AsyncMock(return_value=True))
        )


class PixivRandomTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = RandomHost()
        self.fetch = AsyncMock(
            return_value={
                "error": False,
                "body": {"thumbnails": {"illust": [{"id": "7"}]}},
            }
        )
        patcher = patch.object(random_push, "fetch_json", self.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(random_push, "save_json")
        self.save = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(random_push.random, "shuffle")
        self.shuffle = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_first_push_waits_and_inclusive_delay_boundaries(self):
        for minutes, expected_time in (
            (1, "2026-11-21 00:00:00+08:00"),
            (120, "2026-11-21 01:59:00+08:00"),
        ):
            host = RandomHost()
            waits = []

            async def sleep(delay):
                waits.append(delay)
                if len(waits) == 1:
                    host.plugin.context.send_message.assert_not_awaited()
                    self.assertEqual(host._fetch_detail.await_count, 0)
                    host.log.info.assert_called_once_with(
                        "[juhkff] Pixiv random push scheduled: next=%s, wait_minutes=%s",
                        expected_time,
                        minutes,
                    )
                else:
                    host.log.info.assert_any_call(
                        "[juhkff] Pixiv random push triggered"
                    )
                    raise asyncio.CancelledError

            with (
                patch.object(
                    random_push,
                    "now",
                    return_value=datetime(2026, 11, 20, 15, 59, tzinfo=timezone.utc),
                ),
                patch.object(
                    random_push.random, "randint", return_value=minutes
                ) as rng,
                patch.object(random_push.asyncio, "sleep", side_effect=sleep),
                self.assertRaises(asyncio.CancelledError),
            ):
                await host.run_random_push()
            self.assertEqual(waits, [minutes * 60, minutes * 60])
            self.assertEqual(rng.call_count, 2)
            rng.assert_called_with(1, 120)
            host.plugin.context.send_message.assert_awaited_once_with(
                ORIGIN, "rendered"
            )

    async def test_configured_delay_range_and_invalid_bounds(self):
        cases = (
            ({"min_minutes": 5, "max_minutes": 30}, (5, 30)),
            ({"min_minutes": 30, "max_minutes": 5}, (5, 30)),
            ({"min_minutes": 10, "max_minutes": 10}, (10, 10)),
            ({"min_minutes": 180, "max_minutes": 300}, (180, 300)),
            ({"min_minutes": 0, "max_minutes": -2}, (1, 120)),
            ({"min_minutes": True, "max_minutes": "30"}, (1, 120)),
            ({"min_minutes": 1.5, "max_minutes": None}, (1, 120)),
            ({"min_minutes": 10}, (10, 120)),
            ({"max_minutes": 30}, (1, 30)),
        )
        for configured, bounds in cases:
            with self.subTest(configured=configured):
                host = RandomHost()
                host.cfg["random_push"].update(configured)
                with (
                    patch.object(
                        random_push.random, "randint", return_value=bounds[0]
                    ) as rng,
                    patch.object(
                        random_push.asyncio, "sleep", side_effect=asyncio.CancelledError
                    ) as sleep,
                    self.assertRaises(asyncio.CancelledError),
                ):
                    await host.run_random_push()
                rng.assert_called_once_with(*bounds)
                sleep.assert_awaited_once_with(bounds[0] * 60)
        self.fetch.assert_not_awaited()

    async def test_each_new_wait_reads_current_delay_configuration(self):
        self.host.cfg["random_push"].update(min_minutes=5, max_minutes=10)
        waits = []

        async def sleep(delay):
            waits.append(delay)
            if len(waits) == 1:
                self.host.cfg["random_push"].update(min_minutes=3, max_minutes=7)
            else:
                raise asyncio.CancelledError

        with (
            patch.object(random_push.random, "randint", side_effect=[8, 4]) as rng,
            patch.object(random_push.asyncio, "sleep", side_effect=sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.host.run_random_push()
        self.assertEqual(waits, [480, 240])
        self.assertEqual([call.args for call in rng.call_args_list], [(5, 10), (3, 7)])

    async def test_discovery_popularity_content_filter_and_shared_renderer(self):
        self.fetch.return_value["body"]["thumbnails"]["illust"] = [
            {"id": str(i)} for i in range(1, 6)
        ] + [{"id": "6", "illustType": 2}, {"id": "5"}, {"id": "bad"}]
        self.host._fetch_detail.side_effect = [
            {"id": "1", "view_count": 9999, "x_restrict": 0},
            {"id": "2", "view_count": 10000, "x_restrict": 1},
            {"id": "3", "view_count": 10000, "x_restrict": -1},
            {"id": "4", "view_count": True, "x_restrict": 0},
            {"id": "5", "view_count": 10000, "x_restrict": 0},
        ]
        result = await self.host._push_random_group(
            ORIGIN, deepcopy(self.host.cfg["random_push"])
        )
        self.assertEqual(result, 1)
        self.fetch.assert_awaited_once()
        call = self.fetch.await_args
        self.assertEqual(call.args[0], "https://www.pixiv.net/ajax/discovery/artworks")
        self.assertEqual(call.kwargs["params"], {"mode": "all", "limit": 60})
        self.shuffle.assert_called_once()
        render = self.host._render_items.await_args
        self.assertEqual(
            render.args, ([{"id": "5", "view_count": 10000, "x_restrict": 0}], ORIGIN)
        )
        self.assertTrue(render.kwargs["delivery_active"]())
        self.assertEqual(self.host._random_push_recent, {ORIGIN: ["5"]})

    async def test_bounded_details_without_popularity_fallback(self):
        self.fetch.return_value["body"]["thumbnails"]["illust"] = [
            {"id": str(i)} for i in range(1, 61)
        ]
        self.host._fetch_detail.side_effect = lambda iid: {
            "id": iid,
            "view_count": 9999,
            "x_restrict": 0,
        }
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 0
        )
        self.assertEqual(self.host._fetch_detail.await_count, 12)
        self.assertEqual(self.fetch.await_count, 3)
        for request in self.fetch.await_args_list:
            self.assertEqual(
                request.args[0], "https://www.pixiv.net/ajax/discovery/artworks"
            )
            self.assertEqual(request.kwargs["params"], {"mode": "all", "limit": 60})
        self.host._render_items.assert_not_awaited()
        self.host.plugin.context.send_message.assert_not_awaited()

    @staticmethod
    def discovery(*rows):
        return {
            "error": False,
            "body": {
                "thumbnails": {
                    "illust": [
                        row if isinstance(row, dict) else {"id": row} for row in rows
                    ]
                }
            },
        }

    async def test_refills_after_history_only_and_low_view_batches(self):
        self.host._random_push_recent[ORIGIN] = ["7"]
        self.fetch.side_effect = [
            self.discovery("7"),
            self.discovery("8"),
            self.discovery("9"),
        ]
        self.host._fetch_detail.side_effect = lambda iid: {
            "id": iid,
            "view_count": 9999 if iid == "8" else 10000,
            "x_restrict": 0,
        }
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(self.fetch.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in self.host._fetch_detail.await_args_list],
            ["8", "9"],
        )
        self.assertEqual(
            [call.args[0] for call in self.shuffle.call_args_list if call.args[0]],
            [["8"], ["9"]],
        )
        self.host.plugin.context.send_message.assert_awaited_once_with(
            ORIGIN, "rendered"
        )
        self.assertEqual(self.host._random_push_recent[ORIGIN], ["7", "9"])

    async def test_normalized_duplicates_and_previously_filtered_ids_are_not_retried(
        self,
    ):
        self.fetch.side_effect = [
            self.discovery(
                "0007", {"id": "8", "illustType": 2}, {"id": "9", "xRestrict": 1}
            ),
            self.discovery(7, "0008", "0009", "10"),
        ]
        self.host._fetch_detail.side_effect = lambda iid: {
            "id": iid,
            "view_count": 9999 if iid == "7" else 10000,
            "x_restrict": 0,
        }
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(self.fetch.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in self.host._fetch_detail.await_args_list],
            ["7", "10"],
        )

    async def test_unique_low_view_batches_stop_at_five_requests_and_sixty_details(
        self,
    ):
        self.fetch.side_effect = [
            self.discovery(*(str(i) for i in range(start, start + 60)))
            for start in range(1, 361, 60)
        ]
        self.host._fetch_detail.side_effect = lambda iid: {
            "id": iid,
            "view_count": 9999,
            "x_restrict": 0,
        }
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 0
        )
        self.assertEqual(self.fetch.await_count, 5)
        self.assertEqual(self.host._fetch_detail.await_count, 60)
        expected = [
            str(i) for start in range(1, 301, 60) for i in range(start, start + 12)
        ]
        self.assertEqual(
            [call.args[0] for call in self.host._fetch_detail.await_args_list], expected
        )
        self.assertEqual(self.shuffle.call_count, 5)
        for request in self.fetch.await_args_list:
            self.assertEqual(request.kwargs["params"], {"mode": "all", "limit": 60})
        self.host._render_items.assert_not_awaited()
        self.host.plugin.context.send_message.assert_not_awaited()

    async def test_two_empty_or_invalid_id_batches_stop_without_details(self):
        self.fetch.side_effect = [
            self.discovery(),
            self.discovery("0", "000", "bad", "-1", True),
            self.discovery("7"),
        ]
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 0
        )
        self.assertEqual(self.fetch.await_count, 2)
        self.host._fetch_detail.assert_not_awaited()
        self.host.plugin.context.send_message.assert_not_awaited()

    async def test_new_filtered_ids_reset_no_new_streak(self):
        self.host._random_push_recent[ORIGIN] = ["7"]
        self.fetch.side_effect = [
            self.discovery("7"),
            self.discovery("007"),
            self.discovery({"id": "8", "illustType": 2}, {"id": "9", "xRestrict": 1}),
            self.discovery("8", "009"),
            self.discovery("10"),
        ]
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(self.fetch.await_count, 5)
        self.host._fetch_detail.assert_awaited_once_with("10")
        self.host.plugin.context.send_message.assert_awaited_once_with(
            ORIGIN, "rendered"
        )

    async def test_detail_and_download_failures_refill_until_render_success(self):
        self.fetch.side_effect = [
            self.discovery("1"),
            self.discovery("2"),
            self.discovery("3"),
        ]
        self.host._fetch_detail.side_effect = [
            RuntimeError("detail unavailable"),
            {"id": "2", "view_count": 10000, "x_restrict": 0},
            {"id": "3", "view_count": 10000, "x_restrict": 0},
        ]
        self.host._render_items.side_effect = [None, "rendered"]
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(self.fetch.await_count, 3)
        self.assertEqual(self.host._render_items.await_count, 2)
        self.host.plugin.context.send_message.assert_awaited_once_with(
            ORIGIN, "rendered"
        )
        self.assertEqual(self.host._random_push_recent[ORIGIN], ["3"])

    async def test_second_discovery_invalid_response_or_network_error_propagates(self):
        for failure in (
            {"error": True},
            {"body": {}},
            RuntimeError("network unavailable"),
        ):
            with self.subTest(failure=failure):
                host = RandomHost()
                host._random_push_recent[ORIGIN] = ["7"]
                self.fetch.reset_mock()
                self.fetch.side_effect = [
                    self.discovery("7"),
                    failure,
                    self.discovery("8"),
                ]
                expected = (
                    RuntimeError if isinstance(failure, Exception) else ValueError
                )
                with self.assertRaises(expected):
                    await host._push_random_group(ORIGIN, host.cfg["random_push"])
                self.assertEqual(self.fetch.await_count, 2)
                host._fetch_detail.assert_not_awaited()
                host.plugin.context.send_message.assert_not_awaited()

    async def test_cancel_during_second_discovery_propagates(self):
        self.host._random_push_recent[ORIGIN] = ["7"]
        self.fetch.side_effect = [self.discovery("7"), asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"])
        self.assertEqual(self.fetch.await_count, 2)
        self.host._fetch_detail.assert_not_awaited()
        self.host.plugin.context.send_message.assert_not_awaited()

    async def test_revocation_during_second_discovery_prevents_details_and_delivery(
        self,
    ):
        for revoke in ("disable", "remove", "stop", "module", "threshold", "policy"):
            with self.subTest(revoke=revoke):
                host = RandomHost()
                host._random_push_recent[ORIGIN] = ["7"]
                snapshot = deepcopy(host.cfg["random_push"])
                calls = 0

                async def discovery(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return self.discovery("7")
                    if revoke == "disable":
                        host.cfg["random_push"]["enable"] = False
                    elif revoke == "remove":
                        host.cfg["random_push"]["groups"] = []
                    elif revoke == "stop":
                        host._stopped = True
                    elif revoke == "module":
                        host.enabled = False
                    elif revoke == "threshold":
                        host.cfg["random_push"]["min_views"] = 10001
                    else:
                        host._content_policy.return_value = (False, "original")
                    return self.discovery("8")

                with patch.object(random_push, "fetch_json", side_effect=discovery):
                    self.assertEqual(await host._push_random_group(ORIGIN, snapshot), 0)
                self.assertEqual(calls, 2)
                host._fetch_detail.assert_not_awaited()
                host._render_items.assert_not_awaited()
                host.plugin.context.send_message.assert_not_awaited()

    async def test_failed_send_aborts_without_refilling_or_trying_another_candidate(
        self,
    ):
        for failure in (False, RuntimeError("send failed")):
            with self.subTest(failure=failure):
                host = RandomHost()
                host.plugin.context.send_message.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                host.plugin.context.send_message.return_value = failure
                self.fetch.reset_mock()
                self.fetch.side_effect = [self.discovery("7", "8"), self.discovery("9")]
                with self.assertRaises(RuntimeError):
                    await host._push_random_group(ORIGIN, host.cfg["random_push"])
                self.fetch.assert_awaited_once()
                host._fetch_detail.assert_awaited_once_with("7")
                host.plugin.context.send_message.assert_awaited_once()
                self.assertEqual(host._random_push_recent, {})
                self.save.assert_not_called()

    async def test_group_policy_can_allow_restricted_work(self):
        self.host._content_policy.return_value = (False, "link")
        self.host._fetch_detail.return_value = {
            "id": "7",
            "view_count": 10000,
            "x_restrict": 2,
        }
        self.host._fetch_detail.side_effect = None
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.host._render_items.assert_awaited_once()

    async def test_revocation_after_each_await_prevents_delivery(self):
        for stage in ("discovery", "detail", "render"):
            for revoke in (
                "disable",
                "remove",
                "stop",
                "module",
                "threshold",
                "policy",
            ):
                with self.subTest(stage=stage, revoke=revoke):
                    host = RandomHost()
                    snapshot = deepcopy(host.cfg["random_push"])

                    def change():
                        if revoke == "disable":
                            host.cfg["random_push"]["enable"] = False
                        elif revoke == "remove":
                            host.cfg["random_push"]["groups"] = []
                        elif revoke == "stop":
                            host._stopped = True
                        elif revoke == "module":
                            host.enabled = False
                        elif revoke == "threshold":
                            host.cfg["random_push"]["min_views"] = 10001
                        else:
                            host._content_policy.return_value = (False, "original")

                    async def discovery(*args, **kwargs):
                        if stage == "discovery":
                            change()
                        return self.fetch.return_value

                    async def detail(iid):
                        if stage == "detail":
                            change()
                        return {"id": iid, "view_count": 10000, "x_restrict": 0}

                    async def render(*args, **kwargs):
                        self.assertTrue(kwargs["delivery_active"]())
                        if stage == "render":
                            change()
                            self.assertFalse(kwargs["delivery_active"]())
                        return "rendered"

                    host._fetch_detail.side_effect = detail
                    host._render_items.side_effect = render
                    with patch.object(random_push, "fetch_json", side_effect=discovery):
                        self.assertEqual(
                            await host._push_random_group(ORIGIN, snapshot), 0
                        )
                    host.plugin.context.send_message.assert_not_awaited()

    async def test_current_groups_after_wait_and_failures_are_isolated(self):
        host = self.host
        host.cfg["random_push"]["groups"] = ["old:GroupMessage:1"]
        sleeps = 0

        async def sleep(delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError
            host.cfg["random_push"]["groups"] = [
                ORIGIN,
                ORIGIN,
                OTHER,
                "private:FriendMessage:3",
                "bad",
                "x:GroupMessage:",
                7,
            ]

        host.plugin.context.send_message.side_effect = [
            RuntimeError("unavailable"),
            True,
        ]
        with patch.object(random_push.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await host.run_random_push()
        self.assertEqual(
            [call.args[0] for call in host.plugin.context.send_message.await_args_list],
            [ORIGIN, OTHER],
        )
        host.log.exception.assert_called_once()
        host.log.info.assert_any_call(
            "[juhkff] Pixiv random push completed: groups=%s", 1
        )

    async def test_disabled_after_sleep_skips_network_and_rewaits(self):
        waits = 0

        async def sleep(delay):
            nonlocal waits
            waits += 1
            self.host.cfg["random_push"]["enable"] = False
            if waits == 2:
                raise asyncio.CancelledError

        with patch.object(random_push.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.host.run_random_push()
        self.fetch.assert_not_awaited()
        self.assertEqual(waits, 2)
        self.assertEqual(self.host.log.info.call_count, 1)

    async def test_cancel_during_detail_propagates(self):
        self.host._fetch_detail.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"])
        self.host.plugin.context.send_message.assert_not_awaited()

    async def test_previous_work_and_ugoira_are_not_requested(self):
        self.host._random_push_recent[ORIGIN] = ["7"]
        self.fetch.return_value["body"]["thumbnails"]["illust"].extend(
            [{"id": "8", "illustType": 2}, {"id": "9", "xRestrict": 1}]
        )
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 0
        )
        self.host._fetch_detail.assert_not_awaited()

    async def test_history_prevents_nonconsecutive_repeats_and_exhaustion(self):
        self.fetch.return_value["body"]["thumbnails"]["illust"] = [
            {"id": "7"},
            {"id": "8"},
            {"id": "9"},
        ]
        for expected in ("7", "8", "9"):
            self.assertEqual(
                await self.host._push_random_group(
                    ORIGIN, self.host.cfg["random_push"]
                ),
                1,
            )
            self.assertEqual(self.host._random_push_recent[ORIGIN][-1], expected)
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 0
        )
        self.assertEqual(self.host._fetch_detail.await_count, 3)
        self.assertEqual(self.host.plugin.context.send_message.await_count, 3)
        self.assertEqual(self.save.call_count, 3)

    async def test_history_is_bounded_and_independent_between_groups(self):
        self.host._random_push_recent[ORIGIN] = [str(i) for i in range(100, 300)]
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(
            self.host._random_push_recent[ORIGIN],
            [str(i) for i in range(101, 300)] + ["7"],
        )
        self.host.cfg["random_push"]["groups"].append(OTHER)
        self.assertEqual(
            await self.host._push_random_group(OTHER, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(self.host._random_push_recent[OTHER], ["7"])

    async def test_unsuccessful_render_or_delivery_does_not_update_history(self):
        self.host._render_items.return_value = None
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 0
        )
        self.host._render_items.return_value = "rendered"
        for failure in (False, RuntimeError("send failed"), asyncio.CancelledError()):
            with self.subTest(failure=failure):
                send = self.host.plugin.context.send_message
                send.side_effect = (
                    failure if isinstance(failure, BaseException) else None
                )
                send.return_value = failure
                with self.assertRaises((RuntimeError, asyncio.CancelledError)):
                    await self.host._push_random_group(
                        ORIGIN, self.host.cfg["random_push"]
                    )
                self.assertEqual(self.host._random_push_recent, {})
                self.save.assert_not_called()

    async def test_history_save_failure_retains_memory_and_success(self):
        self.save.side_effect = OSError("disk full")
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(self.host._random_push_recent[ORIGIN], ["7"])
        self.host.log.exception.assert_called_once()
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 0
        )

    async def test_history_added_during_render_skips_duplicate_send(self):
        async def render(*args, **kwargs):
            async with self.host._push_history_lock:
                self.host._random_push_recent[ORIGIN] = ["7"]
            return "rendered"

        self.host._render_items.side_effect = render
        result = await asyncio.wait_for(
            self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(result, 0)
        self.host.plugin.context.send_message.assert_not_awaited()
        self.save.assert_not_called()
        self.assertEqual(self.host._random_push_recent[ORIGIN], ["7"])

    async def test_settings_revoked_while_waiting_for_history_lock_skip_send(self):
        rendered = asyncio.Event()

        async def render(*args, **kwargs):
            rendered.set()
            return "rendered"

        self.host._render_items.side_effect = render
        async with self.host._push_history_lock:
            task = asyncio.create_task(
                self.host._push_random_group(
                    ORIGIN, deepcopy(self.host.cfg["random_push"])
                )
            )
            try:
                await asyncio.wait_for(rendered.wait(), 1)
                self.host.cfg["random_push"]["enable"] = False
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        self.assertEqual(await asyncio.wait_for(task, 1), 0)
        self.host.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(self.host._random_push_recent, {})
        self.save.assert_not_called()

    async def test_success_merges_latest_history_after_render(self):
        self.host._random_push_recent[ORIGIN] = ["old"]

        async def render(*args, **kwargs):
            async with self.host._push_history_lock:
                self.host._random_push_recent[ORIGIN] = ["old", "monitor-work"]
            return "rendered"

        self.host._render_items.side_effect = render
        result = await asyncio.wait_for(
            self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.assertEqual(result, 1)
        self.assertEqual(
            self.host._random_push_recent[ORIGIN], ["old", "monitor-work", "7"]
        )
        self.save.assert_called_once_with(
            self.host._random_push_history_path, self.host._random_push_recent
        )

    async def test_invalid_discovery_fails_closed_and_detail_errors_continue(self):
        self.fetch.return_value = {"error": False, "body": {"recommendedIllusts": []}}
        with self.assertRaisesRegex(ValueError, "omitted its artwork collection"):
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"])
        self.fetch.return_value = {
            "body": {"thumbnails": {"illust": [{"id": "1"}, {"id": "2"}]}}
        }
        self.host._fetch_detail.side_effect = [
            RuntimeError("failed"),
            {"id": "2", "view_count": 10000, "x_restrict": 0},
        ]
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 1
        )
        self.host.log.exception.assert_called_once()


if __name__ == "__main__":
    main()
