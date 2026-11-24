"""Offline regressions for incremental Pixiv ranking delivery."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, Mock, patch

from juhkff_modules import pixiv_random as random_push
from juhkff_modules import pixiv_ranking_monitor as monitor
from juhkff_modules.pixiv_ranking_monitor import PixivRankingMonitorMixin


class MonitorHost(PixivRankingMonitorMixin):
    def __init__(self):
        self.groups = ["test:GroupMessage:1", "test:GroupMessage:2"]
        self.cfg: dict[str, Any] = {
            "ranking": {
                "monitor": {
                    "enable": True,
                    "mode": "daily",
                    "pages": 10,
                    "check_minutes": 30,
                    "groups": self.groups.copy(),
                }
            }
        }
        self.enabled = True
        self._stopped = False
        self._checks = set()
        self._ranking_monitor_ids = None
        self._ranking_monitor_pending = {}
        self._push_history_lock = asyncio.Lock()
        self._random_push_recent = {}
        self._random_push_history_path = Path("unused.json")
        self.tz_name = "Asia/Shanghai"
        self.log = Mock()
        self.scheduler = SimpleNamespace(every_minutes=AsyncMock())
        self.plugin = SimpleNamespace(
            context=SimpleNamespace(send_message=AsyncMock(return_value=True))
        )
        self._browse_candidates = AsyncMock(return_value=[("1", 1)])
        self._fetch_detail = AsyncMock(
            side_effect=lambda iid: {"id": iid, "x_restrict": 0}
        )
        self._content_policy = Mock(return_value=(True, "link"))
        self._render_items = AsyncMock(return_value="rendered")


class PixivRankingMonitorTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.save = self.enterContext(patch.object(monitor, "save_json"))
        self.host = MonitorHost()
        self.config = self.host.cfg["ranking"]["monitor"]
        self.send = self.host.plugin.context.send_message

    async def asyncTearDown(self):
        tasks = [task for task in self.host._checks if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_start_resets_state_schedules_and_tracks_initial_baseline(self):
        self.host._ranking_monitor_ids = {"old"}
        self.host._ranking_monitor_pending = {self.host.groups[0]: {"old": 2}}
        entered = asyncio.Event()
        release = asyncio.Event()

        async def candidates(*args):
            entered.set()
            await release.wait()
            return [("1", 1)]

        self.host._browse_candidates.side_effect = candidates
        await self.host.start_ranking_monitor()
        await asyncio.wait_for(entered.wait(), 1)
        self.host.scheduler.every_minutes.assert_awaited_once_with(
            30,
            self.host.check_ranking_updates,
            "pixiv-ranking-monitor",
            tz_name="Asia/Shanghai",
        )
        self.assertIsNone(self.host._ranking_monitor_ids)
        self.assertEqual(self.host._ranking_monitor_pending, {})
        tasks = list(self.host._checks)
        self.assertEqual(len(tasks), 1)
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        self.assertEqual(self.host._checks, set())
        self.assertEqual(self.host._ranking_monitor_ids, {"1"})
        self.send.assert_not_awaited()

    async def test_baseline_new_ids_rank_changes_and_reentry(self):
        await self.host.check_ranking_updates()
        self.host._browse_candidates.assert_awaited_once_with("daily", 10)
        self.send.assert_not_awaited()
        self.host._fetch_detail.assert_not_awaited()
        self.host._browse_candidates.return_value = [("2", 1), ("1", 2)]
        await self.host.check_ranking_updates()
        self.assertEqual(
            [call.args[0] for call in self.send.await_args_list], self.host.groups
        )
        for call in self.host._render_items.await_args_list:
            self.assertEqual(call.args[0], [{"id": "2", "x_restrict": 0}])
            self.assertEqual(call.kwargs["ranks"], {"2": 1})
            self.assertTrue(call.kwargs["delivery_active"]())
        self.send.reset_mock()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1)]
        await self.host.check_ranking_updates()
        self.send.assert_not_awaited()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 3)]
        await self.host.check_ranking_updates()
        self.send.assert_not_awaited()
        self.assertFalse(any(self.host._ranking_monitor_pending.values()))
        # Two hundred later sends evict the old work from each group's history.
        for origin in self.host.groups:
            self.host._random_push_recent[origin] = [str(i) for i in range(100, 300)]
        self.host._browse_candidates.return_value = [("1", 1)]
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 3)]
        await self.host.check_ranking_updates()
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.host._render_items.await_args.kwargs["ranks"], {"2": 3})

    async def test_failed_group_retries_without_duplicate_to_successful_group(self):
        await self.host.check_ranking_updates()
        first, second = self.host.groups

        async def send(origin, chain):
            if origin == first:
                raise RuntimeError("offline group")
            return True

        self.send.side_effect = send
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        await self.host.check_ranking_updates()
        self.assertEqual(
            {call.args[0] for call in self.send.await_args_list}, {first, second}
        )
        self.assertEqual(self.host._ranking_monitor_pending.get(first), {"2": 2})
        self.assertFalse(self.host._ranking_monitor_pending.get(second))
        self.send.reset_mock(side_effect=True)
        self.send.return_value = False
        await self.host.check_ranking_updates()
        self.send.assert_awaited_once_with(first, "rendered")
        self.assertEqual(self.host._ranking_monitor_pending.get(first), {"2": 2})
        self.send.reset_mock()
        self.send.return_value = True
        await self.host.check_ranking_updates()
        self.send.assert_awaited_once_with(first, "rendered")
        self.assertFalse(self.host._ranking_monitor_pending.get(first))
        self.send.reset_mock()
        await self.host.check_ranking_updates()
        self.send.assert_not_awaited()

    async def test_existing_history_skips_detail_render_send_and_pending(self):
        await self.host.check_ranking_updates()
        for origin in self.host.groups:
            self.host._random_push_recent[origin] = ["2"]
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        await self.host.check_ranking_updates()
        self.host._fetch_detail.assert_not_awaited()
        self.host._render_items.assert_not_awaited()
        self.send.assert_not_awaited()
        self.save.assert_not_called()
        self.assertFalse(any(self.host._ranking_monitor_pending.values()))

    async def test_history_is_independent_bounded_and_saved(self):
        await self.host.check_ranking_updates()
        first, second = self.host.groups
        self.host._random_push_recent = {
            first: ["2"],
            second: [str(i) for i in range(100, 300)],
        }
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        await self.host.check_ranking_updates()
        self.send.assert_awaited_once_with(second, "rendered")
        self.assertEqual(self.host._random_push_recent[first], ["2"])
        self.assertEqual(
            self.host._random_push_recent[second],
            [str(i) for i in range(101, 300)] + ["2"],
        )
        self.save.assert_called_once_with(
            self.host._random_push_history_path, self.host._random_push_recent
        )
        self.assertFalse(any(self.host._ranking_monitor_pending.values()))

    async def test_failed_send_does_not_record_history(self):
        self.config["groups"] = [self.host.groups[0]]
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        for failure in (False, RuntimeError("offline")):
            with self.subTest(failure=failure):
                self.send.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                self.send.return_value = False
                await self.host.check_ranking_updates()
                self.assertEqual(self.host._random_push_recent, {})
                self.save.assert_not_called()
                self.assertEqual(
                    self.host._ranking_monitor_pending[self.host.groups[0]], {"2": 2}
                )

    async def test_history_save_failure_retains_success_and_prevents_retry(self):
        origin = self.host.groups[0]
        self.config["groups"] = [origin]
        await self.host.check_ranking_updates()
        self.save.side_effect = OSError("disk full")
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        await self.host.check_ranking_updates()
        self.assertEqual(self.host._random_push_recent, {origin: ["2"]})
        self.assertFalse(self.host._ranking_monitor_pending[origin])
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1)]
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        await self.host.check_ranking_updates()
        self.send.assert_awaited_once_with(origin, "rendered")
        self.save.assert_called_once()

    async def test_history_added_during_render_is_checked_before_send(self):
        origin = self.host.groups[0]
        self.config["groups"] = [origin]
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]

        async def render(*args, **kwargs):
            async with self.host._push_history_lock:
                self.host._random_push_recent[origin] = ["2"]
            return "rendered"

        self.host._render_items.side_effect = render
        await asyncio.wait_for(self.host.check_ranking_updates(), 1)
        self.host._render_items.assert_awaited_once()
        self.send.assert_not_awaited()
        self.save.assert_not_called()
        self.assertFalse(self.host._ranking_monitor_pending[origin])

    async def test_settings_revoked_while_waiting_for_history_lock_skip_send(self):
        self.config["groups"] = [self.host.groups[0]]
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        rendered = asyncio.Event()

        async def render(*args, **kwargs):
            rendered.set()
            return "rendered"

        self.host._render_items.side_effect = render
        async with self.host._push_history_lock:
            task = asyncio.create_task(self.host.check_ranking_updates())
            try:
                await asyncio.wait_for(rendered.wait(), 1)
                self.config["enable"] = False
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        await asyncio.wait_for(task, 1)
        self.send.assert_not_awaited()
        self.assertEqual(self.host._random_push_recent, {})
        self.save.assert_not_called()

    async def test_random_and_monitor_concurrently_send_shared_work_only_once(self):
        class SharedHost(MonitorHost, random_push.PixivRandomMixin):
            pass

        host = SharedHost()
        origin = host.groups[0]
        host.cfg["ranking"]["monitor"]["groups"] = [origin]
        host.cfg["random_push"] = {
            "enable": True,
            "groups": [origin],
            "min_views": 10000,
        }
        host.proxy = ""
        host._api_headers = Mock(return_value={})
        host.log.scope.side_effect = nullcontext
        host._fetch_detail.side_effect = lambda iid: {
            "id": iid,
            "x_restrict": 0,
            "view_count": 10000,
        }
        await host.check_ranking_updates()
        host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        both_rendering = asyncio.Event()
        render_count = 0

        async def render(*args, **kwargs):
            nonlocal render_count
            render_count += 1
            if render_count == 2:
                both_rendering.set()
            await both_rendering.wait()
            return "rendered"

        async def send(*args):
            await asyncio.sleep(0)
            return True

        host._render_items.side_effect = render
        host.plugin.context.send_message.side_effect = send
        with (
            patch.object(
                random_push,
                "fetch_json",
                AsyncMock(
                    return_value={"body": {"thumbnails": {"illust": [{"id": "2"}]}}}
                ),
            ),
            patch.object(random_push, "save_json") as random_save,
        ):
            await asyncio.wait_for(
                asyncio.gather(
                    host.check_ranking_updates(),
                    host._push_random_group(origin, host.cfg["random_push"]),
                ),
                1,
            )
        self.assertEqual(render_count, 2)
        host.plugin.context.send_message.assert_awaited_once_with(origin, "rendered")
        self.assertEqual(host._random_push_recent, {origin: ["2"]})
        self.assertFalse(host._ranking_monitor_pending[origin])
        self.assertEqual(self.save.call_count + random_save.call_count, 1)

    async def test_pending_is_discarded_when_work_leaves_ranking(self):
        await self.host.check_ranking_updates()
        self.send.return_value = False
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        await self.host.check_ranking_updates()
        self.assertTrue(any(self.host._ranking_monitor_pending.values()))
        self.send.reset_mock()
        self.host._browse_candidates.return_value = [("1", 1)]
        await self.host.check_ranking_updates()
        self.send.assert_not_awaited()
        self.assertFalse(any(self.host._ranking_monitor_pending.values()))

    async def test_api_failure_and_empty_result_preserve_successful_baseline(self):
        for failure in (RuntimeError("API unavailable"), []):
            with self.subTest(failure=failure):
                self.host._ranking_monitor_ids = None
                self.host._browse_candidates.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                self.host._browse_candidates.return_value = []
                await self.host.check_ranking_updates()
                self.assertIsNone(self.host._ranking_monitor_ids)
                self.host._browse_candidates.side_effect = None
                self.host._browse_candidates.return_value = [("1", 1)]
                await self.host.check_ranking_updates()
                self.assertEqual(self.host._ranking_monitor_ids, {"1"})
                self.send.assert_not_awaited()
                self.host._browse_candidates.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                self.host._browse_candidates.return_value = []
                await self.host.check_ranking_updates()
                self.assertEqual(self.host._ranking_monitor_ids, {"1"})
                self.assertEqual(self.host._checks, set())

    async def test_disabled_and_missing_valid_groups_do_not_fetch(self):
        for groups, enabled in (
            ([], True),
            (["invalid"], True),
            (self.host.groups, False),
        ):
            with self.subTest(groups=groups, enabled=enabled):
                self.config.update(groups=groups, enable=enabled)
                await self.host.check_ranking_updates()
                self.host._browse_candidates.assert_not_awaited()
        self.config.update(groups=self.host.groups, enable=True)
        self.host.enabled = False
        await self.host.check_ranking_updates()
        self.host._browse_candidates.assert_not_awaited()
        self.host.enabled = True
        self.host._stopped = True
        await self.host.check_ranking_updates()
        self.host._browse_candidates.assert_not_awaited()

    async def test_unknown_restriction_and_filtered_r18_are_not_rendered(self):
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2), ("3", 3)]
        self.host._fetch_detail.side_effect = lambda iid: {
            "id": iid,
            "x_restrict": None if iid == "2" else 1,
        }
        await self.host.check_ranking_updates()
        self.host._render_items.assert_not_awaited()
        self.send.assert_not_awaited()
        self.assertFalse(any(self.host._ranking_monitor_pending.values()))

    async def test_removing_group_during_render_revokes_delivery(self):
        await self.host.check_ranking_updates()
        origin = self.host.groups[0]
        self.config["groups"] = [origin]
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]

        async def render(*args, **kwargs):
            self.assertTrue(kwargs["delivery_active"]())
            self.config["groups"] = []
            self.assertFalse(kwargs["delivery_active"]())
            return "rendered"

        self.host._render_items.side_effect = render
        await self.host.check_ranking_updates()
        self.host._render_items.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_disable_or_policy_change_after_detail_revokes_delivery(self):
        for change in ("disable", "policy", "stop", "mode"):
            with self.subTest(change=change):
                self.host = MonitorHost()
                self.config = self.host.cfg["ranking"]["monitor"]
                self.send = self.host.plugin.context.send_message
                await self.host.check_ranking_updates()
                self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
                self.host._content_policy.return_value = (False, "image")

                async def detail(iid):
                    if change == "disable":
                        self.config["enable"] = False
                    elif change == "policy":
                        self.host._content_policy.return_value = (True, "link")
                    elif change == "stop":
                        self.host._stopped = True
                    else:
                        self.config["mode"] = "weekly"
                    return {"id": iid, "x_restrict": 1}

                self.host._fetch_detail.side_effect = detail
                await self.host.check_ranking_updates()
                self.host._render_items.assert_not_awaited()
                self.send.assert_not_awaited()

    async def test_disable_during_snapshot_fetch_does_not_establish_baseline(self):
        async def candidates(*args):
            self.config["enable"] = False
            return [("1", 1)]

        self.host._browse_candidates.side_effect = candidates
        await self.host.check_ranking_updates()
        self.assertIsNone(self.host._ranking_monitor_ids)
        self.send.assert_not_awaited()
        self.assertEqual(self.host._checks, set())
        self.config["enable"] = True
        self.host._browse_candidates.side_effect = None
        await self.host.check_ranking_updates()
        self.assertEqual(self.host._ranking_monitor_ids, {"1"})
        self.send.assert_not_awaited()

    async def test_group_content_policy_is_applied_independently(self):
        await self.host.check_ranking_updates()
        first, second = self.host.groups
        self.host._content_policy.side_effect = lambda origin: (origin == first, "link")
        self.host._fetch_detail.side_effect = lambda iid: {"id": iid, "x_restrict": 1}
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        await self.host.check_ranking_updates()
        self.send.assert_awaited_once_with(second, "rendered")
        self.host._render_items.assert_awaited_once()
        self.assertEqual(self.host._render_items.await_args.args[1], second)
        self.assertFalse(any(self.host._ranking_monitor_pending.values()))

    async def test_removed_group_pending_is_dropped_without_sending(self):
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        self.send.return_value = False
        await self.host.check_ranking_updates()
        first, second = self.host.groups
        self.assertEqual(self.host._ranking_monitor_pending[first], {"2": 2})
        self.config["groups"] = [second]
        self.send.reset_mock()
        self.send.return_value = True
        await self.host.check_ranking_updates()
        self.send.assert_awaited_once_with(second, "rendered")
        self.assertNotIn(first, self.host._ranking_monitor_pending)

    async def test_source_switch_rebaselines_and_clears_old_pending(self):
        await self.host.check_ranking_updates()
        self.host._browse_candidates.return_value = [("1", 1), ("2", 2)]
        self.send.return_value = False
        await self.host.check_ranking_updates()
        self.assertTrue(any(self.host._ranking_monitor_pending.values()))
        self.config["mode"] = "weekly"
        self.host._browse_candidates.return_value = [("3", 1)]
        self.send.reset_mock()
        await self.host.check_ranking_updates()
        self.host._browse_candidates.assert_awaited_with("weekly", 10)
        self.assertEqual(self.host._ranking_monitor_ids, {"3"})
        self.assertFalse(any(self.host._ranking_monitor_pending.values()))
        self.send.assert_not_awaited()

    async def test_reentry_is_skipped_and_cancellation_releases_task(self):
        entered = asyncio.Event()

        async def candidates(*args):
            entered.set()
            await asyncio.Future()

        self.host._browse_candidates.side_effect = candidates
        task = asyncio.create_task(self.host.check_ranking_updates())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIn(task, self.host._checks)
        await self.host.check_ranking_updates()
        self.host._browse_candidates.assert_awaited_once()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.host._checks, set())
        self.host._browse_candidates.side_effect = None
        await self.host.check_ranking_updates()
        self.assertEqual(self.host._ranking_monitor_ids, {"1"})
        self.send.assert_not_awaited()


if __name__ == "__main__":
    main()
