"""Offline regression coverage for per-module logging and shared ownership."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, DEFAULT, patch

import aiohttp
from astrbot.api import logger as api_logger
from juhkff_modules import common, renderer, report_sources
from juhkff_modules.module_logging import ModuleLogger, module_log_scope


MODULE_KEYS = ("group_chat", "share_parse", "emoji_steal", "daily_report", "pixiv_push", "help")
LEVELS = ("info", "debug", "warning", "error", "exception")


class ExampleModule(common.BaseModule):
    key = "group_chat"


def make_module(print_log=True):
    return ExampleModule(
        cast(
            Any,
            SimpleNamespace(
                config={
                    "global": {"enabled": True, "debug": False},
                    "group_chat": {"enable": True, "print_log": print_log},
                },
                global_enabled=True,
            ),
        )
    )


class ModuleLoggingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.multiple(api_logger, **dict.fromkeys(LEVELS, DEFAULT))
        self.logs = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_silent(self):
        for mock in self.logs.values():
            mock.assert_not_called()

    def reset_logs(self):
        for mock in self.logs.values():
            mock.reset_mock()

    def test_schema_exposes_six_default_enabled_module_switches(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
        self.assertNotIn("debug", schema["global"]["items"])
        self.assertNotIn("print_log", schema["global"]["items"])
        for key in MODULE_KEYS:
            with self.subTest(module=key):
                setting = schema[key]["items"]["print_log"]
                self.assertEqual(setting["type"], "bool")
                self.assertIs(setting["default"], True)
                self.assertEqual(setting["description"], "打印日志")

    def test_base_module_reads_switch_dynamically_and_ignores_legacy_global_debug(self):
        module = make_module()
        del module.cfg["print_log"]
        self.assertTrue(module.log_enabled)
        module.debug("default enabled despite old global.debug=False")
        self.logs["info"].assert_called_once()
        self.logs["debug"].assert_not_called()
        self.reset_logs()
        module.cfg["print_log"] = False
        module.plugin.config["global"]["debug"] = True
        self.assertFalse(module.log_enabled)
        module.debug("silenced instead of downgraded")
        self.assert_silent()
        module.plugin.config["group_chat"] = {"print_log": True}
        module.debug("replacement configuration is read immediately")
        self.logs["info"].assert_called_once()

    def test_all_levels_and_warn_alias_are_silent_when_disabled(self):
        module = make_module(False)
        for level in (*LEVELS, "warn"):
            getattr(module.log, level)("suppressed")
        self.assert_silent()
        module.cfg["print_log"] = True
        for level in LEVELS:
            getattr(module.log, level)("visible")
            self.logs[level].assert_called_once_with("visible")
        module.log.warn("warning alias")
        self.assertEqual(self.logs["warning"].call_count, 2)

    def test_unowned_logger_is_silent_and_bound_owner_wins_over_scope(self):
        shared = ModuleLogger()
        shared.error("no owner")
        self.assert_silent()
        enabled, disabled = make_module(), make_module(False)
        with enabled.log.scope():
            shared.info("scope owner")
            disabled.log.error("explicit disabled owner")
        with disabled.log.scope():
            enabled.log.warning("explicit enabled owner")
        self.logs["info"].assert_called_once_with("scope owner")
        self.logs["warning"].assert_called_once_with("explicit enabled owner")
        self.logs["error"].assert_not_called()
        self.assertIsNone(module_log_scope.get())

    async def test_concurrent_scopes_do_not_leak_between_tasks(self):
        shared = ModuleLogger()
        enabled, disabled = make_module(), make_module(False)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def quiet():
            with disabled.log.scope():
                entered.set()
                await release.wait()
                shared.info("quiet task")

        async def noisy():
            await entered.wait()
            with enabled.log.scope():
                shared.info("loud task")
                release.set()
                await asyncio.sleep(0)
                shared.info("loud task after yield")

        await asyncio.gather(quiet(), noisy())
        self.assertEqual([call.args[0] for call in self.logs["info"].call_args_list],
                         ["loud task", "loud task after yield"])
        shared.info("outside tasks")
        self.assertEqual(self.logs["info"].call_count, 2)
        self.assertIsNone(module_log_scope.get())

    async def test_shared_helpers_follow_current_owner(self):
        module = make_module(False)
        with TemporaryDirectory() as folder:
            invalid = Path(folder) / "broken.json"
            invalid.write_text("not JSON")
            sources = {"politics": (report_sources.Source("offline", "https://offline.invalid/rss"),)}
            for enabled in (False, True):
                with self.subTest(print_log=enabled):
                    self.reset_logs()
                    module.cfg["print_log"] = enabled
                    with (
                        module.log.scope(),
                        patch.object(renderer.LocalHtmlRenderer, "available", return_value=True),
                        patch.object(renderer.renderer, "render", AsyncMock(side_effect=RuntimeError("render failed"))),
                        patch.object(report_sources, "SECTION_SOURCES", sources),
                        patch.object(report_sources, "_request", AsyncMock(side_effect=aiohttp.ClientError("offline"))),
                    ):
                        self.assertEqual(common.load_json(invalid, "fallback"), "fallback")
                        self.assertIsNone(await renderer.render_template("hello", {}))
                        self.assertEqual(await report_sources.collect_sections(None, Path(folder)), {"politics": []})
                    if enabled:
                        self.assertGreaterEqual(self.logs["warning"].call_count, 2)
                        self.logs["exception"].assert_called_once()
                    else:
                        self.assert_silent()

    async def test_scheduler_retains_owner_for_callback_and_deletion_errors(self):
        handlers = []

        async def add_job(**kwargs):
            handlers.append(kwargs["handler"])
            return SimpleNamespace(job_id=str(len(handlers)))

        cron = SimpleNamespace(add_basic_job=AsyncMock(side_effect=add_job),
                               delete_job=AsyncMock(side_effect=RuntimeError("delete failed")))
        scheduler = common.Scheduler(cast(Any, SimpleNamespace(cron_manager=cron)))
        noisy, quiet = make_module(), make_module(False)

        async def callback():
            common.logger.info("shared callback log")
            raise RuntimeError("callback failed")

        for owner in (noisy, quiet):
            with owner.log.scope():
                await scheduler.daily("12:00", callback, "owned")
        # Execute under the opposite scope: ownership belongs to registration.
        with quiet.log.scope():
            await handlers[0]()
        with noisy.log.scope():
            await handlers[1]()
        self.logs["exception"].assert_called_once()
        self.logs["info"].assert_called_once_with("shared callback log")
        # A task retains its owner, rather than a snapshot of the switch value.
        noisy.cfg["print_log"] = False
        quiet.cfg["print_log"] = True
        self.reset_logs()
        await handlers[0]()
        await handlers[1]()
        self.logs["exception"].assert_called_once()
        self.logs["info"].assert_called_once_with("shared callback log")
        self.reset_logs()
        with noisy.log.scope():
            await scheduler.shutdown()
        self.logs["warning"].assert_called_once()
        self.assertEqual(cron.delete_job.await_count, 2)
        self.assertIsNone(module_log_scope.get())

    async def test_scheduler_registration_failure_respects_owner_switch(self):
        cron = SimpleNamespace(add_basic_job=AsyncMock(side_effect=RuntimeError("registration failed")))
        scheduler = common.Scheduler(cast(Any, SimpleNamespace(cron_manager=cron)))
        for enabled in (False, True):
            owner = make_module(enabled)
            with owner.log.scope():
                self.assertIsNone(await scheduler.daily("12:00", AsyncMock(), "failed"))
            self.assertEqual(self.logs["exception"].call_count, int(enabled))
