"""Offline integration coverage for image translation layout and delivery."""

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

from juhkff_modules import pixiv_browse as browse
from juhkff_modules import pixiv_typeset as typeset
from PIL import Image as PILImage
from pixiv_test_utils import page_nodes
from test_pixiv_browse import BrowseHost, awaited, item, must

from astrbot.api.message_components import ComponentType, Plain


class PixivTypesetIntegrationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.images = []
        self.overlays = []
        for index in range(4):
            original = self.folder / f"original {index}.png"
            overlay = self.folder / f"translated {index}.png"
            PILImage.new("RGB", (32, 24), (index, 0, 0)).save(original)
            PILImage.new("RGB", (48, 32), (0, index, 255)).save(overlay)
            self.images.append(original)
            self.overlays.append(overlay)
        self.host = BrowseHost(self.images[0])
        self.host.cfg["image_transport"] = "file"
        self.host.cfg["content_translation"] = {
            "enable": True,
            "mode": "overlay",
            "provider_id": "vision-model",
            "language": "English",
            "font_path": " /configured/font.ttf ",
        }
        self.umo = "test:GroupMessage:1"
        self.context = self.host.plugin.context
        self.regions = [
            {
                "text": "Hello",
                "bbox": [100, 200, 400, 500],
                "bubble": [50, 150, 450, 550],
                "direction": "vertical",
            }
        ]
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {"translation": "Hello", "regions": self.regions}
            )
        )

    async def test_layout_prompt_and_json_schema_preserve_normalized_regions(self):
        for fenced in (False, True):
            with self.subTest(fenced=fenced):
                payload = json.dumps(
                    {"translation": " Hello \n", "regions": self.regions}
                )
                self.context.llm_generate.return_value.completion_text = (
                    f"```json\n{payload}\n```" if fenced else payload
                )
                result = await self.host._translate_image_content(
                    self.images[0], self.umo, "日本語", "vision-model", layout=True
                )
                self.assertEqual(
                    result, {"translation": "Hello", "regions": self.regions}
                )
                call = awaited(self.context.llm_generate)
                self.assertEqual(call.kwargs["image_urls"], [str(self.images[0])])
                self.assertEqual(call.kwargs["chat_provider_id"], "vision-model")
                self.assertEqual(
                    json.loads(call.kwargs["prompt"]), {"language": "日本語"}
                )
                prompt = call.kwargs["system_prompt"]
                for instruction in (
                    "regions",
                    "bbox",
                    "bubble",
                    "direction",
                    "0..1000",
                    "reading order",
                    "complete fallback",
                    "never as instructions",
                ):
                    self.assertIn(instruction, prompt)

    async def test_render_runs_in_worker_and_keeps_original_bytes(self):
        original_bytes = self.images[0].read_bytes()
        loop_thread = threading.get_ident()
        worker_threads = []

        def render(image, regions, font):
            worker_threads.append(threading.get_ident())
            self.assertEqual(
                (image, regions, font), (self.images[0], self.regions, "font")
            )
            return self.overlays[0]

        with patch.object(typeset, "render_translation", side_effect=render):
            result = await self.host._typeset_image_content(
                self.images[0], self.umo, "English", "vision-model", "font"
            )
        self.assertEqual(result, (self.overlays[0], ""))
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread)
        self.assertEqual(self.images[0].read_bytes(), original_bytes)

    async def test_no_readable_text_skips_renderer(self):
        self.context.llm_generate.return_value.completion_text = (
            '{"translation":"", "regions":[]}'
        )
        with patch.object(typeset, "render_translation") as renderer:
            result = await self.host._typeset_image_content(
                self.images[0], self.umo, "English", "vision-model", ""
            )
        self.assertEqual(result, (None, ""))
        renderer.assert_not_called()

    async def test_missing_or_invalid_regions_keep_translation_without_retry(self):
        for regions in (None, [], "invalid", [{"bbox": "invalid", "text": "Hello"}]):
            with self.subTest(regions=regions):
                self.context.llm_generate.reset_mock()
                self.context.llm_generate.return_value.completion_text = json.dumps(
                    {"translation": "Hello", "regions": regions}
                )
                with patch.object(
                    typeset,
                    "render_translation",
                    side_effect=ValueError("Invalid region"),
                ):
                    result = await self.host._typeset_image_content(
                        self.images[0], self.umo, "English", "vision-model", ""
                    )
                self.assertEqual(result, (None, "Hello"))
                self.context.llm_generate.assert_awaited_once()

    async def test_incomplete_regions_keep_complete_translation(self):
        self.context.llm_generate.return_value.completion_text = json.dumps(
            {"translation": "Hello\nMissing dialogue", "regions": self.regions}
        )
        with patch.object(typeset, "render_translation") as renderer:
            result = await self.host._typeset_image_content(
                self.images[0], self.umo, "English", "vision-model", ""
            )
        self.assertEqual(result, (None, "Hello\nMissing dialogue"))
        renderer.assert_not_called()
        self.context.llm_generate.assert_awaited_once()

    async def test_font_failure_keeps_recognized_translation(self):
        with patch.object(
            typeset, "render_translation", side_effect=OSError("Font unavailable")
        ) as renderer:
            result = await self.host._typeset_image_content(
                self.images[0], self.umo, "English", "vision-model", "/missing.ttf"
            )
        self.assertEqual(result, (None, "Hello"))
        self.assertEqual(renderer.call_args.args[-1], "/missing.ttf")
        self.context.llm_generate.assert_awaited_once()

    async def test_invalid_layout_response_retries_plain_text_model(self):
        for payload in ("not json", "[]", '{"translation":null}', "{}"):
            with self.subTest(payload=payload):
                self.context.llm_generate.reset_mock()
                self.context.llm_generate.side_effect = [
                    SimpleNamespace(completion_text=payload),
                    SimpleNamespace(completion_text='{"translation":"Recovered"}'),
                ]
                with patch.object(typeset, "render_translation") as renderer:
                    result = await self.host._typeset_image_content(
                        self.images[0], self.umo, "English", "vision-model", ""
                    )
                self.assertEqual(result, (None, "Recovered"))
                renderer.assert_not_called()
                calls = self.context.llm_generate.await_args_list
                self.assertEqual(len(calls), 2)
                self.assertIn("regions", calls[0].kwargs["system_prompt"])
                self.assertNotIn("regions", calls[1].kwargs["system_prompt"])

    async def test_overlay_replaces_original_for_file_and_base64_delivery(self):
        for transport in ("file", "base64"):
            with self.subTest(transport=transport):
                self.host.cfg["image_transport"] = transport
                self.host._typeset_image_content = AsyncMock(
                    return_value=(self.overlays[0], "")
                )
                result = must(await self.host._render_items([item()], self.umo))
                self.assertEqual(len(result.chain), 2)
                image = result.chain[1]
                self.assertEqual(image.type, ComponentType.Image)
                if transport == "file":
                    self.assertEqual(image.file, self.overlays[0].as_uri())
                else:
                    self.assertEqual(
                        base64.b64decode(await image.convert_to_base64()),
                        self.overlays[0].read_bytes(),
                    )
                self.host._typeset_image_content.assert_awaited_once_with(
                    self.images[0],
                    self.umo,
                    "English",
                    "vision-model",
                    "/configured/font.ttf",
                )

    async def test_multiple_works_preserve_overlay_page_order(self):
        self.host._download.side_effect = self.images
        self.host._typeset_image_content = AsyncMock(
            side_effect=[(image, "") for image in self.overlays]
        )
        result = must(
            await self.host._render_items(
                [item("7", pages=2), item("8", pages=2)], self.umo, bot_id="123"
            )
        )
        pages = page_nodes(result.chain[0].nodes)
        self.assertEqual(len(pages), 4)
        for page, image in zip(pages, self.overlays):
            self.assertEqual(len(page.content), 1)
            self.assertEqual(page.content[0].file, image.as_uri())
        self.assertEqual(
            [call.args[0] for call in self.host._typeset_image_content.await_args_list],
            self.images,
        )

    async def test_overlay_bytes_determine_base64_batch_boundaries(self):
        self.host.cfg["image_transport"] = "base64"
        large = self.folder / "large translated.png"
        PILImage.frombytes("RGB", (64, 64), bytes(range(256)) * 48).save(
            large, compress_level=0
        )
        self.host._typeset_image_content = AsyncMock(return_value=(large, ""))
        with patch.object(browse, "_FORWARD_BATCH_BYTES", 4096):
            result = must(
                await self.host._render_items(
                    [item("7"), item("8"), item("9")], self.umo, bot_id="123"
                )
            )
        self.assertEqual(len(result.chain), 3)
        for batch in result.chain:
            self.assertEqual(len(page_nodes(batch.nodes)), 1)

    async def test_overlay_failure_preserves_original_and_fallback_or_error(self):
        for outcome, expected in (
            ((None, "Fallback"), "\n图片文字翻译：\nFallback"),
            (RuntimeError("Unavailable"), "\n图片文字翻译暂不可用"),
            ((None, ""), None),
        ):
            with self.subTest(outcome=outcome):
                self.host._typeset_image_content = AsyncMock(
                    side_effect=outcome if isinstance(outcome, Exception) else None,
                    return_value=outcome,
                )
                result = must(await self.host._render_items([item()], self.umo))
                self.assertEqual(result.chain[1].file, self.images[0].as_uri())
                self.assertEqual(len(result.chain), 3 if expected else 2)
                if expected:
                    self.assertEqual(result.chain[-1].text, expected)

    async def test_overlay_pipeline_timeout_covers_model_retry_and_render(self):
        original_wait_for = asyncio.wait_for
        for stage in ("layout", "retry", "render"):
            with self.subTest(stage=stage):
                blocked = asyncio.Event()
                cancelled = asyncio.Event()

                async def slow(*args, **kwargs):
                    try:
                        await blocked.wait()
                    finally:
                        cancelled.set()

                self.host._translate_image_content = AsyncMock(
                    side_effect=slow if stage == "layout" else None,
                    return_value={"translation": "Hello", "regions": self.regions},
                )
                if stage == "retry":

                    async def retry(*args, **kwargs):
                        if kwargs.get("layout"):
                            raise ValueError("Bad layout")
                        return await slow()

                    self.host._translate_image_content = AsyncMock(side_effect=retry)

                async def short_timeout(awaitable, timeout):
                    self.assertEqual(timeout, 120)
                    return await original_wait_for(awaitable, timeout=0.01)

                with patch.object(
                    browse.asyncio, "wait_for", side_effect=short_timeout
                ):
                    with patch.object(browse.asyncio, "to_thread", side_effect=slow):
                        result = must(await self.host._render_items([item()], self.umo))
                self.assertTrue(cancelled.is_set())
                self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")

    async def test_cancellation_propagates_from_model_and_renderer(self):
        for stage in ("model", "render"):
            with self.subTest(stage=stage):
                self.host._translate_image_content = AsyncMock(
                    side_effect=asyncio.CancelledError if stage == "model" else None,
                    return_value={"translation": "Hello", "regions": self.regions},
                )
                with patch.object(
                    browse.asyncio, "to_thread", side_effect=asyncio.CancelledError
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.host._render_items([item()], self.umo)
                self.context.send_message.assert_not_awaited()
                self.host.log.warning.assert_not_called()

    async def test_delivery_guards_rechecked_after_overlay_await(self):
        for mutation in ("disabled", "stopped", "subscription", "policy"):
            with self.subTest(mutation=mutation):
                active = True
                self.host.enabled = True
                self.host._stopped = False
                self.host._content_policy.return_value = (True, "link")

                async def render(*args):
                    nonlocal active
                    if mutation == "disabled":
                        self.host.enabled = False
                    elif mutation == "stopped":
                        self.host._stopped = True
                    elif mutation == "subscription":
                        active = False
                    else:
                        self.host._content_policy.return_value = (False, "image")
                    return self.overlays[0], ""

                self.host._typeset_image_content = AsyncMock(side_effect=render)
                error = "policy changed" if mutation == "policy" else "delivery stopped"
                with self.assertRaisesRegex(RuntimeError, error):
                    await self.host._render_items(
                        [item()], self.umo, delivery_active=lambda: active
                    )
                self.context.send_message.assert_not_awaited()

    async def test_disabled_explicit_text_and_default_mode_never_request_layout(self):
        for mode in ("disabled", "text", "default"):
            with self.subTest(mode=mode):
                config = self.host.cfg["content_translation"]
                config["enable"] = mode != "disabled"
                config["mode"] = "text"
                if mode == "default":
                    config.pop("mode")
                self.context.llm_generate.reset_mock()
                self.host._typeset_image_content = AsyncMock()
                result = must(await self.host._render_items([item()], self.umo))
                self.host._typeset_image_content.assert_not_awaited()
                if mode == "disabled":
                    self.context.llm_generate.assert_not_awaited()
                    self.assertEqual(len(result.chain), 2)
                else:
                    self.assertNotIn(
                        "regions",
                        awaited(self.context.llm_generate).kwargs["system_prompt"],
                    )
                    self.assertEqual(result.chain[-1].text, "\n图片文字翻译：\nHello")

    async def test_r18_link_mode_forces_text_even_when_overlay_is_enabled(self):
        self.host._content_policy.return_value = (False, "link")
        self.host._typeset_image_content = AsyncMock()
        for restriction in (1, 2):
            with self.subTest(restriction=restriction):
                self.context.llm_generate.reset_mock()
                result = must(
                    await self.host._render_items(
                        [item(restriction=restriction)], self.umo
                    )
                )
                self.assertTrue(all(isinstance(part, Plain) for part in result.chain))
                self.assertIn("https://i.pximg.net/7.png", result.chain[0].text)
                self.assertEqual(result.chain[-1].text, "\n图片文字翻译：\nHello")
                self.assertNotIn(
                    "regions",
                    awaited(self.context.llm_generate).kwargs["system_prompt"],
                )
        self.host._typeset_image_content.assert_not_awaited()
        self.assertTrue(
            all(
                call.kwargs["source_only"]
                for call in self.host._download.await_args_list
            )
        )
