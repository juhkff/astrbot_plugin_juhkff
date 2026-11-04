"""Standalone regressions using the real wake stage and message events.

Run separately from unittest discovery: importing main resets the module registry.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Plain, Reply
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.waking_check import stage as waking
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import star_handlers_registry

# The entrypoint must establish its import path and reload classes first.
# isort: off
from data.plugins.astrbot_plugin_juhkff import main as entry
from juhkff_modules.active_chat import ActiveChatModule
from juhkff_modules.daily_report import DailyReportModule
from juhkff_modules.help_gen import HelpGenModule
from juhkff_modules.pixiv_push import PixivPushModule
# isort: on


class CommandRoutingTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = entry.JuhkffPlugin.__new__(entry.JuhkffPlugin)
        self.plugin.config = {
            "global": {"enabled": True},
            "active_chat": {"enable": True, "default_chat_rate": 1},
            "daily_report": {"enable": True},
            "help": {"enable": True},
            "pixiv_push": {"enable": True},
        }
        self.core_config = deepcopy(DEFAULT_CONFIG)
        self.core_config["wake_prefix"] = ["/", "#", "!"]
        self.plugin.context = SimpleNamespace(get_config=lambda umo: self.core_config)
        self.plugin._started = True
        self.plugin.data_dir = "/tmp/juhkff-command-routing-test"
        self.chat = ActiveChatModule(self.plugin)
        self.chat._recent_history = AsyncMock(return_value=[])
        self.chat._append = AsyncMock()
        self.chat._generate = AsyncMock(return_value="chat reply")
        self.chat._should_speak = Mock(return_value=True)
        self.report = DailyReportModule(self.plugin)
        self.report._build_chain = AsyncMock(
            return_value=MessageChain().message("report")
        )
        self.help = HelpGenModule(self.plugin)
        self.help._reply = AsyncMock(return_value=MessageChain().message("help"))
        self.pixiv = PixivPushModule(self.plugin)

        async def pixiv_reply(event):
            return (
                MessageChain().message("pixiv")
                if self.pixiv.intercepts(event)
                else None
            )

        self.pixiv.handle = AsyncMock(side_effect=pixiv_reply)
        self.card = SimpleNamespace(
            key="share_parse",
            title="card parsing",
            enabled=True,
            intercepts=lambda e: False,
            handle=AsyncMock(return_value=None),
        )
        # Keep the original problematic registration order: routing must override it.
        self.plugin.modules = {
            m.key: m for m in (self.chat, self.card, self.report, self.pixiv, self.help)
        }
        self.stage = waking.WakingCheckStage()
        await self.stage.initialize(
            SimpleNamespace(
                astrbot_config=self.core_config,
                astrbot_config_id="test",
                db_helper=None,
            )
        )
        self.listener = next(
            h
            for h in star_handlers_registry
            if h.handler_name == "_dispatch" and "juhkff" in h.handler_module_path
        )
        self.status = next(
            h
            for h in star_handlers_registry
            if h.handler_name == "_cmd_status" and "juhkff" in h.handler_module_path
        )

    def event(self, text, addressed=None):
        obj = AstrBotMessage()
        obj.type = MessageType.GROUP_MESSAGE
        obj.self_id = "100"
        obj.group_id = "200"
        obj.session_id = "200"
        obj.message_id = "300"
        obj.sender = MessageMember("400", "tester")
        components = []
        if addressed == "at":
            components.append(At(qq="100"))
        elif addressed == "reply":
            components.append(Reply(id="old", sender_id="100"))
        elif addressed == "all":
            components.append(At(qq="all"))
        obj.message = components + [Plain(text)]
        obj.message_str = text
        obj.raw_message = {}
        event = AstrMessageEvent(
            text, obj, PlatformMetadata("test", "test", "test"), "200"
        )
        event.send = AsyncMock()
        return event

    async def wake(self, event, additional=()):
        handlers = [self.listener, self.status, *additional]
        with (
            patch.object(
                waking.star_handlers_registry,
                "get_handlers_by_event_type",
                return_value=handlers,
            ),
            patch.object(
                waking.SessionPluginManager,
                "filter_handlers_by_session",
                AsyncMock(side_effect=lambda e, hs: hs),
            ),
        ):
            await self.stage.process(event)

    async def dispatch(self, event):
        async for _ in self.plugin._dispatch(event):
            pass

    def assert_no_chat(self):
        self.chat._recent_history.assert_not_awaited()
        self.chat._append.assert_not_awaited()
        self.chat._generate.assert_not_awaited()

    async def test_report_commands_with_or_without_mentions_and_replies(self):
        for address in (None, "at", "reply"):
            for text in ("#日报", "/日报", "日报", "!日报"):
                with self.subTest(address=address, text=text):
                    event = self.event(text, address)
                    await self.wake(event)
                    await self.dispatch(event)
                    self.assertEqual(event.send.await_count, 1)
                    self.assertTrue(event.is_stopped())
                    self.assert_no_chat()
        self.card.handle.assert_not_awaited()

    async def test_removed_report_commands_do_not_reply_or_enter_chat(self):
        for address in (None, "at", "reply"):
            for prefix in ("#", "/", "!"):
                for suffix in (" 1", " 5", " 随机", " 纸张手帐", "样式"):
                    with self.subTest(address=address, prefix=prefix, suffix=suffix):
                        event = self.event(prefix + "日报" + suffix, address)
                        await self.wake(event)
                        self.assertFalse(self.report.intercepts(event))
                        self.assertIsNone(await self.report.handle(event))
                        await self.dispatch(event)
                        event.send.assert_not_awaited()
                        self.assert_no_chat()
        self.report._build_chain.assert_not_awaited()

    async def test_help_and_custom_help_after_prefix_removal(self):
        for command in ("帮助", "菜单"):
            self.plugin.config["help"]["command"] = command
            for prefix in ("#", "/", "!"):
                for address in (None, "at", "reply"):
                    with self.subTest(command=command, prefix=prefix, address=address):
                        event = self.event(prefix + command, address)
                        await self.wake(event)
                        self.assertEqual(event.message_str, command)
                        await self.dispatch(event)
                        self.assertEqual(event.send.await_count, 1)
                        self.assertTrue(event.is_stopped())
                        self.assert_no_chat()

    async def test_pixiv_accepts_the_framework_stripped_command(self):
        for command in ("pixiv订阅 7", "pixiv取消订阅 7", "pixiv订阅"):
            for prefix in ("#", "/", "!"):
                for address in (None, "at", "reply"):
                    with self.subTest(command=command, prefix=prefix, address=address):
                        event = self.event(prefix + command, address)
                        await self.wake(event)
                        await self.dispatch(event)
                        self.assertEqual(event.send.await_count, 1)
                        self.assertTrue(event.is_stopped())
                        self.assert_no_chat()

    async def test_removed_pixiv_ranking_commands_do_not_reply_or_chat(self):
        for command in ("/pixiv", "/pixiv 最新", "#pixiv"):
            for address in (None, "at", "reply"):
                event = self.event(command, address)
                await self.wake(event)
                self.assertFalse(self.pixiv.intercepts(event))
                await self.dispatch(event)
                event.send.assert_not_awaited()
                self.assert_no_chat()

    async def test_framework_status_command_is_left_to_registered_handler(self):
        for address in (None, "at", "reply"):
            event = self.event("/juhkff", address)
            await self.wake(event)
            self.assertTrue(event.get_extra("handlers_parsed_params"))
            await self.dispatch(event)
            self.assertFalse(event.is_stopped())
            event.send.assert_not_awaited()
            self.assert_no_chat()
            results = [result async for result in self.plugin._cmd_status(event)]
            self.assertEqual(len(results), 1)

    async def test_other_registered_command_without_arguments_wins(self):
        async def ping(self, event):
            pass

        metadata = SimpleNamespace(
            handler=ping,
            handler_name="ping",
            handler_full_name="test.ping",
            handler_module_path="test",
            event_filters=[],
        )
        metadata.event_filters = [CommandFilter("ping", handler_md=metadata)]
        event = self.event("/ping", "at")
        await self.wake(event, [metadata])
        self.assertEqual(event.get_extra("handlers_parsed_params")["test.ping"], {})
        await self.dispatch(event)
        self.assertFalse(event.is_stopped())
        event.send.assert_not_awaited()
        self.assert_no_chat()
        # The standalone active-chat guard must agree with dispatcher routing.
        await self.chat.handle(event)
        self.assert_no_chat()

    async def test_unknown_prefixed_commands_never_enter_chat_history(self):
        for prefix in ("/", "#", "!"):
            for address in ("at", "reply"):
                event = self.event(prefix + "unknown", address)
                await self.wake(event)
                await self.dispatch(event)
                self.assert_no_chat()
                event.send.assert_not_awaited()

    async def test_ordinary_conversation_still_runs_once(self):
        for address in (None, "at", "reply"):
            event = self.event("你好，今天怎么样", address)
            await self.wake(event)
            await self.dispatch(event)
            self.assertEqual(event.send.await_count, 1)
            self.assertTrue(event.is_stopped())
        self.assertEqual(self.chat._generate.await_count, 3)
        self.assertEqual(self.chat._append.await_count, 6)
        self.assertEqual(self.card.handle.await_count, 3)

    async def test_plain_help_word_is_not_a_command_without_wakeup(self):
        event = self.event("帮助")
        await self.wake(event)
        await self.dispatch(event)
        self.help._reply.assert_not_awaited()
        self.chat._generate.assert_awaited_once()

    async def test_other_module_response_prevents_active_chat(self):
        self.card.handle.return_value = MessageChain().message("card parsed")
        event = self.event("普通分享消息", "at")
        await self.wake(event)
        await self.dispatch(event)
        self.assertEqual(event.send.await_count, 1)
        self.assertTrue(event.is_stopped())
        self.chat._recent_history.assert_awaited_once()
        self.chat._append.assert_awaited_once()
        self.chat._generate.assert_not_awaited()

    async def test_command_failure_and_send_failure_do_not_fall_back_to_chat(self):
        self.report._build_chain.side_effect = RuntimeError(
            "expected mocked command failure"
        )
        event = self.event("#日报", "at")
        await self.wake(event)
        with patch.object(entry.logger, "exception"):
            await self.dispatch(event)
        self.assertTrue(event.is_stopped())
        self.assert_no_chat()
        event = self.event("#帮助", "reply")
        event.send.side_effect = RuntimeError("expected mocked send failure")
        await self.wake(event)
        with patch.object(entry.logger, "exception"):
            await self.dispatch(event)
        self.assertTrue(event.is_stopped())
        self.assert_no_chat()

    async def test_already_stopped_and_global_disabled_are_ignored(self):
        event = self.event("你好", "at")
        event.stop_event()
        await self.dispatch(event)
        self.plugin.config["global"]["enabled"] = False
        await self.dispatch(self.event("你好", "at"))
        self.assert_no_chat()
        self.card.handle.assert_not_awaited()

    async def test_observe_is_idempotent_and_keeps_pre_message_snapshot(self):
        history = [{"name": "earlier", "text": "old message", "ts": 1.0}]
        self.chat._recent_history.return_value = history
        event = self.event("current message")
        self.assertIs(await self.chat.observe(event), history)
        self.assertIs(await self.chat.observe(event), history)
        self.chat._recent_history.assert_awaited_once()
        self.chat._append.assert_awaited_once()
        self.assertIn("current message", self.chat._append.await_args.args[1]["text"])
        await self.chat.handle(event)
        self.assertIs(self.chat._generate.await_args.args[6], history)
        self.assertEqual(self.chat._recent_history.await_count, 1)
        self.assertEqual(self.chat._append.await_count, 2)
        self.assertEqual(
            history, [{"name": "earlier", "text": "old message", "ts": 1.0}]
        )

    async def test_emoji_response_records_before_reply_without_llm(self):
        async def reply(event):
            self.chat._recent_history.assert_awaited_once()
            self.chat._append.assert_awaited_once()
            return MessageChain().message("emoji response")

        emoji = SimpleNamespace(
            key="emoji_steal",
            enabled=True,
            intercepts=lambda event: False,
            handle=AsyncMock(side_effect=reply),
        )
        self.plugin.modules = {"active_chat": self.chat, "emoji_steal": emoji}
        event = self.event("ordinary group image")
        await self.dispatch(event)
        self.assertTrue(event.is_stopped())
        event.send.assert_awaited_once()
        self.chat._generate.assert_not_awaited()
        await self.dispatch(event)
        self.chat._append.assert_awaited_once()

    async def test_disabled_chat_leaves_automatic_responder_working(self):
        self.plugin.config["active_chat"]["enable"] = False
        self.card.handle.return_value = MessageChain().message("card response")
        event = self.event("ordinary message")
        await self.dispatch(event)
        event.send.assert_awaited_once()
        self.assert_no_chat()

    async def test_observation_failure_does_not_suppress_card_reply(self):
        self.chat.observe = AsyncMock(side_effect=RuntimeError("observation failed"))
        self.card.handle.return_value = MessageChain().message("card response")
        event = self.event("ordinary message")
        with patch.object(entry.logger, "exception"):
            await self.dispatch(event)
        event.send.assert_awaited_once()
        self.chat._generate.assert_not_awaited()

    async def test_terminate_closes_bound_renderer_after_module_failure(self):
        failing = SimpleNamespace(
            key="failed", stop=AsyncMock(side_effect=RuntimeError("stop failed"))
        )
        other = SimpleNamespace(key="other", stop=AsyncMock())
        self.plugin.modules = {"failed": failing, "other": other}
        self.plugin.scheduler = SimpleNamespace(shutdown=AsyncMock())
        with (
            patch.object(entry.html_renderer, "close", AsyncMock()) as close,
            patch.object(entry.logger, "exception"),
        ):
            await self.plugin.terminate()
            close.assert_awaited_once()
        other.stop.assert_awaited_once()
        self.plugin.scheduler.shutdown.assert_awaited_once()

    async def test_terminate_cancellation_still_releases_jobs_and_renderer(self):
        module = SimpleNamespace(stop=AsyncMock(side_effect=asyncio.CancelledError))
        self.plugin.modules = {"cancelled": module}
        self.plugin.scheduler = SimpleNamespace(shutdown=AsyncMock())
        with patch.object(entry.html_renderer, "close", AsyncMock()) as close:
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin.terminate()
            self.plugin.scheduler.shutdown.assert_awaited_once()
            close.assert_awaited_once()

    async def test_terminate_closes_renderer_even_when_scheduler_fails(self):
        self.plugin.modules = {}
        self.plugin.scheduler = SimpleNamespace(
            shutdown=AsyncMock(side_effect=RuntimeError("scheduler failed"))
        )
        with patch.object(entry.html_renderer, "close", AsyncMock()) as close:
            with self.assertRaisesRegex(RuntimeError, "scheduler failed"):
                await self.plugin.terminate()
            close.assert_awaited_once()


if __name__ == "__main__":
    main(verbosity=2)
