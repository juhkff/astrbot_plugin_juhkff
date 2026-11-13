"""Verify plugin runtime configuration against native core services offline."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import test_group_chat as fixtures

from astrbot.core.astr_agent_runtime.main_agent.config import MainAgentBuildConfig
from astrbot.core.astr_agent_runtime.main_agent.web_search import apply_web_search
from astrbot.core.config.runtime import MessageConfig, current_message_config
from astrbot.core.config.snapshot import AstrBotConfigSnapshot
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.manager import ProviderManager


class FrameworkConfigTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.GroupChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.module = self.fixture.module
        self.plugin = self.fixture.plugin
        self.event = self.fixture.event(addressed=True)
        self.shared = self.fixture.core_config
        self.shared["provider_settings"]["enable"] = False
        self.shared["provider_settings"]["wake_prefix"] = "global-gate"
        self.before = deepcopy(self.shared)
        self.snapshot = AstrBotConfigSnapshot(self.shared, {})
        self.acm = SimpleNamespace(get_conf=lambda umo: self.snapshot)
        self.plugin.context.astrbot_config_mgr = self.acm
        self.plugin.context.get_config = lambda **kwargs: self.snapshot
        self.pipeline = PipelineContext(
            self.shared, SimpleNamespace(context=self.plugin.context), "offline"
        )
        self.scope = MessageConfig(
            umo=self.event.unified_msg_origin,
            config=self.snapshot,
            context=self.pipeline,
            override_paths=frozenset(),
        )
        token = current_message_config.set(self.scope)
        self.addCleanup(current_message_config.reset, token)
        self.framework = {
            "agent": {
                "runtime": "astrbot",
                "config": {
                    "model": {
                        "provider_id": "plugin-model",
                        "fallback_provider_ids": ["vision-backup"],
                    },
                    "persona": {"persona_id": "plugin-persona"},
                    "misc": {"max_steps": 7, "tool_call_timeout": 19},
                },
            },
            "provider_settings": {
                "web_search": True,
                "websearch_provider": "tavily",
                "websearch_tavily_key": ["offline-key"],
            },
            "kb_names": ["plugin-kb"],
            "kb_agentic_mode": True,
        }
        self.module.cfg["framework"] = self.framework

    async def test_prepare_overrides_before_media_and_preserves_shared_values(self):
        self.event.set_extra("selected_provider", "stale-model")
        self.event.set_extra("selected_model", "stale-name")

        async def format_message(event, cfg):
            self.assertTrue(self.snapshot["provider_settings"]["web_search"])
            self.assertEqual(
                self.snapshot["agent"]["config"]["model"]["provider_id"], "plugin-model"
            )
            return "current group text"

        with patch.object(
            self.module.formatter, "_format_message", side_effect=format_message
        ):
            await self.module.prepare(self.event)
        built = MainAgentBuildConfig.from_config(self.snapshot)
        self.assertTrue(built.provider_settings["web_search"])
        self.assertEqual(built.max_steps, 7)
        self.assertEqual(built.tool_call_timeout, 19)
        self.assertTrue(built.kb_agentic_mode)
        self.assertTrue(self.snapshot["provider_settings"]["enable"])
        self.assertIs(self.pipeline.astrbot_config, self.snapshot)
        self.assertTrue(self.pipeline.astrbot_config["provider_settings"]["enable"])
        self.assertFalse(self.shared["provider_settings"]["enable"])
        self.assertEqual(self.snapshot["provider_settings"]["wake_prefix"], "")
        self.assertIsNone(self.event.get_extra("selected_provider"))
        self.assertIsNone(self.event.get_extra("selected_model"))
        self.assertIn("agent.config.persona.persona_id", self.scope.override_paths)
        self.assertEqual(self.shared, self.before)
        self.snapshot["agent"]["config"]["model"]["fallback_provider_ids"].append(
            "private-only"
        )
        self.assertEqual(
            self.framework["agent"]["config"]["model"]["fallback_provider_ids"],
            ["vision-backup"],
        )
        self.assertEqual(AstrBotConfigSnapshot(self.shared, {}), self.before)

    async def test_native_provider_and_persona_resolve_plugin_selections(self):
        self.assertTrue(self.module._apply_framework_config(self.event))
        provider = SimpleNamespace(provider_config={"id": "plugin-model"})
        manager = ProviderManager.__new__(ProviderManager)
        manager.acm = self.acm
        manager.inst_map = {"plugin-model": provider}
        manager.provider_insts = [
            SimpleNamespace(provider_config={"id": "global-first"})
        ]
        selected = await manager.get_using_provider_async(
            ProviderType.CHAT_COMPLETION, self.event.unified_msg_origin
        )
        self.assertIs(selected, provider)
        personas = PersonaManager.__new__(PersonaManager)
        personas.acm = self.acm
        persona = {"name": "plugin-persona", "prompt": "Configured persona"}
        personas.personas_v3 = [persona]
        name, selected_persona, special = await personas.resolve_selected_persona(
            umo=self.event.unified_msg_origin, platform_name="aiocqhttp"
        )
        self.assertEqual(name, "plugin-persona")
        self.assertIs(selected_persona, persona)
        self.assertFalse(special)

    async def test_native_search_injection_survives_flattening(self):
        await self.module.prepare(self.event)
        self.plugin.context.get_llm_tool_manager = lambda: SimpleNamespace(
            get_builtin_tool=lambda cls: cls()
        )
        req = fixtures.ProviderRequest(prompt="latest", system_prompt="User persona")
        built = MainAgentBuildConfig.from_config(self.snapshot)
        apply_web_search(req, self.plugin.context, built.provider_settings, self.event)
        names = [tool.name for tool in req.func_tool.tools]
        self.assertIn("web_search_tavily", names)
        await self.module.decorate(self.event, req)
        self.assertEqual([tool.name for tool in req.func_tool.tools], names)
        self.assertEqual(req.system_prompt, "User persona")
        disabled = fixtures.ProviderRequest(prompt="latest")
        apply_web_search(
            disabled, self.plugin.context, {"web_search": False}, self.event
        )
        self.assertIsNone(disabled.func_tool)

    async def test_per_event_core_stage_applies_cached_options_and_never_retries(self):
        # Importing the plugin entry reloads its modules. Keep existing test
        # fixtures and patch targets bound to the same module instances.
        module_cache = {
            name: module
            for name, module in sys.modules.copy().items()
            if name == "juhkff_modules" or name.startswith("juhkff_modules.")
        }
        try:
            import check_group_chat_routing as routing_checks
        finally:
            sys.modules.update(module_cache)
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
            InternalAgentSubStage,
        )

        self.framework["provider_settings"].update(
            {
                "streaming_response": True,
                "show_tool_use_status": True,
                "show_tool_call_result": True,
                "buffer_intermediate_messages": True,
                "display_reasoning_text": True,
            }
        )
        self.snapshot["provider_settings"]["unsupported_streaming_strategy"] = (
            "realtime_segmenting"
        )
        self.module.cfg["probability"] = 1
        self.pipeline.agent_runtimes["astrbot"] = object()
        routing = routing_checks.GroupChatRoutingTests()
        routing.__dict__.update(self.fixture.__dict__)
        routing.core_config = self.snapshot
        seen = []
        for bare in (False, True):
            for outcome in ("empty", "yield", "error"):
                with self.subTest(bare=bare, outcome=outcome):
                    event = self.fixture.event(
                        "" if bare else "latest",
                        addressed=bare,
                        components=[fixtures.At(qq="bot-1")] if bare else None,
                    )

                    async def process(stage, current, wake_prefix):
                        seen.append(stage)
                        self.assertEqual(wake_prefix, "")
                        self.assertTrue(stage.streaming_response)
                        self.assertTrue(stage.show_tool_use)
                        self.assertTrue(stage.show_tool_call_result)
                        self.assertTrue(stage.buffer_intermediate_messages)
                        self.assertTrue(stage.show_reasoning)
                        self.assertEqual(
                            stage.unsupported_streaming_strategy, "realtime_segmenting"
                        )
                        self.assertTrue(current.get_extra("enable_streaming"))
                        self.assertTrue(current.get_extra("enable_reasoning"))
                        if bare:
                            self.assertIsNotNone(current.get_extra("provider_request"))
                        if outcome == "error":
                            raise RuntimeError("offline model failure")
                        if outcome == "yield":
                            yield

                    with patch.object(InternalAgentSubStage, "process", process):
                        requests = await routing.run_pipeline(
                            event, outcome="empty", builtin_enabled=False
                        )
                    self.assertEqual(requests, [])
                    self.assertTrue(event.call_llm)
        self.assertEqual(len(seen), 6)
        self.assertEqual(len({id(stage) for stage in seen}), 6)
        self.assertEqual(self.shared, self.before)

    async def test_missing_wrong_or_shared_scope_is_rejected_without_mutation(self):
        for scope, config in (
            (None, self.snapshot),
            (
                MessageConfig("other", self.snapshot, self.scope.context, frozenset()),
                self.snapshot,
            ),
            (self.scope, self.shared),
        ):
            with self.subTest(scope=scope is not None, shared=config is self.shared):
                token = current_message_config.set(scope)
                try:
                    self.plugin.context.get_config = lambda **kwargs: config
                    self.assertFalse(self.module._apply_framework_config(self.event))
                    self.assertTrue(self.event.call_llm)
                    self.assertEqual(self.shared, self.before)
                finally:
                    current_message_config.reset(token)

    async def test_commands_and_unmanaged_groups_do_not_apply_plugin_runtime(self):
        command = self.fixture.event("reset", addressed=True)
        command.set_extra("handlers_parsed_params", {"builtin.reset": {}})
        await self.module.prepare(command)
        self.assertEqual(self.snapshot, self.before)
        self.module.cfg["groups"] = ["another-group"]
        await self.module.prepare(self.event)
        self.assertEqual(self.snapshot, self.before)

    async def test_legacy_keys_are_normalized_without_saving_snapshot(self):
        self.framework["provider_settings"]["websearch_tavily_key"] = "legacy-key"
        self.framework["provider_settings"]["websearch_provider"] = "default"
        self.assertTrue(self.module._apply_framework_config(self.event))
        built = MainAgentBuildConfig.from_config(self.snapshot)
        self.assertEqual(
            built.provider_settings["websearch_tavily_key"], ["legacy-key"]
        )
        self.assertEqual(built.provider_settings["websearch_provider"], "tavily")
        self.assertEqual(self.shared, self.before)

    async def test_removed_compression_override_is_ignored(self):
        original = deepcopy(self.snapshot["agent"]["config"]["compression"])
        self.framework["agent"]["config"]["compression"] = {
            "max_turns": 1,
            "provider_id": "obsolete-compression-provider",
        }
        self.assertTrue(self.module._apply_framework_config(self.event))
        self.assertEqual(self.snapshot["agent"]["config"]["compression"], original)
        self.assertEqual(self.shared, self.before)

    async def test_real_schema_defaults_build_a_native_agent_without_global_setup(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
        )
        self.assertNotIn("context_ttl", schema["group_chat"]["items"])
        framework_schema = schema["group_chat"]["items"]["framework"]
        self.assertNotIn(
            "compression",
            framework_schema["items"]["agent"]["items"]["config"]["items"],
        )
        values = {}
        pending = [(framework_schema, values)]
        while pending:
            node, result = pending.pop()
            for key, item in node["items"].items():
                if item["type"] == "object":
                    result[key] = {}
                    pending.append((item, result[key]))
                else:
                    result[key] = deepcopy(item["default"])
        self.module.cfg["framework"] = values
        self.assertTrue(self.module._apply_framework_config(self.event))
        built = MainAgentBuildConfig.from_config(self.snapshot)
        self.assertEqual(self.snapshot["agent"]["runtime"], "astrbot")
        self.assertEqual(
            built.max_steps, values["agent"]["config"]["misc"]["max_steps"]
        )
        self.assertEqual(
            built.provider_settings["web_search"],
            values["provider_settings"]["web_search"],
        )
        self.assertEqual(self.shared, self.before)
