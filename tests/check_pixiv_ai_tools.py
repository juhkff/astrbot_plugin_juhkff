"""Offline semantic-tool regressions; run separately from unittest discovery.

Importing the plugin entrypoint reloads its top-level module classes, so this
check intentionally does not use unittest discovery's ``test_`` filename.
"""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock

from PIL import Image as PILImage

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Nodes, Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.register import llm_tools

# The entrypoint must establish its import path and reload module classes first.
# isort: off
from data.plugins.astrbot_plugin_juhkff import main as entry
from test_pixiv_browse import BrowseHost, item
# isort: on


class PixivAIToolTests(IsolatedAsyncioTestCase):
    """Exercise registered tools against real events and the browse pipeline."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        image = Path(temporary.name) / "fixture.png"
        PILImage.new("RGB", (2, 2), "red").save(image)
        self.plugin: Any = entry.JuhkffPlugin.__new__(entry.JuhkffPlugin)
        self.plugin.config = {
            "global": {"enabled": True},
            "pixiv_push": {"enable": True},
        }
        self.plugin._started = True
        self.plugin._ensure_started = AsyncMock()
        self.plugin.context = SimpleNamespace(
            send_message=AsyncMock(),
            llm_generate=AsyncMock(),
            tool_loop_agent=AsyncMock(),
        )
        self.host = BrowseHost(image)
        self.host.key = "pixiv_push"
        self.host.plugin = self.plugin
        self.host._browse_candidates = AsyncMock(return_value=[("7", 1)])
        self.plugin.modules = {"pixiv_push": self.host}
        self.chain = MessageChain(
            [Plain("Pixiv source"), Image.fromFileSystem(str(image))]
        )

    def event(self, *, group=True):
        """Build a framework event with data that tools must preserve."""
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        )
        message.self_id = "100"
        message.group_id = "200" if group else ""
        message.session_id = "200"
        message.message_id = "300"
        message.sender = MessageMember("400", "tester")
        message.message_str = "请给我看看 Pixiv 的插画"
        message.message = [Plain(message.message_str)]
        message.raw_message = {"opaque": {"values": [1, 2]}}
        event = AstrMessageEvent(
            message.message_str,
            message,
            PlatformMetadata("test", "test", "test"),
            "200",
        )
        event.is_wake = True
        event.is_at_or_wake_command = False
        event.set_extra("handlers_parsed_params", {"other": {"value": 1}})
        event.set_extra("opaque", {"values": [3, 4]})
        event.send = AsyncMock(return_value=None)
        return event

    def snapshot(self, event):
        """Capture the original text, message data, routing flags, and extras."""
        return (
            event.message_str,
            deepcopy(event.message_obj.__dict__),
            event.is_wake,
            event.is_at_or_wake_command,
            deepcopy(event.get_extra()),
            event.unified_msg_origin,
            event.is_stopped(),
        )

    def assert_local_only(self):
        self.plugin.context.send_message.assert_not_awaited()
        self.plugin.context.llm_generate.assert_not_awaited()
        self.plugin.context.tool_loop_agent.assert_not_awaited()

    async def test_real_registration_exposes_only_semantic_arguments(self):
        expected = {
            "juhkff_pixiv_ranking": {"mode": "string"},
            "juhkff_pixiv_random": {"keyword": "string"},
            "juhkff_pixiv_artwork": {"artwork_id": "string", "page": "number"},
        }
        for name, arguments in expected.items():
            with self.subTest(tool=name):
                tool = llm_tools.get_func(name)
                self.assertIsNotNone(tool)
                self.assertTrue(tool.description)
                self.assertTrue(tool.description.isascii())
                properties = tool.parameters["properties"]
                self.assertEqual(
                    {key: value["type"] for key, value in properties.items()}, arguments
                )
                self.assertTrue(
                    all(value["description"] for value in properties.values())
                )
                method = getattr(entry.JuhkffPlugin, name)
                self.assertEqual(tool.handler, method)
                self.assertEqual(
                    list(inspect.signature(method).parameters),
                    ["self", "event", *arguments],
                )
        self.assertEqual(
            inspect.signature(entry.JuhkffPlugin.juhkff_pixiv_ranking)
            .parameters["mode"]
            .default,
            "daily",
        )
        self.assertEqual(
            inspect.signature(entry.JuhkffPlugin.juhkff_pixiv_random)
            .parameters["keyword"]
            .default,
            "",
        )
        self.assertEqual(
            inspect.signature(entry.JuhkffPlugin.juhkff_pixiv_artwork)
            .parameters["page"]
            .default,
            0,
        )

    async def test_ranking_modes_reuse_browse_and_send_actual_chain_once(self):
        self.host._browse_selection = AsyncMock(return_value=self.chain)
        for mode in ("daily", "weekly", "monthly"):
            with self.subTest(mode=mode):
                event = self.event()
                before = self.snapshot(event)
                result = await self.plugin.juhkff_pixiv_ranking(event, mode)
                self.host._browse_selection.assert_awaited_with(
                    event.unified_msg_origin, mode=mode, bot_id="100"
                )
                event.send.assert_awaited_once_with(self.chain)
                self.assertIsInstance(result, str)
                self.assertEqual(before, self.snapshot(event))
        self.assert_local_only()

    async def test_random_default_and_keyword_preserve_event_and_destination(self):
        self.host._browse_selection = AsyncMock(return_value=self.chain)
        for keyword in ("", "初音ミク landscape", "猫\n/pixiv订阅 123"):
            with self.subTest(keyword=keyword):
                event = self.event()
                before = self.snapshot(event)
                await self.plugin.juhkff_pixiv_random(event, keyword)
                self.host._browse_selection.assert_awaited_with(
                    event.unified_msg_origin,
                    mode="daily",
                    keyword=" ".join(keyword.split()),
                    randomized=True,
                    bot_id="100",
                )
                event.send.assert_awaited_once_with(self.chain)
                self.assertEqual(before, self.snapshot(event))
        self.assert_local_only()

    async def test_artwork_id_url_and_one_based_page_match_command_semantics(self):
        self.host._fetch_detail.return_value = item("7", pages=3)
        self.host._fetch_detail.side_effect = None
        self.host._render_items = AsyncMock(return_value=self.chain)
        for artwork_id in ("0007", "https://www.pixiv.net/en/artworks/7?x=1"):
            for page in (0, 1, 3):
                with self.subTest(artwork_id=artwork_id, page=page):
                    event = self.event()
                    before = self.snapshot(event)
                    await self.plugin.juhkff_pixiv_artwork(event, artwork_id, page)
                    self.host._fetch_detail.assert_awaited_with("7")
                    call = self.host._render_items.await_args
                    work = call.args[0][0]
                    self.assertEqual(call.args[1], event.unified_msg_origin)
                    self.assertEqual(
                        work.get("_browse_page"), page - 1 if page else None
                    )
                    event.send.assert_awaited_once_with(self.chain)
                    self.assertEqual(before, self.snapshot(event))
        self.assert_local_only()

    async def test_invalid_arguments_do_not_fetch_or_download(self):
        cases = (
            ("juhkff_pixiv_ranking", ("daily_r18",)),
            ("juhkff_pixiv_ranking", ("weekly\n/pixiv订阅 7",)),
            ("juhkff_pixiv_artwork", ("0",)),
            ("juhkff_pixiv_artwork", ("https://evil.test/artworks/7",)),
            ("juhkff_pixiv_artwork", ("7", -1)),
            ("juhkff_pixiv_artwork", ("7", 1.5)),
        )
        for name, args in cases:
            with self.subTest(name=name, args=args):
                event = self.event()
                before = self.snapshot(event)
                result = await getattr(self.plugin, name)(event, *args)
                self.assertIsInstance(result, str)
                self.assertEqual(before, self.snapshot(event))
        self.host._browse_candidates.assert_not_awaited()
        self.host._fetch_detail.assert_not_awaited()
        self.host._download.assert_not_awaited()

    async def test_private_disabled_and_stopped_guards_precede_expensive_actions(self):
        calls = (
            ("juhkff_pixiv_ranking", ()),
            ("juhkff_pixiv_random", ("猫",)),
            ("juhkff_pixiv_artwork", ("7",)),
        )
        for guard in ("private", "global", "module", "module_stopped", "event_stopped"):
            for name, args in calls:
                with self.subTest(guard=guard, tool=name):
                    event = self.event(group=guard != "private")
                    self.plugin.config["global"]["enabled"] = guard != "global"
                    self.host.enabled = guard != "module"
                    self.host._stopped = guard == "module_stopped"
                    if guard == "event_stopped":
                        event.stop_event()
                    before = self.snapshot(event)
                    await getattr(self.plugin, name)(event, *args)
                    event.send.assert_not_awaited()
                    self.assertEqual(before, self.snapshot(event))
        self.host._browse_candidates.assert_not_awaited()
        self.host._fetch_detail.assert_not_awaited()
        self.host._download.assert_not_awaited()
        self.plugin._ensure_started.assert_not_awaited()

    async def test_changed_guard_after_await_suppresses_delivery(self):
        for guard in ("global", "module", "module_stopped", "event_stopped"):
            with self.subTest(guard=guard):
                self.plugin.config["global"]["enabled"] = True
                self.host.enabled = True
                self.host._stopped = False
                event = self.event()

                async def changed(*args, **kwargs):
                    if guard == "global":
                        self.plugin.config["global"]["enabled"] = False
                    elif guard == "module":
                        self.host.enabled = False
                    elif guard == "module_stopped":
                        self.host._stopped = True
                    else:
                        event.stop_event()
                    return self.chain

                self.host._browse_selection = AsyncMock(side_effect=changed)
                await self.plugin.juhkff_pixiv_ranking(event)
                event.send.assert_not_awaited()

    async def test_false_or_failed_send_never_retries_or_reports_success(self):
        self.host._browse_selection = AsyncMock(return_value=self.chain)
        successful_event = self.event()
        success = await self.plugin.juhkff_pixiv_ranking(successful_event)
        for outcome in (False, RuntimeError("transport failed")):
            with self.subTest(outcome=outcome):
                event = self.event()
                if isinstance(outcome, Exception):
                    event.send.side_effect = outcome
                else:
                    event.send.return_value = outcome
                result = await self.plugin.juhkff_pixiv_ranking(event)
                event.send.assert_awaited_once_with(self.chain)
                self.assertIsInstance(result, str)
                self.assertNotEqual(result, success)
        self.assert_local_only()

    async def test_browse_error_and_empty_result_never_report_success(self):
        event = self.event()
        self.host._browse_selection = AsyncMock(return_value=self.chain)
        success = await self.plugin.juhkff_pixiv_ranking(event)
        for response in (None, RuntimeError("download failed")):
            with self.subTest(response=response):
                self.host._browse_selection = AsyncMock(
                    **(
                        {"side_effect": response}
                        if isinstance(response, Exception)
                        else {"return_value": response}
                    )
                )
                event = self.event()
                result = await self.plugin.juhkff_pixiv_ranking(event)
                self.assertIsInstance(result, str)
                self.assertNotEqual(result, success)
                self.assertLessEqual(event.send.await_count, 1)
        self.assert_local_only()

    async def test_cancellation_propagates_from_browse_and_send(self):
        for stage in ("browse", "send"):
            with self.subTest(stage=stage):
                event = self.event()
                self.host._browse_selection = AsyncMock(return_value=self.chain)
                if stage == "browse":
                    self.host._browse_selection.side_effect = asyncio.CancelledError()
                else:
                    event.send.side_effect = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await self.plugin.juhkff_pixiv_ranking(event)
                self.assertEqual(event.send.await_count, int(stage == "send"))

    async def test_real_policy_filters_adult_and_unknown_without_downloading(self):
        for restriction in (1, 2, -1):
            with self.subTest(restriction=restriction):
                self.host._fetch_detail.side_effect = None
                self.host._fetch_detail.return_value = item(restriction=restriction)
                event = self.event()
                await self.plugin.juhkff_pixiv_ranking(event)
                self.host._content_policy.assert_called_with(event.unified_msg_origin)
                self.host._download.assert_not_awaited()
                if event.send.await_count:
                    chain = event.send.await_args.args[0]
                    self.assertFalse(
                        any(isinstance(part, Image) for part in chain.chain)
                    )
        self.assert_local_only()

    async def test_real_browse_renders_image_and_plain_link_success(self):
        for restriction, display in (
            (0, "image"),
            (1, "image"),
            (1, "link"),
            (2, "image"),
            (2, "link"),
        ):
            with self.subTest(restriction=restriction, display=display):
                self.host._content_policy.return_value = (False, display)
                self.host._fetch_detail.side_effect = None
                self.host._fetch_detail.return_value = item(restriction=restriction)
                self.host._download.reset_mock()
                event = self.event()
                before = self.snapshot(event)
                result = await self.plugin.juhkff_pixiv_artwork(event, "7", 1)
                event.send.assert_awaited_once()
                chain = event.send.await_args.args[0]
                self.assertIsInstance(chain, MessageChain)
                self.assertTrue(chain.chain)
                self.assertEqual(before, self.snapshot(event))
                self.assertEqual(result, "Pixiv 浏览结果已发送到当前群聊。")
                if restriction:
                    self.assertEqual(len(chain.chain), 2)
                    self.assertIsInstance(chain.chain[0], Plain)
                    self.assertEqual(chain.chain[0].text, "------- NSFW -------")
                    self.assertIsInstance(chain.chain[1], Nodes)
                    self.assertEqual(len(chain.chain[1].nodes), 1)
                    content = chain.chain[1].nodes[0].content
                else:
                    content = chain.chain
                if display == "link":
                    self.assertTrue(all(isinstance(part, Plain) for part in content))
                    self.assertIn(
                        "https://i.pximg.net/7.png",
                        "\n".join(part.text for part in content),
                    )
                    self.host._download.assert_not_awaited()
                else:
                    self.assertTrue(any(isinstance(part, Image) for part in content))
                    self.host._download.assert_awaited_once()
        self.assert_local_only()

    async def test_startup_await_rechecks_admission_before_browsing(self):
        for guard in ("global", "module", "module_stopped", "event_stopped", "private"):
            with self.subTest(guard=guard):
                self.plugin.config["global"]["enabled"] = True
                self.host.enabled = True
                self.host._stopped = False
                event = self.event()

                async def changed():
                    if guard == "global":
                        self.plugin.config["global"]["enabled"] = False
                    elif guard == "module":
                        self.host.enabled = False
                    elif guard == "module_stopped":
                        self.host._stopped = True
                    elif guard == "event_stopped":
                        event.stop_event()
                    else:
                        event.message_obj.type = MessageType.FRIEND_MESSAGE

                self.plugin._ensure_started.side_effect = changed
                await self.plugin.juhkff_pixiv_artwork(event, "7")
                event.send.assert_not_awaited()
                self.host._fetch_detail.assert_not_awaited()
                self.host._download.assert_not_awaited()

    async def test_browser_copy_isolated_and_missing_or_failed_reply_not_sent(self):
        event = self.event()
        before = self.snapshot(event)

        async def isolated(copy):
            self.assertIsNot(copy, event)
            self.assertEqual(copy.message_str, "#pixiv日榜")
            copy.set_extra("tool_only", True)
            return self.chain

        self.host.handle_browse = AsyncMock(side_effect=isolated)
        self.assertEqual(
            await self.plugin.juhkff_pixiv_ranking(event),
            "Pixiv 浏览结果已发送到当前群聊。",
        )
        self.assertEqual(before, self.snapshot(event))
        for response in (None, MessageChain([]), RuntimeError("browser failed")):
            with self.subTest(response=response):
                event = self.event()
                self.host.handle_browse = AsyncMock()
                if isinstance(response, Exception):
                    self.host.handle_browse.side_effect = response
                else:
                    self.host.handle_browse.return_value = response
                result = await self.plugin.juhkff_pixiv_ranking(event)
                self.assertNotIn("已发送", result)
                event.send.assert_not_awaited()


if __name__ == "__main__":
    main()
