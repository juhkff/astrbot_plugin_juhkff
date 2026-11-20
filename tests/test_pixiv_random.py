"""Offline coverage of random discovery delivery and revocation guards."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
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
        self.log = Mock()
        self.log.scope.side_effect = nullcontext
        self._random_push_recent: dict[str, str] = {}
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
        patcher = patch.object(random_push.random, "shuffle")
        self.shuffle = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_first_push_waits_and_inclusive_delay_boundaries(self):
        for minutes in (1, 120):
            host = RandomHost()
            waits = []

            async def sleep(delay):
                waits.append(delay)
                if len(waits) == 1:
                    host.plugin.context.send_message.assert_not_awaited()
                    self.assertEqual(host._fetch_detail.await_count, 0)
                else:
                    raise asyncio.CancelledError

            with (
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
        self.assertEqual(self.host._random_push_recent, {ORIGIN: "5"})

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
        self.fetch.assert_awaited_once()
        self.host._render_items.assert_not_awaited()
        self.host.plugin.context.send_message.assert_not_awaited()

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
        host.log.info.assert_called_once_with(
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

    async def test_cancel_during_detail_propagates(self):
        self.host._fetch_detail.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"])
        self.host.plugin.context.send_message.assert_not_awaited()

    async def test_previous_work_and_ugoira_are_not_requested(self):
        self.host._random_push_recent[ORIGIN] = "7"
        self.fetch.return_value["body"]["thumbnails"]["illust"].extend(
            [{"id": "8", "illustType": 2}, {"id": "9", "xRestrict": 1}]
        )
        self.assertEqual(
            await self.host._push_random_group(ORIGIN, self.host.cfg["random_push"]), 0
        )
        self.host._fetch_detail.assert_not_awaited()

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
