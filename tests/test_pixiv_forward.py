"""Regressions for event sender IDs and bounded OneBot login lookups."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from juhkff_modules import pixiv_push as pixiv
from pixiv_test_utils import page_nodes

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Nodes
from astrbot.api.platform import MessageType


class PixivForwardTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.image = Path(temporary.name) / "fixture.png"
        self.image.write_bytes(b"fixture")
        self.lookup = Mock()
        plugin: Any = SimpleNamespace(
            global_enabled=True,
            config={
                "pixiv_push": {
                    "enable": True,
                    "ranking": {"count": 10, "pool_pages": 1},
                    "random": {"count": 10},
                }
            },
            context=SimpleNamespace(get_platform_inst=self.lookup),
        )
        self.module = pixiv.PixivPushModule(plugin)

    async def asyncTearDown(self):
        await self.module.stop()

    async def test_browse_uses_event_sender_for_all_33_pages_without_login_lookup(self):
        pages = [1, 1, 3, 1, 4, 2, 1, 4, 13, 3]
        self.lookup.side_effect = AssertionError("Unexpected login lookup")
        # A different account cached for this adapter must not override the event.
        self.module._bot_ids["test"] = "999"
        self.module._browse_candidates = AsyncMock(
            return_value=[(str(i), i) for i in range(1, 11)]
        )
        self.module._fetch_detail = AsyncMock(
            side_effect=lambda iid: {
                "id": iid,
                "title": "Work",
                "author": "Artist",
                "page_count": pages[int(iid) - 1],
                "x_restrict": 0,
                "original": f"https://i.pximg.net/{iid}.png",
                "regular": "",
            }
        )
        self.module._fetch_pages = AsyncMock(
            side_effect=lambda item: [item] * item["page_count"]
        )
        self.module._download = AsyncMock(return_value=self.image)
        for command, sender, count in (
            ("#pixiv日榜", "111", 33),
            ("#日榜", "222", 33),
            ("#pixiv随机", "333", 33),
            ("#pixiv作品 9", "444", 13),
        ):
            with self.subTest(command=command):
                event = cast(
                    AstrMessageEvent,
                    SimpleNamespace(
                        message_str=command,
                        is_at_or_wake_command=True,
                        unified_msg_origin="test:GroupMessage:1",
                        get_message_type=lambda: MessageType.GROUP_MESSAGE,
                        get_self_id=lambda: sender,
                    ),
                )
                result = await self.module.handle(event)
                assert result is not None
                nodes = result.chain[0]
                assert isinstance(nodes, Nodes)
                self.assertEqual(len(page_nodes(nodes.nodes)), count)
                self.assertEqual(len(nodes.nodes), 1 if "作品" in command else 10)
                self.assertTrue(
                    all(node.uin == sender for node in page_nodes(nodes.nodes))
                )
        self.lookup.assert_not_called()

    async def test_background_lookup_caches_a_valid_account(self):
        action = AsyncMock(return_value={"user_id": 123})
        self.lookup.return_value = SimpleNamespace(
            get_client=lambda: SimpleNamespace(call_action=action)
        )
        self.assertEqual(await self.module._get_bot_id("test:GroupMessage:1"), "123")
        self.assertEqual(await self.module._get_bot_id("test:GroupMessage:2"), "123")
        action.assert_awaited_once_with("get_login_info")

    async def test_scheduled_push_uses_configured_platform_without_any_group_message(
        self,
    ):
        self.module.plugin.config["pixiv_push"]["ranking"] = {
            "count": 1,
            "daily_push": {
                "enable": True,
                "groups": ["one:GroupMessage:10", "two:GroupMessage:20"],
            },
        }
        action = AsyncMock(side_effect=AssertionError("Unexpected login lookup"))
        clients = {
            key: SimpleNamespace(
                _wsr_api_clients={sender: object()}, call_action=action
            )
            for key, sender in (("one", "111"), ("two", "222"))
        }
        self.lookup.side_effect = lambda key: SimpleNamespace(
            get_client=lambda: clients[key]
        )
        send = AsyncMock(return_value=True)
        self.module.plugin.context.send_message = send
        self.module._browse_candidates = AsyncMock(return_value=[("7", 1)])
        self.module._fetch_detail = AsyncMock(
            return_value={
                "id": "7",
                "title": "Work",
                "author": "Artist",
                "page_count": 2,
                "x_restrict": 0,
                "original": "https://i.pximg.net/7.png",
                "regular": "",
            }
        )
        self.module._fetch_pages = AsyncMock(side_effect=lambda item: [item, item])
        self.module._download = AsyncMock(return_value=self.image)
        await self.module.push_daily_ranking()
        self.assertEqual(send.await_count, 2)
        for call, origin, sender in zip(
            send.await_args_list,
            ("one:GroupMessage:10", "two:GroupMessage:20"),
            ("111", "222"),
        ):
            self.assertEqual(call.args[0], origin)
            nodes = call.args[1].chain[0]
            self.assertIsInstance(nodes, Nodes)
            self.assertEqual(len(nodes.nodes), 1)
            self.assertEqual(len(page_nodes(nodes.nodes)), 2)
            self.assertTrue(all(node.uin == sender for node in page_nodes(nodes.nodes)))
        action.assert_not_awaited()

    async def test_background_reads_live_connection_after_account_changes(self):
        action = AsyncMock(side_effect=AssertionError("Unexpected login lookup"))
        connections = {"123": object()}
        client = SimpleNamespace(_wsr_api_clients=connections, call_action=action)
        self.lookup.return_value = SimpleNamespace(get_client=lambda: client)
        self.module._bot_ids["test"] = "999"
        self.assertEqual(await self.module._get_bot_id("test:GroupMessage:1"), "123")
        connections.clear()
        connections["456"] = object()
        self.assertEqual(await self.module._get_bot_id("test:GroupMessage:1"), "456")
        action.assert_not_awaited()

    async def test_disconnected_or_ambiguous_platform_never_uses_a_stale_account(self):
        action = AsyncMock(side_effect=AssertionError("Unexpected login lookup"))
        self.module._bot_ids["test"] = "999"
        for connections in ({}, {"123": object(), "456": object()}):
            with self.subTest(accounts=list(connections)):
                client = SimpleNamespace(
                    _wsr_api_clients=connections, call_action=action
                )
                self.lookup.return_value = SimpleNamespace(get_client=lambda: client)
                with self.assertRaises(RuntimeError):
                    await self.module._get_bot_id("test:GroupMessage:1")
        action.assert_not_awaited()

    async def test_background_lookup_timeout_cancels_the_api_without_caching(self):
        cancelled = asyncio.Event()

        async def no_response(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        action = AsyncMock(side_effect=no_response)
        self.lookup.return_value = SimpleNamespace(
            get_client=lambda: SimpleNamespace(call_action=action)
        )
        with patch.object(pixiv, "_LOGIN_INFO_TIMEOUT", 0.01):
            with self.assertRaisesRegex(RuntimeError, "login information timed out"):
                await self.module._get_bot_id("test:GroupMessage:1")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.module._bot_ids, {})
