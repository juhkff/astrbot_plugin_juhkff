"""Run standalone: importing the entrypoint reloads the plugin's module classes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, patch

from data.plugins.astrbot_plugin_juhkff import main as entry


class PluginLifecycleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        for target in (
            entry.common,
            __import__(entry.PixivPushModule.__module__, fromlist=["data_dir"]),
        ):
            patcher = patch.object(target, "data_dir", return_value=self.folder)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cron = SimpleNamespace(add_basic_job=AsyncMock(), delete_job=AsyncMock())
        self.cron.add_basic_job.side_effect = lambda **kwargs: SimpleNamespace(
            job_id=f"job-{self.cron.add_basic_job.await_count}"
        )
        # Providers and platforms are deliberately absent, as during cold loading.
        self.context = SimpleNamespace(cron_manager=self.cron)
        self.config = {
            "global": {"enabled": True, "timezone": "Asia/Shanghai"},
            "pixiv_push": {
                "enable": True,
                "ranking": {
                    "daily_push": {
                        "enable": True,
                        "time": "09:30",
                        "groups": ["test:GroupMessage:1"],
                    }
                },
                "random_push": {"enable": True, "groups": ["test:GroupMessage:1"]},
            },
        }
        self.random_started = asyncio.Event()

        async def random_loop():
            self.random_started.set()
            await asyncio.Event().wait()

        for name, mock in (
            ("check", AsyncMock()),
            ("_check_login", AsyncMock()),
            ("run_random_push", AsyncMock(side_effect=random_loop)),
        ):
            patcher = patch.object(entry.PixivPushModule, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(entry.html_renderer, "close", AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plugins = []
        self.plugin = self.make_plugin()

    def make_plugin(self):
        """Create a fresh instance against the same runtime and configuration."""
        plugin = entry.JuhkffPlugin(
            cast(Any, self.context), cast(Any, deepcopy(self.config))
        )
        self.plugins.append(plugin)
        return plugin

    async def asyncTearDown(self):
        for plugin in reversed(self.plugins):
            await plugin.terminate()

    async def test_initialize_registers_jobs_and_random_loop_without_messages(self):
        await self.plugin.initialize()
        await asyncio.wait_for(self.random_started.wait(), timeout=1)
        jobs = {
            call.kwargs["name"].rsplit(":", 1)[-1]: call.kwargs
            for call in self.cron.add_basic_job.await_args_list
        }
        self.assertEqual(
            set(jobs), {"pixiv-check", "pixiv-daily-ranking", "pixiv-cache-cleanup"}
        )
        self.assertEqual(jobs["pixiv-daily-ranking"]["cron_expression"], "30 9 * * *")
        self.assertEqual(jobs["pixiv-daily-ranking"]["timezone"], "Asia/Shanghai")
        self.assertFalse(jobs["pixiv-daily-ranking"]["persistent"])
        self.assertIsNotNone(self.plugin.modules["pixiv_push"]._random_push_task)

    async def test_loaded_hook_and_concurrent_fallback_do_not_duplicate_jobs(self):
        await asyncio.gather(
            self.plugin.initialize(),
            self.plugin._on_astrbot_loaded(),
            self.plugin._ensure_started(),
        )
        await self.plugin.initialize()
        self.assertEqual(self.cron.add_basic_job.await_count, 3)

    async def test_reload_removes_old_jobs_and_restarts_without_loaded_event(self):
        await self.plugin.initialize()
        await asyncio.wait_for(self.random_started.wait(), timeout=1)
        old_task = self.plugin.modules["pixiv_push"]._random_push_task
        await self.plugin.terminate()
        self.assertTrue(old_task.cancelled())
        self.assertEqual(self.cron.delete_job.await_count, 3)
        replacement = self.make_plugin()
        await replacement.initialize()
        self.assertEqual(self.cron.add_basic_job.await_count, 6)
        self.assertIsNot(replacement.modules["pixiv_push"]._random_push_task, old_task)

    async def test_disabled_global_or_module_does_not_register_jobs(self):
        for section, key in (("global", "enabled"), ("pixiv_push", "enable")):
            with self.subTest(section=section):
                plugin = self.make_plugin()
                plugin.config[section][key] = False
                await plugin.initialize()
                self.cron.add_basic_job.assert_not_awaited()
                self.assertIsNone(plugin.modules["pixiv_push"]._random_push_task)

    async def test_one_module_failure_does_not_block_pixiv_startup(self):
        broken = SimpleNamespace(
            key="broken",
            enabled=True,
            log_enabled=False,
            start=AsyncMock(side_effect=RuntimeError("unavailable")),
            stop=AsyncMock(),
        )
        self.plugin.modules = {"broken": broken, **self.plugin.modules}
        await self.plugin.initialize()
        broken.start.assert_awaited_once()
        self.assertEqual(self.cron.add_basic_job.await_count, 3)


if __name__ == "__main__":
    main()
