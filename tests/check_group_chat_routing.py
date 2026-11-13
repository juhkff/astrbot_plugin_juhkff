"""Standalone real ProcessStage regressions; entry import reloads plugin modules.

Run with AstrBot and this plugin root on PYTHONPATH, separately from discovery.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import main
from unittest.mock import AsyncMock, patch

import test_group_chat as fixtures

from astrbot.core.pipeline.process_stage.method import star_request
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.waking_check import stage as waking
from astrbot.core.star.star_handler import EventType, star_handlers_registry
from data.plugins.astrbot_plugin_juhkff import main as entry


class GroupChatRoutingTests(fixtures.GroupChatTests):
    async def run_pipeline(self, event, *, outcome="empty", builtin_enabled=True):
        visited, requests = [], []

        async def prepare(event):
            visited.append("prepare")
            await entry.JuhkffPlugin._prepare_group_chat(self.plugin, event)

        async def admit(event):
            visited.append("admit")
            async for result in entry.JuhkffPlugin._admit_group_chat(
                self.plugin, event
            ):
                yield result

        async def unrelated(event):
            visited.append("unrelated")

        async def persistence(event):
            visited.append("persist")

        async def forbidden_builtin(event):
            visited.append("FORBIDDEN")

        def handler(name, callback, module="offline.plugin"):
            return SimpleNamespace(
                handler_name=name,
                handler_module_path=module,
                handler_full_name=f"{module}.{name}",
                handler=callback,
                event_filters=[],
                event_type=EventType.AdapterMessageEvent,
                extras_configs={"priority": 0},
            )

        official = "astrbot.builtin_stars.astrbot.main"
        handlers = [
            handler("_prepare_group_chat", prepare),
            handler("handle_empty_mention", forbidden_builtin, official),
            handler("on_message", unrelated, "unrelated.plugin"),
            handler("on_message", forbidden_builtin, official),
            handler("persist_group_message", persistence, official),
            handler("_admit_group_chat", admit),
        ]
        if not builtin_enabled:
            handlers[:] = [h for h in handlers if h.handler_module_path != official]
        event.set_extra("activated_handlers", handlers)
        event.set_extra("handlers_parsed_params", {})
        ctx = SimpleNamespace(astrbot_config=self.core_config)
        stage = ProcessStage()
        stage.ctx = ctx
        stage.star_request_sub_stage = star_request.StarRequestSubStage()
        await stage.star_request_sub_stage.initialize(ctx)

        async def agent_process(event):
            requests.append(event.get_extra("provider_request"))
            if outcome == "error":
                raise RuntimeError("offline model failure")
            if outcome == "yield":
                yield None
            # Empty and yield both deliberately leave _has_send_oper=False.

        stage.agent_sub_stage = SimpleNamespace(process=agent_process)
        metadata = SimpleNamespace(name="offline", display_name="Offline")
        with (
            patch.dict(
                star_request.star_map,
                {
                    "offline.plugin": metadata,
                    "unrelated.plugin": metadata,
                    official: metadata,
                },
            ),
            patch.object(star_request, "call_event_hook", AsyncMock()),
        ):
            if outcome == "error":
                with self.assertRaisesRegex(RuntimeError, "offline model failure"):
                    async for _ in stage.process(event):
                        pass
            else:
                async for _ in stage.process(event):
                    pass
        self.assertIs(event.get_extra("activated_handlers"), handlers)
        expected = ["prepare", "unrelated"]
        if builtin_enabled:
            expected.append("persist")
        self.assertEqual(visited, expected + ["admit"])
        return requests

    async def test_pipeline_bare_mention_zero_output_has_no_default_retry(self):
        for outcome in ("empty", "yield", "error"):
            with self.subTest(outcome=outcome):
                event = self.event(
                    " \n",
                    addressed=True,
                    components=[fixtures.At(qq="bot-1"), fixtures.Plain(" \n")],
                )
                requests = await self.run_pipeline(event, outcome=outcome)
                self.assertEqual(len(requests), 1)
                self.assertIs(requests[0].conversation, self.conversation)
                self.assertTrue(event.call_llm)
                self.assertFalse(event._has_send_oper)

    async def test_pipeline_runs_with_official_plugin_disabled(self):
        self.module.cfg["probability"] = 1
        for bare in (False, True):
            with self.subTest(bare=bare):
                event = self.event(
                    "" if bare else "normal group message",
                    addressed=bare,
                    components=[fixtures.At(qq="bot-1")] if bare else None,
                )
                requests = await self.run_pipeline(event, builtin_enabled=False)
                self.assertEqual(len(requests), 1)
                if bare:
                    self.assertIs(requests[0].conversation, self.conversation)
                else:
                    self.assertIsNone(requests[0])
                req = fixtures.ProviderRequest(
                    prompt="current", contexts=[{"role": "user", "content": "OLD_DB"}]
                )
                await entry.JuhkffPlugin._group_chat_context(self.plugin, event, req)
                self.assertNotIn("OLD_DB", str(req.contexts))
                self.assertTrue(all(m["role"] == "user" for m in req.contexts))
                self.assertFalse(req.extra_user_content_parts)
                self.assertTrue(req.prompt.startswith("[发言人："))

    async def test_pipeline_active_admission_enters_default_exactly_once(self):
        self.module.cfg["probability"] = 1
        event = self.event("an ordinary group message")
        self.core_config["wake_prefix"] = []
        wake_stage = waking.WakingCheckStage()
        await wake_stage.initialize(
            SimpleNamespace(
                astrbot_config=self.core_config,
                astrbot_config_id="offline",
                db_helper=None,
                plugin_manager=SimpleNamespace(context=self.plugin.context),
            )
        )
        listeners = [
            h
            for h in star_handlers_registry
            if h.handler_name in {"_prepare_group_chat", "_admit_group_chat"}
            and "juhkff" in h.handler_module_path
        ]
        self.assertEqual(len(listeners), 2)
        with (
            patch.object(
                waking.star_handlers_registry,
                "get_handlers_by_event_type",
                return_value=listeners,
            ),
            patch.object(wake_stage._umo_auto_name_recorder, "schedule"),
        ):
            await wake_stage.process(event)
        self.assertTrue(event.is_wake)
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(event.is_stopped())
        requests = await self.run_pipeline(event)
        self.assertEqual(requests, [None])  # No explicit ProviderRequest override.
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(event.call_llm)
        self.manager.get_curr_conversation_id.assert_not_awaited()


# Only run the pipeline cases here; the base suite is covered by discovery.
if __name__ == "__main__":
    main(
        defaultTest=[
            "GroupChatRoutingTests.test_pipeline_bare_mention_zero_output_has_no_default_retry",
            "GroupChatRoutingTests.test_pipeline_active_admission_enters_default_exactly_once",
            "GroupChatRoutingTests.test_pipeline_runs_with_official_plugin_disabled",
        ]
    )
