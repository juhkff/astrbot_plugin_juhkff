"""Offline integration coverage for built-in image translation and delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from juhkff_modules import pixiv_baidu as baidu
from juhkff_modules import pixiv_browse as browse
from juhkff_modules import pixiv_typeset as typeset
from juhkff_modules.pixiv_config import migrate_pixiv_config
from PIL import Image as PILImage
from pixiv_test_utils import page_nodes
from test_pixiv_browse import BrowseHost, awaited, item, must

from astrbot.api import AstrBotConfig
from astrbot.api.message_components import ComponentType


class PixivBuiltinIntegrationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.original = self.folder / "original image.png"
        self.translated = self.folder / "translated image.png"
        PILImage.new("RGB", (800, 1000), "white").save(self.original)
        PILImage.new("RGB", (800, 1000), "yellow").save(self.translated)
        self.host = BrowseHost(self.original)
        self.host.cfg["image_transport"] = "file"
        self.host.cfg["content_translation"] = {
            "method": "builtin",
            "enable": False,
            "mode": "overlay",
            "provider_id": "text-vision",
            "builtin_provider_id": "builtin-vision",
            "language": "English",
        }
        self.umo = "test:GroupMessage:1"
        self.context = self.host.plugin.context
        self.regions = [
            {
                "bbox": [100, 100, 400, 250],
                "direction": "horizontal",
                "translation": "Hello",
                "font_size": 28,
            }
        ]
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps({"regions": self.regions})
        )

    async def test_provider_selection_json_fences_and_worker_thread(self):
        loop_thread = threading.get_ident()
        original_bytes = self.original.read_bytes()
        for provider, expected in (
            ("builtin-vision", "builtin-vision"),
            ("", "group-model"),
        ):
            for fence in ("", "json", "plain"):
                with self.subTest(provider=provider, fence=fence):
                    self.context.get_current_chat_provider_id.reset_mock()
                    self.context.llm_generate.reset_mock()
                    payload = json.dumps({"regions": self.regions})
                    if fence:
                        payload = (
                            f"```{'json' if fence == 'json' else ''}\n{payload}\n```"
                        )
                    self.context.llm_generate.return_value.completion_text = payload
                    worker_threads = []

                    def render(image, regions):
                        worker_threads.append(threading.get_ident())
                        self.assertEqual(image, self.original)
                        self.assertEqual(regions, self.regions)
                        return self.translated

                    with (
                        patch.object(
                            typeset, "render_translation", side_effect=render
                        ) as renderer,
                        patch.object(
                            baidu, "translate_image", new_callable=AsyncMock
                        ) as external,
                    ):
                        result = await self.host._builtin_typeset_image_content(
                            self.original, self.umo, "日本語", provider
                        )
                    self.assertEqual(result, (self.translated, ""))
                    renderer.assert_called_once()
                    self.assertEqual(len(worker_threads), 1)
                    self.assertNotEqual(worker_threads[0], loop_thread)
                    external.assert_not_awaited()
                    if provider:
                        self.context.get_current_chat_provider_id.assert_not_awaited()
                    else:
                        self.context.get_current_chat_provider_id.assert_awaited_once_with(
                            self.umo
                        )
                    call = awaited(self.context.llm_generate)
                    self.assertEqual(call.kwargs["chat_provider_id"], expected)
                    self.assertEqual(call.kwargs["image_urls"], [str(self.original)])
                    self.assertEqual(
                        json.loads(call.kwargs["prompt"])["language"], "日本語"
                    )
                    self.assertIn("regions", call.kwargs["system_prompt"])
                    self.assertIn("1000", call.kwargs["system_prompt"])
        self.assertEqual(self.original.read_bytes(), original_bytes)

    async def test_empty_regions_deliver_original_without_failure_notice(self):
        self.context.llm_generate.return_value.completion_text = '{"regions":[]}'
        with patch.object(typeset, "render_translation") as renderer:
            result = must(await self.host._render_items([item()], self.umo))
        renderer.assert_not_called()
        self.assertEqual(len(result.chain), 2)
        self.assertEqual(result.chain[1].file, self.original.as_uri())

    async def test_invalid_response_never_reaches_renderer(self):
        for payload in (
            "not json",
            "[]",
            "null",
            "{}",
            '{"regions":null}',
            '{"regions":{}}',
            'prefix {"regions":[]}',
        ):
            with self.subTest(payload=payload):
                self.context.llm_generate.return_value.completion_text = payload
                with patch.object(typeset, "render_translation") as renderer:
                    with self.assertRaises(ValueError):
                        await self.host._builtin_typeset_image_content(
                            self.original, self.umo, "English", "builtin-vision"
                        )
                renderer.assert_not_called()

    async def test_rendered_file_and_base64_delivery_use_original_download(self):
        for transport in ("file", "base64"):
            with self.subTest(transport=transport):
                self.host.cfg["image_transport"] = transport
                self.host._download.reset_mock()
                with (
                    patch.object(
                        typeset, "render_translation", return_value=self.translated
                    ) as renderer,
                    patch.object(
                        baidu, "translate_image", new_callable=AsyncMock
                    ) as external,
                ):
                    result = must(await self.host._render_items([item()], self.umo))
                self.assertEqual(len(result.chain), 2)
                image = result.chain[1]
                self.assertEqual(image.type, ComponentType.Image)
                if transport == "file":
                    self.assertEqual(image.file, self.translated.as_uri())
                else:
                    self.assertEqual(
                        base64.b64decode(await image.convert_to_base64()),
                        self.translated.read_bytes(),
                    )
                renderer.assert_called_once_with(self.original, self.regions)
                external.assert_not_awaited()
                self.assertTrue(awaited(self.host._download).kwargs["source_only"])
                self.assertEqual(
                    awaited(self.context.llm_generate).kwargs["chat_provider_id"],
                    "builtin-vision",
                )

    async def test_r18_links_use_builtin_provider_for_plain_translation(self):
        self.host._content_policy.return_value = (False, "link")
        self.host._translate_image_content = AsyncMock(return_value="Hello")
        with (
            patch.object(typeset, "render_translation") as renderer,
            patch.object(baidu, "translate_image", new_callable=AsyncMock) as external,
        ):
            for restriction in (1, 2):
                with self.subTest(restriction=restriction):
                    result = must(
                        await self.host._render_items(
                            [item(restriction=restriction)], self.umo
                        )
                    )
                    pages = page_nodes(result.chain[1].nodes)
                    self.assertEqual(len(pages), 1)
                    self.assertIn("https://i.pximg.net/7.png", pages[0].content[0].text)
                    self.assertEqual(
                        pages[0].content[-1].text, "\n图片文字翻译：\nHello"
                    )
                    self.assertTrue(
                        all(
                            part.type == ComponentType.Plain
                            for part in pages[0].content
                        )
                    )
                    self.host._translate_image_content.assert_awaited_with(
                        self.original, self.umo, "English", "builtin-vision"
                    )
        renderer.assert_not_called()
        external.assert_not_awaited()
        self.context.llm_generate.assert_not_awaited()

    async def test_model_and_renderer_failures_keep_original(self):
        for stage in ("model", "renderer"):
            with self.subTest(stage=stage):
                self.context.llm_generate.side_effect = (
                    RuntimeError("Unavailable") if stage == "model" else None
                )
                with patch.object(
                    typeset,
                    "render_translation",
                    side_effect=ValueError("Invalid regions"),
                ):
                    result = must(await self.host._render_items([item()], self.umo))
                self.assertEqual(result.chain[1].file, self.original.as_uri())
                self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")

    async def test_outer_timeout_cancels_model_and_keeps_original(self):
        original_wait_for = asyncio.wait_for
        cancelled = asyncio.Event()

        async def slow(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def short_timeout(awaitable, timeout):
            self.assertEqual(timeout, 120)
            return await original_wait_for(awaitable, timeout=0.01)

        self.context.llm_generate.side_effect = slow
        with (
            patch.object(browse.asyncio, "wait_for", side_effect=short_timeout),
            patch.object(typeset, "render_translation") as renderer,
        ):
            result = must(await self.host._render_items([item()], self.umo))
        self.assertTrue(cancelled.is_set())
        renderer.assert_not_called()
        self.assertEqual(result.chain[1].file, self.original.as_uri())
        self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")

    async def test_cancellation_propagates_without_render_or_delivery(self):
        self.context.llm_generate.side_effect = asyncio.CancelledError
        with patch.object(typeset, "render_translation") as renderer:
            with self.assertRaises(asyncio.CancelledError):
                await self.host._render_items([item()], self.umo)
        renderer.assert_not_called()
        self.context.send_message.assert_not_awaited()
        self.host.log.warning.assert_not_called()

    async def test_cancelled_native_render_keeps_capacity_until_thread_exits(self):
        entered = asyncio.Event()
        second_model_done = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        render_calls = []

        def render(image, regions):
            render_calls.append(image)
            if len(render_calls) == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(timeout=3):
                    raise TimeoutError("Test renderer was not released")
            return self.translated

        async def model(**kwargs):
            second_model_done.set()
            return SimpleNamespace(
                completion_text=json.dumps({"regions": self.regions})
            )

        with patch.object(typeset, "render_translation", side_effect=render):
            first = asyncio.create_task(
                self.host._builtin_typeset_image_content(
                    self.original, self.umo, "English", "builtin-vision"
                )
            )
            second = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.context.llm_generate.side_effect = model
                second = asyncio.create_task(
                    self.host._builtin_typeset_image_content(
                        self.original, self.umo, "English", "builtin-vision"
                    )
                )
                await asyncio.wait_for(second_model_done.wait(), timeout=1)
                self.assertEqual(render_calls, [self.original])
                self.assertFalse(second.done())
                release.set()
                self.assertEqual(
                    await asyncio.wait_for(second, timeout=1), (self.translated, "")
                )
                self.assertEqual(render_calls, [self.original, self.original])
            finally:
                release.set()
                for task in (first, second):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (first, second) if task is not None),
                    return_exceptions=True,
                )

    async def test_schema_and_migration_preserve_builtin_model(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
        )
        path = self.folder / "plugin.json"
        path.write_text(
            json.dumps(
                {
                    "pixiv_push": {
                        "content_translation": self.host.cfg["content_translation"]
                    }
                }
            ),
            encoding="utf-8",
        )
        config = AstrBotConfig(config_path=str(path), schema=schema)
        migrate_pixiv_config(config)
        config.save_config()
        reloaded = AstrBotConfig(config_path=str(path), schema=schema)
        self.assertFalse(migrate_pixiv_config(reloaded))
        settings = reloaded["pixiv_push"]["content_translation"]
        self.assertEqual(settings["method"], "builtin")
        self.assertEqual(settings["builtin_provider_id"], "builtin-vision")
        self.assertEqual(settings["provider_id"], "text-vision")
        provider_schema = schema["pixiv_push"]["items"]["content_translation"]["items"][
            "builtin_provider_id"
        ]
        self.assertEqual(provider_schema["_special"], "select_provider")
        self.assertEqual(provider_schema["condition"], {"method": "builtin"})
