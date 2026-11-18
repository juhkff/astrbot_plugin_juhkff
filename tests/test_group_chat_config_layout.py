"""Exercise shallow settings through real schema loading and native core readers."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import test_group_chat as fixtures
from juhkff_modules import group_chat
from juhkff_modules.group_chat_config import FRAMEWORK_FIELDS

from astrbot.api import AstrBotConfig
from astrbot.core.astr_agent_runtime.main_agent.config import MainAgentBuildConfig
from astrbot.core.astr_agent_runtime.main_agent.web_search import apply_web_search
from astrbot.core.config.runtime import MessageConfig, current_message_config
from astrbot.core.config.snapshot import AstrBotConfigSnapshot
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.manager import ProviderManager


class GroupChatConfigLayoutTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.GroupChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.plugin = self.fixture.plugin
        self.schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
        )
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "plugin_config.json"

    def load_config(self, values=None):
        if values is not None:
            self.path.write_text(json.dumps(values), encoding="utf-8")
        return AstrBotConfig(config_path=str(self.path), schema=self.schema)

    def construct_module(self, config):
        self.plugin.config = config
        # Keep this construction independent of the composed fixture's patch lifetime.
        with patch.object(
            group_chat, "RedisGroupChatStore", fixtures.MemoryGroupChatStore
        ):
            module = group_chat.GroupChatModule(self.plugin)
        self.plugin.modules[module.key] = module
        return module

    def native_scope(self):
        event = self.fixture.event(addressed=True)
        shared = self.fixture.core_config
        snapshot = AstrBotConfigSnapshot(shared, {})
        acm = SimpleNamespace(get_conf=lambda umo: snapshot)
        self.plugin.context.astrbot_config_mgr = acm
        self.plugin.context.get_config = lambda **kwargs: snapshot
        pipeline = PipelineContext(
            shared, SimpleNamespace(context=self.plugin.context), "offline"
        )
        scope = MessageConfig(event.unified_msg_origin, snapshot, pipeline, frozenset())
        token = current_message_config.set(scope)
        self.addCleanup(current_message_config.reset, token)
        return event, snapshot, scope, acm

    def test_schema_has_six_direct_runtime_groups_and_local_conditions(self):
        groups = self.schema["group_chat"]["items"]
        visible = {
            key
            for key, meta in groups.items()
            if meta["type"] == "object" and not meta.get("invisible")
        }
        runtime_groups = {"model", "search", "reply", "tools", "knowledge", "input"}
        self.assertEqual(visible, runtime_groups | {"caption_settings"})
        self.assertEqual(set(FRAMEWORK_FIELDS), runtime_groups)
        for group in runtime_groups:
            meta = groups[group]
            self.assertEqual(meta["type"], "object")
            siblings = meta["items"]
            self.assertEqual(set(siblings), set(FRAMEWORK_FIELDS[group]))
            for key, leaf in siblings.items():
                with self.subTest(group=group, field=key):
                    self.assertNotEqual(leaf["type"], "object")
                    self.assertNotEqual(leaf["type"], "template_list")
                    for dependency in leaf.get("condition", {}):
                        self.assertIn(dependency, siblings)
                        self.assertNotIn(".", dependency)
        self.assertEqual(groups["framework"]["type"], "dict")
        self.assertEqual(groups["framework"]["default"], {})
        self.assertTrue(groups["framework"]["invisible"])
        self.assertNotIn("items", groups["framework"])

    def test_real_loader_preserves_legacy_values_then_migrates_once(self):
        old = {
            "agent": {
                "runtime": "astrbot",
                "config": {
                    "model": {
                        "provider_id": "old-model",
                        "fallback_provider_ids": [],
                        "request_max_retries": 0,
                    },
                    "persona": {"persona_id": "", "safety_mode": False},
                    "misc": {"max_steps": 17, "tool_call_timeout": 0},
                },
            },
            "provider_settings": {
                "web_search": False,
                "websearch_tavily_key": [],
                "streaming_response": False,
                "prompt_prefix": "",
                "proactive_capability": {"add_cron_tools": False},
                "file_extract": {
                    "enable": True,
                    "provider": "moonshotai",
                    "moonshotai_api_key": "saved-key",
                },
            },
            "kb_names": ["saved-kb"],
            "kb_fusion_top_k": 0,
            "kb_final_top_k": 0,
            "kb_agentic_mode": True,
        }
        config = self.load_config({"group_chat": {"framework": deepcopy(old)}})
        settings = config["group_chat"]
        self.assertEqual(settings["framework"], old)
        self.assertNotEqual(settings["model"]["provider_id"], "old-model")
        with patch.object(
            AstrBotConfig,
            "save_config",
            autospec=True,
            side_effect=AstrBotConfig.save_config,
        ) as save:
            self.construct_module(config)
            self.assertEqual(save.call_count, 1)
        settings = config["group_chat"]
        self.assertEqual(settings["model"]["provider_id"], "old-model")
        self.assertEqual(settings["model"]["fallback_provider_ids"], [])
        self.assertEqual(settings["model"]["request_max_retries"], 0)
        self.assertEqual(settings["model"]["persona_id"], "")
        self.assertIs(settings["model"]["safety_mode"], False)
        self.assertEqual(settings["tools"]["tool_call_timeout"], 0)
        self.assertIs(settings["tools"]["add_cron_tools"], False)
        self.assertEqual(settings["search"]["websearch_tavily_key"], [])
        self.assertIs(settings["reply"]["streaming_response"], False)
        self.assertEqual(settings["input"]["prompt_prefix"], "")
        self.assertEqual(settings["input"]["file_extract_api_key"], "saved-key")
        self.assertEqual(settings["knowledge"]["kb_names"], ["saved-kb"])
        self.assertEqual(settings["knowledge"]["kb_final_top_k"], 0)
        self.assertEqual(settings["knowledge"]["kb_fusion_top_k"], 0)
        self.assertIs(settings["knowledge"]["kb_agentic_mode"], True)
        self.assertEqual(settings["framework"], {})
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8-sig"))["group_chat"][
                "framework"
            ],
            {},
        )

        settings["model"]["provider_id"] = "new-choice"
        settings["knowledge"]["kb_names"] = ["new-kb"]
        settings["knowledge"]["kb_agentic_mode"] = False
        config.save_config()
        reloaded = self.load_config()
        with patch.object(
            AstrBotConfig,
            "save_config",
            autospec=True,
            side_effect=AstrBotConfig.save_config,
        ) as save:
            self.construct_module(reloaded)
            save.assert_not_called()
        newer = reloaded["group_chat"]
        self.assertEqual(newer["model"]["provider_id"], "new-choice")
        self.assertEqual(newer["knowledge"]["kb_names"], ["new-kb"])
        self.assertIs(newer["knowledge"]["kb_agentic_mode"], False)

    def test_wrapped_six_groups_move_to_direct_settings_once(self):
        wrapped = {
            "model": {"provider_id": "wrapped-model", "request_max_retries": 0},
            "search": {"web_search": False, "websearch_tavily_key": []},
            "reply": {"streaming_response": False},
            "tools": {"max_steps": 23, "tool_call_timeout": 0},
            "knowledge": {"kb_names": [], "kb_agentic_mode": False},
            "input": {"prompt_prefix": "", "file_extract_api_key": "wrapped-key"},
            # A partially migrated wrapper can still contain stale native sections.
            "agent": {
                "config": {
                    "model": {"provider_id": "stale-model", "request_max_retries": 9},
                    "misc": {"max_steps": 99, "tool_call_timeout": 120},
                }
            },
            "provider_settings": {
                "web_search": True,
                "streaming_response": True,
                "prompt_prefix": "stale-prefix",
            },
            "kb_names": ["stale-kb"],
            "kb_agentic_mode": True,
        }
        config = self.load_config({"group_chat": {"framework": deepcopy(wrapped)}})
        self.assertEqual(config["group_chat"]["framework"], wrapped)
        with patch.object(
            AstrBotConfig,
            "save_config",
            autospec=True,
            side_effect=AstrBotConfig.save_config,
        ) as save:
            self.construct_module(config)
            self.assertEqual(save.call_count, 1)
        settings = config["group_chat"]
        self.assertEqual(settings["framework"], {})
        for group in FRAMEWORK_FIELDS:
            for key, value in wrapped[group].items():
                with self.subTest(group=group, field=key):
                    self.assertEqual(settings[group][key], value)

        settings["model"]["provider_id"] = "edited-model"
        settings["search"]["web_search"] = True
        settings["reply"]["streaming_response"] = True
        settings["tools"]["max_steps"] = 31
        settings["knowledge"]["kb_names"] = ["edited-kb"]
        settings["input"]["prompt_prefix"] = "edited {{prompt}}"
        expected = {group: deepcopy(settings[group]) for group in FRAMEWORK_FIELDS}
        config.save_config()
        reloaded = self.load_config()
        with patch.object(
            AstrBotConfig,
            "save_config",
            autospec=True,
            side_effect=AstrBotConfig.save_config,
        ) as save:
            self.construct_module(reloaded)
            save.assert_not_called()
        self.assertEqual(reloaded["group_chat"]["framework"], {})
        for group, values in expected.items():
            self.assertEqual(reloaded["group_chat"][group], values)

    def test_fresh_config_does_not_migrate_injected_legacy_defaults(self):
        config = self.load_config({})
        settings = config["group_chat"]
        settings["knowledge"]["kb_names"] = ["fresh-kb"]
        settings["knowledge"]["kb_agentic_mode"] = True
        before = deepcopy(dict(config))
        with patch.object(
            AstrBotConfig,
            "save_config",
            autospec=True,
            side_effect=AstrBotConfig.save_config,
        ) as save:
            self.construct_module(config)
            save.assert_not_called()
        self.assertEqual(dict(config), before)

    async def test_shallow_settings_reach_native_agent_provider_persona_and_search(
        self,
    ):
        config = self.load_config({})
        settings = config["group_chat"]
        selections = {
            "model": {
                "provider_id": "plugin-model",
                "fallback_provider_ids": ["backup"],
                "request_max_retries": 2,
                "persona_id": "plugin-persona",
                "safety_mode": False,
            },
            "search": {
                "web_search": True,
                "websearch_provider": "tavily",
                "websearch_tavily_key": ["offline-key"],
            },
            "tools": {"max_steps": 7, "tool_call_timeout": 19, "add_cron_tools": False},
            "reply": {"streaming_response": False, "display_reasoning_text": True},
            "knowledge": {"kb_names": ["plugin-kb"], "kb_agentic_mode": True},
            "input": {
                "file_extract_enabled": True,
                "file_extract_provider": "moonshotai",
                "file_extract_api_key": "extract-key",
                "prompt_prefix": "custom {{prompt}}",
            },
        }
        for group, values in selections.items():
            settings[group].update(values)
        module = self.construct_module(config)
        event, snapshot, scope, acm = self.native_scope()
        before_shared = deepcopy(self.fixture.core_config)
        before_plugin = deepcopy(dict(config))
        self.assertTrue(module._apply_framework_config(event))
        built = MainAgentBuildConfig.from_config(snapshot)
        self.assertEqual(built.max_steps, 7)
        self.assertEqual(built.tool_call_timeout, 19)
        self.assertEqual(built.request_max_retries, 2)
        self.assertEqual(built.fallback_provider_ids, ["backup"])
        self.assertFalse(built.llm_safety_mode)
        self.assertFalse(built.add_cron_tools)
        self.assertFalse(built.streaming_response)
        self.assertTrue(built.kb_agentic_mode)
        self.assertTrue(built.file_extract_enabled)
        self.assertEqual(built.file_extract_msh_api_key, "extract-key")
        self.assertEqual(snapshot["kb_names"], ["plugin-kb"])
        self.assertEqual(built.provider_settings["prompt_prefix"], "custom {{prompt}}")
        self.assertIn("agent.config.persona.persona_id", scope.override_paths)
        self.assertFalse(event.get_extra("enable_streaming"))
        self.assertTrue(event.get_extra("enable_reasoning"))
        self.assertNotIn("model", snapshot)
        self.assertNotIn("search", snapshot)

        provider = SimpleNamespace(provider_config={"id": "plugin-model"})
        manager = ProviderManager.__new__(ProviderManager)
        manager.acm = acm
        manager.inst_map = {"plugin-model": provider}
        manager.provider_insts = []
        selected = await manager.get_using_provider_async(
            ProviderType.CHAT_COMPLETION, event.unified_msg_origin
        )
        self.assertIs(selected, provider)
        personas = PersonaManager.__new__(PersonaManager)
        personas.acm = acm
        persona = {"name": "plugin-persona", "prompt": "Configured persona"}
        personas.personas_v3 = [persona]
        name, selected_persona, special = await personas.resolve_selected_persona(
            umo=event.unified_msg_origin, platform_name="aiocqhttp"
        )
        self.assertEqual(name, "plugin-persona")
        self.assertIs(selected_persona, persona)
        self.assertFalse(special)
        self.plugin.context.get_llm_tool_manager = lambda: SimpleNamespace(
            get_builtin_tool=lambda cls: cls()
        )
        req = fixtures.ProviderRequest(prompt="latest")
        apply_web_search(req, self.plugin.context, built.provider_settings, event)
        self.assertIn("web_search_tavily", [tool.name for tool in req.func_tool.tools])
        snapshot["agent"]["config"]["model"]["fallback_provider_ids"].append("local")
        self.assertEqual(dict(config), before_plugin)
        self.assertEqual(self.fixture.core_config, before_shared)
