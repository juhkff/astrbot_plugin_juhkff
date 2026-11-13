"""Offline group admission tests using real AstrBot message events and requests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from itertools import count
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, patch

from juhkff_modules import group_chat

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.db.po import Conversation
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.platform_metadata import PlatformMetadata


class MemoryGroupChatStore:
    """Exercise module behavior without a Redis server."""

    def __init__(self, *args):
        self.records = {}
        self.closed = False

    async def start(self):
        pass

    async def snapshot_append(self, umo, record_id, tick, text, limit):
        rows = list(self.records.get(umo, []))
        snapshot = [row[2] for row in rows[-limit:]]
        rows.append([record_id, tick, text])
        self.records[umo] = rows[-limit:]
        return snapshot

    async def replace(self, umo, record_id, text):
        for row in self.records.get(umo, []):
            if row[0] == record_id:
                row[2] = text
                break

    async def clear(self, umo):
        self.records.pop(umo, None)

    async def close(self):
        self.closed = True


class GroupChatTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 1000.0
        patcher = patch.object(
            group_chat,
            "time",
            SimpleNamespace(
                monotonic=lambda: self.clock,
                time=lambda: self.clock,
                strftime=lambda pattern: "00:00:00",
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ids = count(1)
        self.core_config = deepcopy(DEFAULT_CONFIG)
        self.core_config["provider_ltm_settings"]["image_caption"] = False
        self.expected_core_config = deepcopy(self.core_config)
        self.conversation = Conversation(
            platform_id="adapter-1", user_id="group-a", cid="conversation-1"
        )
        self.conversation.cid = "conversation-1"
        self.conversation.history = "[]"
        self.conversation.persona_id = "existing-persona"
        self.manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value=self.conversation.cid),
            new_conversation=AsyncMock(return_value=self.conversation.cid),
            get_conversation=AsyncMock(return_value=self.conversation),
        )
        self.plugin = SimpleNamespace(
            global_enabled=True,
            config={
                "global": {"debug": False},
                "group_chat": {
                    "enable": True,
                    "groups": [],
                    "probability": 0,
                    "context_messages": 3,
                },
            },
            context=SimpleNamespace(
                get_config=lambda **kwargs: self.core_config,
                conversation_manager=self.manager,
            ),
            modules={},
        )
        store_patch = patch.object(
            group_chat, "RedisGroupChatStore", MemoryGroupChatStore
        )
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.module = group_chat.GroupChatModule(self.plugin)
        self.plugin.modules[self.module.key] = self.module

    def event(
        self,
        text="hello",
        *,
        group="group-a",
        addressed=False,
        components=None,
        sender="user-1",
        platform="aiocqhttp",
        message_type=MessageType.GROUP_MESSAGE,
    ):
        message = AstrBotMessage()
        message.type = message_type
        message.self_id = "bot-1"
        message.session_id = group
        message.group_id = group if message_type == MessageType.GROUP_MESSAGE else None
        message.message_id = str(next(self.ids))
        message.sender = MessageMember(user_id=sender, nickname="Tester")
        message.message_str = text
        message.message = components if components is not None else [Plain(text)]
        message.raw_message = {}
        event = AstrMessageEvent(
            text,
            message,
            PlatformMetadata(name=platform, description="offline", id="adapter-1"),
            group,
        )
        event.is_at_or_wake_command = addressed
        return event

    @staticmethod
    def handler(name, module="astrbot.builtin_stars.astrbot.main"):
        return SimpleNamespace(handler_name=name, handler_module_path=module)

    async def test_removal_mutates_current_iteration_and_preserves_unrelated_handlers(
        self,
    ):
        event = self.event()
        own = self.handler("prepare", "plugin")
        other = self.handler("on_message", "another.plugin")
        persisted = self.handler("persist_group_message")
        tail = self.handler("last", "plugin")
        handlers = [
            own,
            self.handler("handle_empty_mention"),
            other,
            self.handler("on_message"),
            persisted,
            tail,
        ]
        event.set_extra("activated_handlers", handlers)
        visited = []
        # Model the framework's already-running `for handler in activated_handlers`.
        for handler in handlers:
            visited.append(handler)
            if handler is own:
                await self.module.prepare(event)
        self.assertIs(event.get_extra("activated_handlers"), handlers)
        self.assertEqual(visited, [own, other, persisted, tail])
        self.assertEqual(handlers, visited)

    async def test_parameterless_official_command_has_priority_over_probability_and_recording(
        self,
    ):
        self.module.cfg["probability"] = 1
        event = self.event("reset", addressed=True)
        event.set_extra("handlers_parsed_params", {"builtin.reset": {}})
        await self.module.prepare(event)
        self.assertIsNone(await self.module.admit(event))
        self.assertFalse(event.get_extra("juhkff_group_owned", False))
        self.assertFalse(event.call_llm)
        self.assertFalse(self.module.store.records)
        self.manager.get_curr_conversation_id.assert_not_awaited()

    async def test_plugin_intercept_prevents_passive_context_and_reply(self):
        self.plugin.modules["command"] = SimpleNamespace(
            enabled=True, intercepts=lambda event: True
        )
        event = self.event("#help", addressed=True)
        await self.module.prepare(event)
        self.assertIsNone(await self.module.admit(event))
        self.assertFalse(self.module.store.records)
        self.assertFalse(event.get_extra("juhkff_group_owned", False))

    async def test_bare_self_mention_with_whitespace_binds_existing_conversation(self):
        event = self.event(
            " \t\n",
            addressed=True,
            components=[Plain(" \t"), At(qq="bot-1"), Plain("\n")],
        )
        await self.module.prepare(event)
        req = await self.module.admit(event)
        self.assertIsInstance(req, ProviderRequest)
        self.assertIs(req.conversation, self.conversation)
        self.assertEqual(req.contexts, [])
        self.assertTrue(
            event.call_llm
        )  # Suppress the default fallback, not this request.
        self.assertFalse(event.is_stopped())
        self.assertTrue(req.prompt.strip())
        self.manager.get_curr_conversation_id.assert_awaited_once_with(
            event.unified_msg_origin
        )
        self.manager.new_conversation.assert_not_awaited()
        self.manager.get_conversation.assert_awaited_once_with(
            event.unified_msg_origin, self.conversation.cid
        )

    async def test_bare_mention_creates_missing_conversation(self):
        self.manager.get_curr_conversation_id.return_value = None
        event = self.event("", addressed=True, components=[At(qq="bot-1")])
        await self.module.prepare(event)
        req = await self.module.admit(event)
        self.assertIs(req.conversation, self.conversation)
        self.manager.new_conversation.assert_awaited_once_with(
            event.unified_msg_origin, platform_id=event.get_platform_id()
        )

    async def test_normal_addressed_text_keeps_official_default_request(self):
        event = self.event(
            "specific question",
            addressed=True,
            components=[At(qq="bot-1"), Plain("specific question")],
        )
        await self.module.prepare(event)
        self.assertIsNone(await self.module.admit(event))
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(event.call_llm)
        self.assertFalse(event.is_stopped())
        self.manager.get_curr_conversation_id.assert_not_awaited()

    async def test_probability_zero_never_admits_even_when_rng_is_zero(self):
        event = self.event()
        await self.module.prepare(event)
        with patch.object(group_chat.random, "random", return_value=0):
            self.assertIsNone(await self.module.admit(event))
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(event.call_llm)

    async def test_probability_one_admits_consecutive_messages_without_time_advancing(
        self,
    ):
        self.module.cfg["probability"] = 1
        with patch.object(group_chat.random, "random", return_value=0.99999) as rng:
            for text in ("first", "immediate follow-up"):
                event = self.event(text)
                await self.module.prepare(event)
                self.assertIsNone(await self.module.admit(event))
                self.assertTrue(event.is_at_or_wake_command)
                self.assertFalse(event.call_llm)
            self.assertEqual(rng.call_count, 2)

    async def test_legacy_cooldown_config_does_not_throttle_messages(self):
        self.module.cfg.update(probability=1, cooldown=10**12)
        with patch.object(group_chat.random, "random", return_value=0.5) as rng:
            for text in ("first", "second", "third"):
                event = self.event(text)
                await self.module.prepare(event)
                await self.module.admit(event)
                self.assertTrue(event.is_at_or_wake_command)
            self.assertEqual(rng.call_count, 3)

    async def test_consecutive_messages_sample_probability_independently(self):
        self.module.cfg["probability"] = 0.2
        with patch.object(
            group_chat.random, "random", side_effect=[0.1, 0.9, 0.05]
        ) as rng:
            admitted = []
            for text in ("first", "second", "third"):
                event = self.event(text)
                await self.module.prepare(event)
                await self.module.admit(event)
                admitted.append(event.is_at_or_wake_command)
            self.assertEqual(admitted, [True, False, True])
            self.assertEqual(rng.call_count, 3)

    async def test_bare_mention_bypasses_zero_probability(self):
        prior = self.event()
        await self.module.prepare(prior)
        mention = self.event(
            " ", addressed=True, components=[At(qq="bot-1"), Plain(" ")]
        )
        await self.module.prepare(mention)
        req = await self.module.admit(mention)
        self.assertIs(req.conversation, self.conversation)

    async def test_other_plugins_consumed_events_never_admit(self):
        self.module.cfg["probability"] = 1
        for consumption in ("stopped", "sent", "provider_request"):
            with self.subTest(consumption=consumption):
                event = self.event()
                await self.module.prepare(event)
                if consumption == "stopped":
                    event.stop_event()
                elif consumption == "sent":
                    event._has_send_oper = True
                else:
                    event.set_extra("provider_request", ProviderRequest(prompt="other"))
                self.assertIsNone(await self.module.admit(event))
                self.assertFalse(event.is_at_or_wake_command)
                self.assertFalse(event.call_llm)

    async def test_snapshot_excludes_current_and_future_and_is_not_consumed(self):
        first = self.event("PAST_TOKEN")
        current = self.event("CURRENT_TOKEN", addressed=True)
        future = self.event("FUTURE_TOKEN")
        for event in (first, current, future):
            await self.module.prepare(event)
        before = deepcopy(self.module.store.records[current.unified_msg_origin])
        req = ProviderRequest(prompt="CURRENT_TOKEN", conversation=self.conversation)
        original_part = TextPart(text="existing context")
        req.extra_user_content_parts.append(original_part)
        await self.module.decorate(current, req)
        await self.module.decorate(current, req)
        self.assertEqual(req.extra_user_content_parts, [original_part])
        self.assertEqual(len(req.contexts), 1)
        message = req.contexts[0]
        self.assertEqual(message["role"], "user")
        self.assertTrue(message["_no_save"])
        self.assertIn("PAST_TOKEN", message["content"])
        self.assertNotIn("CURRENT_TOKEN", message["content"])
        self.assertNotIn("FUTURE_TOKEN", message["content"])
        self.assertIs(req.conversation, self.conversation)
        self.assertEqual(before, self.module.store.records[current.unified_msg_origin])
        second = ProviderRequest(prompt="retry")
        await self.module.decorate(current, second)
        self.assertEqual(second.contexts, req.contexts)

    async def test_snapshot_is_taken_before_awaiting_formatter(self):
        prior = self.event("HISTORY_TOKEN")
        await self.module.prepare(prior)
        entered, release = asyncio.Event(), asyncio.Event()
        original_format = self.module.formatter._format_message

        async def delayed_format(event, cfg):
            if event.message_str == "CURRENT_TOKEN":
                entered.set()
                await release.wait()
            return await original_format(event, cfg)

        with patch.object(self.module.formatter, "_format_message", delayed_format):
            current = self.event("CURRENT_TOKEN", addressed=True)
            task = asyncio.create_task(self.module.prepare(current))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await self.module.prepare(self.event("FUTURE_TOKEN"))
            finally:
                release.set()
                await task
        req = ProviderRequest(prompt="current")
        await self.module.decorate(current, req)
        self.assertIn(
            "HISTORY_TOKEN", "\n".join(item["content"] for item in req.contexts)
        )
        self.assertNotIn(
            "CURRENT_TOKEN", "\n".join(item["content"] for item in req.contexts)
        )
        self.assertNotIn(
            "FUTURE_TOKEN", "\n".join(item["content"] for item in req.contexts)
        )

    async def test_context_rolls_to_configured_bound(self):
        self.module.cfg["context_messages"] = 2
        for token in ("OLDEST", "OLDER", "NEWEST", "CURRENT"):
            event = self.event(token)
            await self.module.prepare(event)
        req = ProviderRequest(prompt="CURRENT")
        await self.module.decorate(event, req)
        text = "\n".join(item["content"] for item in req.contexts)
        self.assertNotIn("OLDEST", text)
        self.assertIn("OLDER", text)
        self.assertIn("NEWEST", text)
        self.assertNotIn("CURRENT", text)
        self.assertEqual(len(self.module.store.records[event.unified_msg_origin]), 2)

    async def test_groups_are_isolated_for_context(self):
        self.module.cfg["probability"] = 1
        a = self.event("PRIVATE_GROUP_A", group="a")
        b = self.event("PRIVATE_GROUP_B", group="b")
        await self.module.prepare(a)
        await self.module.admit(a)
        await self.module.prepare(b)
        await self.module.admit(b)
        self.assertTrue(a.is_at_or_wake_command)
        self.assertTrue(b.is_at_or_wake_command)
        current = self.event("question", group="b", addressed=True)
        await self.module.prepare(current)
        req = ProviderRequest(prompt="question")
        await self.module.decorate(current, req)
        self.assertIn(
            "PRIVATE_GROUP_B", "\n".join(item["content"] for item in req.contexts)
        )
        self.assertNotIn(
            "PRIVATE_GROUP_A", "\n".join(item["content"] for item in req.contexts)
        )

    async def test_old_context_remains_usable_and_legacy_ttl_is_ignored(self):
        self.module.cfg["context_ttl"] = 1
        await self.module.prepare(self.event("RETAINED_TOKEN"))
        self.clock += 10 * 365 * 24 * 3600
        current = self.event("new turn", addressed=True)
        await self.module.prepare(current)
        req = ProviderRequest(prompt="new turn")
        await self.module.decorate(current, req)
        self.assertIn(
            "RETAINED_TOKEN", "\n".join(item["content"] for item in req.contexts)
        )
        self.assertEqual(len(self.module.store.records[current.unified_msg_origin]), 2)

    async def test_reset_clears_only_target_group_after_success(self):
        a, b = self.event(group="a"), self.event(group="b")
        await self.module.prepare(a)
        await self.module.prepare(b)
        await self.module.after_sent(a)
        self.assertEqual(len(self.module.store.records), 2)
        a.set_extra("_clean_group_context_session", True)
        await self.module.after_sent(a)
        self.assertNotIn(a.unified_msg_origin, self.module.store.records)
        self.assertIn(b.unified_msg_origin, self.module.store.records)
        stored = deepcopy(self.module.store.records)
        await self.module.stop()
        self.assertEqual(self.module.store.records, stored)
        self.assertTrue(self.module.store.closed)

    async def test_generated_reply_is_recorded_only_after_delivery_and_once(self):
        event = self.event("hello", addressed=True)
        await self.module.prepare(event)
        before = deepcopy(self.module.store.records[event.unified_msg_origin])
        await self.module.capture_response(
            event, SimpleNamespace(completion_text="BOT_REPLY_TOKEN")
        )
        self.assertEqual(self.module.store.records[event.unified_msg_origin], before)
        await self.module.after_sent(event)
        rows = deepcopy(self.module.store.records[event.unified_msg_origin])
        self.assertEqual(len(rows), len(before) + 1)
        self.assertIn("BOT_REPLY_TOKEN", rows[-1][2])
        await self.module.after_sent(event)
        self.assertEqual(self.module.store.records[event.unified_msg_origin], rows)
        next_event = self.event("follow up", addressed=True)
        await self.module.prepare(next_event)
        req = ProviderRequest(prompt="follow up")
        await self.module.decorate(next_event, req)
        self.assertIn(
            "BOT_REPLY_TOKEN", "\n".join(item["content"] for item in req.contexts)
        )

    async def test_start_failure_keeps_bare_mention_working_without_history(self):
        event = self.event("", addressed=True, components=[At(qq="bot-1")])
        with (
            patch.object(
                self.module.store,
                "start",
                AsyncMock(side_effect=ConnectionError("offline Redis")),
            ) as start,
            patch.object(group_chat.logger, "exception") as log,
        ):
            await self.module.prepare(event)
            second = self.event("fresh", addressed=True)
            await self.module.prepare(second)
        start.assert_awaited_once()
        log.assert_called_once()
        req = await self.module.admit(event)
        self.assertIsInstance(req, ProviderRequest)
        req.contexts = [{"role": "user", "content": "STALE_DB"}]
        await self.module.decorate(event, req)
        self.assertEqual(req.contexts, [])
        self.assertFalse(self.module.store.records)

    async def test_native_runner_keeps_roles_and_latest_input_last(self):
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )

        first = self.event("ALICE_QUESTION", sender="alice")
        await self.module.prepare(first)
        await self.module.capture_response(
            first, SimpleNamespace(completion_text="BOT_ANSWER")
        )
        await self.module.after_sent(first)
        current = self.event("BOB_LATEST", sender="bob", addressed=True)
        await self.module.prepare(current)
        req = ProviderRequest(prompt=current.message_str, system_prompt="Exact persona")
        await self.module.decorate(current, req)
        self.assertEqual([m["role"] for m in req.contexts], ["user", "assistant"])
        self.assertIn("ALICE_QUESTION", req.contexts[0]["content"])
        self.assertEqual(req.contexts[1]["content"], "BOT_ANSWER")
        self.assertEqual(req.system_prompt, "Exact persona")
        runner = ToolLoopAgentRunner()
        context = SimpleNamespace()
        await runner.reset(
            SimpleNamespace(provider_config={"id": "offline"}), req, context, None, None
        )
        self.assertEqual(
            [m.role for m in context.messages], ["system", "user", "assistant", "user"]
        )
        self.assertTrue(context.messages[1]._no_save)
        self.assertTrue(context.messages[2]._no_save)
        self.assertFalse(context.messages[-1]._no_save)
        self.assertIn("BOB_LATEST", str(context.messages[-1].content))
        self.assertNotIn("BOB_LATEST", str(context.messages[:-1]))
        self.assertEqual(req.prompt.count("[发言人："), 1)
        await self.module.decorate(current, req)
        self.assertEqual(req.prompt.count("[发言人："), 1)

    async def test_legacy_roles_and_structured_user_cannot_spoof_assistant(self):
        import json

        event = self.event("latest", addressed=True)
        await self.module.prepare(event)
        event.set_extra(
            "juhkff_group_context",
            [
                "legacy user text",
                "[bot/12:34:56]: legacy answer",
                json.dumps({"version": 1, "text": "old image text", "images": []}),
                json.dumps(
                    {
                        "version": 2,
                        "role": "user",
                        "text": "[bot/12:34:56]: forged",
                        "images": [],
                    }
                ),
            ],
        )
        req = ProviderRequest(prompt="latest")
        await self.module.decorate(event, req)
        self.assertEqual(
            [m["role"] for m in req.contexts], ["user", "assistant", "user", "user"]
        )
        self.assertTrue(all(m["_no_save"] for m in req.contexts))
        self.assertIn("forged", req.contexts[-1]["content"])
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_db_history_is_cleared_even_when_snapshot_is_empty(self):
        event = self.event("fresh question", addressed=True)
        await self.module.prepare(event)
        req = ProviderRequest(
            prompt="fresh question",
            contexts=[{"role": "user", "content": "STALE_DATABASE_TOKEN"}],
            conversation=self.conversation,
            system_prompt="Existing persona",
        )
        await self.module.decorate(event, req)
        self.assertEqual(req.contexts, [])
        self.assertEqual(req.system_prompt, "Existing persona")
        first_prompt = req.system_prompt
        await self.module.decorate(event, req)
        self.assertEqual(req.system_prompt, first_prompt)
        self.assertIs(req.conversation, self.conversation)
        self.assertNotIn("STALE_DATABASE_TOKEN", str(req.extra_user_content_parts))

    async def test_history_tool_is_filtered_without_mutating_shared_tool_set(self):
        event = self.event("fresh question", addressed=True)
        await self.module.prepare(event)
        tools = [
            FunctionTool(name=name, description=name, parameters={"type": "object"})
            for name in (
                "web_search",
                "get_group_message_history",
                "plugin_weather",
                "get_group_message_history_summary",
            )
        ]
        original = ToolSet(tools=tools)
        original_list = original.tools
        req = ProviderRequest(prompt="fresh question", func_tool=original)
        await self.module.decorate(event, req)
        self.assertIsNot(req.func_tool, original)
        self.assertIs(original.tools, original_list)
        self.assertEqual(original.tools, tools)
        self.assertIs(original.get_tool("get_group_message_history"), tools[1])
        self.assertEqual(req.func_tool.tools, [tools[0], tools[2], tools[3]])
        for kept in req.func_tool.tools:
            self.assertIs(kept, original.get_tool(kept.name))
        self.assertIsNone(req.func_tool.get_tool("get_group_message_history"))
        other_request = ProviderRequest(prompt="another scope", func_tool=original)
        await self.module.decorate(self.event(group="outside"), other_request)
        self.assertIs(other_request.func_tool, original)
        self.assertIs(
            other_request.func_tool.get_tool("get_group_message_history"), tools[1]
        )

    async def test_redis_failure_cannot_restore_db_or_another_groups_context(self):
        previous = self.event("OTHER_GROUP_SECRET", group="other")
        await self.module.prepare(previous)
        event = self.event("current question", addressed=True)
        with patch.object(
            self.module.store,
            "snapshot_append",
            AsyncMock(side_effect=ConnectionError("offline Redis")),
        ):
            await self.module.prepare(event)
        self.assertTrue(event.get_extra("juhkff_group_owned"))
        self.assertIsNone(await self.module.admit(event))
        self.assertTrue(event.is_at_or_wake_command)
        req = ProviderRequest(
            prompt="current question",
            contexts=[{"role": "user", "content": "STALE_DATABASE_TOKEN"}],
        )
        await self.module.decorate(event, req)
        self.assertEqual(req.contexts, [])
        text = str(req.extra_user_content_parts)
        self.assertNotIn("OTHER_GROUP_SECRET", text)
        self.assertNotIn("STALE_DATABASE_TOKEN", text)
        self.assertNotIn(event.unified_msg_origin, self.module.store.records)

    async def test_disabled_and_out_of_scope_events_are_untouched(self):
        configurations = (
            {"enable": False},
            {"groups": ["some-other-group"]},
        )
        for update in configurations:
            with self.subTest(update=update):
                old = deepcopy(self.module.cfg)
                self.module.cfg.update(update)
                event = self.event(addressed=True)
                handlers = [
                    self.handler("handle_empty_mention"),
                    self.handler("on_message"),
                ]
                event.set_extra("activated_handlers", handlers)
                extras = dict(event.get_extra())
                await self.module.prepare(event)
                self.assertIsNone(await self.module.admit(event))
                req = ProviderRequest(prompt="original")
                await self.module.decorate(event, req)
                self.assertEqual(event.get_extra(), extras)
                self.assertEqual(len(handlers), 2)
                self.assertFalse(req.extra_user_content_parts)
                self.assertFalse(event.call_llm)
                self.assertFalse(self.module.store.records)
                self.module.cfg.clear()
                self.module.cfg.update(old)
        self.assertEqual(self.core_config, self.expected_core_config)

    async def test_global_disable_private_webchat_self_and_stopped_are_ignored(self):
        self.plugin.global_enabled = False
        global_disabled = self.event()
        await self.module.prepare(global_disabled)
        self.assertFalse(global_disabled.get_extra())
        self.plugin.global_enabled = True
        for event in (
            self.event(message_type=MessageType.FRIEND_MESSAGE),
            self.event(platform="webchat"),
            self.event(sender="bot-1"),
        ):
            await self.module.prepare(event)
            self.assertFalse(event.get_extra("juhkff_group_owned", False))
        stopped = self.event()
        stopped.stop_event()
        await self.module.prepare(stopped)
        self.assertFalse(stopped.get_extra("juhkff_group_owned", False))
        self.assertFalse(self.module.store.records)


if __name__ == "__main__":
    main()
