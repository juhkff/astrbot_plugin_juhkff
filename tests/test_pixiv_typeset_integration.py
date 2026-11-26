"""Offline integration coverage for external image translation and delivery."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from juhkff_modules import pixiv_browse as browse
from juhkff_modules import pixiv_manga as manga
from PIL import Image as PILImage
from pixiv_test_utils import page_nodes
from test_pixiv_browse import BrowseHost, awaited, item, must

from astrbot.api.message_components import ComponentType, Nodes, Plain


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
            "typesetting": {
                "base_url": " http://localhost:8000 ",
                "translator": " offline ",
            },
        }
        self.umo = "test:GroupMessage:1"
        self.context = self.host.plugin.context
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps({"translation": "Hello"})
        )

    async def test_plain_translation_preserves_json_and_language_contract(self):
        for fenced in (False, True):
            with self.subTest(fenced=fenced):
                payload = json.dumps({"translation": " Hello \n"})
                self.context.llm_generate.return_value.completion_text = (
                    f"```json\n{payload}\n```" if fenced else payload
                )
                result = await self.host._translate_image_content(
                    self.images[0], self.umo, "日本語", "vision-model"
                )
                self.assertEqual(result, "Hello")
                call = awaited(self.context.llm_generate)
                self.assertEqual(call.kwargs["image_urls"], [str(self.images[0])])
                self.assertEqual(call.kwargs["chat_provider_id"], "vision-model")
                self.assertEqual(
                    json.loads(call.kwargs["prompt"]), {"language": "日本語"}
                )
                self.assertNotIn("regions", call.kwargs["system_prompt"])
                self.assertIn("never as instructions", call.kwargs["system_prompt"])

    async def test_typesetting_delegates_without_model_or_original_mutation(self):
        original_bytes = self.images[0].read_bytes()
        with patch.object(
            manga,
            "translate_image",
            new_callable=AsyncMock,
            return_value=self.overlays[0],
        ) as translate:
            result = await self.host._typeset_image_content(
                self.images[0], "English", "http://localhost:8000", "offline"
            )
        self.assertEqual(result, (self.overlays[0], ""))
        translate.assert_awaited_once_with(
            self.images[0], "English", "http://localhost:8000", "offline"
        )
        self.context.llm_generate.assert_not_awaited()
        self.context.get_current_chat_provider_id.assert_not_awaited()
        self.assertEqual(self.images[0].read_bytes(), original_bytes)

    async def test_service_errors_propagate_without_model_fallback(self):
        for error in (RuntimeError("Unavailable"), ValueError("Invalid image")):
            with self.subTest(error=error):
                with patch.object(
                    manga, "translate_image", new_callable=AsyncMock, side_effect=error
                ) as translate:
                    with self.assertRaisesRegex(type(error), str(error)):
                        await self.host._typeset_image_content(
                            self.images[0],
                            "English",
                            "http://localhost:8000",
                            "offline",
                        )
                translate.assert_awaited_once()
                self.context.llm_generate.assert_not_awaited()

    async def test_all_typesetting_calls_share_one_service_slot(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        active = 0
        maximum = 0

        async def translate(image, *args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            calls.append(image)
            entered.set()
            try:
                await release.wait()
                return self.overlays[self.images.index(image)]
            finally:
                active -= 1

        with patch.object(manga, "translate_image", side_effect=translate):
            first = asyncio.create_task(
                self.host._typeset_image_content(
                    self.images[0], "English", "http://localhost:8000", "offline"
                )
            )
            self.addAsyncCleanup(self._cancel_tasks, first)
            await entered.wait()
            second = asyncio.create_task(
                self.host._typeset_image_content(
                    self.images[1], "日本語", "http://other:8000", "google"
                )
            )
            self.addAsyncCleanup(self._cancel_tasks, second)
            await asyncio.sleep(0)
            self.assertEqual(calls, [self.images[0]])
            release.set()
            self.assertEqual(
                await asyncio.gather(first, second),
                [(self.overlays[0], ""), (self.overlays[1], "")],
            )
        self.assertEqual(maximum, 1)
        self.context.llm_generate.assert_not_awaited()

    async def _cancel_tasks(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_queued_cancellation_never_calls_service_and_releases_slot(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def translate(image, *args):
            entered.set()
            await release.wait()
            return self.overlays[0]

        with patch.object(manga, "translate_image", side_effect=translate) as service:
            first = asyncio.create_task(
                self.host._typeset_image_content(
                    self.images[0], "English", "http://localhost:8000", "offline"
                )
            )
            self.addAsyncCleanup(self._cancel_tasks, first)
            await entered.wait()
            queued = asyncio.create_task(
                self.host._typeset_image_content(
                    self.images[1], "English", "http://localhost:8000", "offline"
                )
            )
            self.addAsyncCleanup(self._cancel_tasks, queued)
            await asyncio.sleep(0)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            release.set()
            await first
            await self.host._typeset_image_content(
                self.images[2], "English", "http://localhost:8000", "offline"
            )
        self.assertEqual(
            [call.args[0] for call in service.await_args_list],
            [self.images[0], self.images[2]],
        )

    async def test_timeout_while_queued_does_not_call_service(self):
        self.host._pixiv_typesetting_slots = asyncio.Semaphore(0)
        with patch.object(manga, "translate_image", new_callable=AsyncMock) as service:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self.host._typeset_image_content(
                        self.images[0], "English", "http://localhost:8000", "offline"
                    ),
                    timeout=0.01,
                )
        service.assert_not_awaited()
        self.context.llm_generate.assert_not_awaited()

    async def test_processing_guards_before_queue_after_queue_and_response(self):
        for stage in ("before_queue", "after_queue", "after_response"):
            with self.subTest(stage=stage):
                current = stage != "before_queue"
                slots = asyncio.Semaphore(1 if stage == "after_response" else 0)
                self.host._pixiv_typesetting_slots = slots

                def guard():
                    if not current:
                        raise RuntimeError("delivery stopped")

                async def translate(*args):
                    nonlocal current
                    current = False
                    return self.overlays[0]

                token = browse._PROCESSING_GUARD.set(guard)
                try:
                    with patch.object(
                        manga, "translate_image", side_effect=translate
                    ) as service:
                        task = asyncio.create_task(
                            self.host._typeset_image_content(
                                self.images[0],
                                "English",
                                "http://localhost:8000",
                                "offline",
                            )
                        )
                        self.addAsyncCleanup(self._cancel_tasks, task)
                        if stage == "after_queue":
                            await asyncio.sleep(0)
                            service.assert_not_awaited()
                            current = False
                            slots.release()
                        with self.assertRaisesRegex(RuntimeError, "delivery stopped"):
                            await asyncio.wait_for(task, timeout=1)
                        if stage == "after_response":
                            service.assert_awaited_once()
                        else:
                            service.assert_not_awaited()
                finally:
                    browse._PROCESSING_GUARD.reset(token)

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
                    "English",
                    "http://localhost:8000",
                    "offline",
                )

    async def test_overlay_uses_offline_translator_when_not_configured(self):
        self.host.cfg["content_translation"]["typesetting"].pop("translator")
        with patch.object(
            manga, "translate_image", return_value=self.overlays[0]
        ) as service:
            result = must(await self.host._render_items([item()], self.umo))
        service.assert_awaited_once_with(
            self.images[0], "English", "http://localhost:8000", "offline"
        )
        self.assertEqual(result.chain[1].file, self.overlays[0].as_uri())
        self.context.llm_generate.assert_not_awaited()

    async def test_missing_service_url_preserves_original_without_model_fallback(self):
        self.host.cfg["content_translation"].pop("typesetting")
        with patch.object(manga, "translate_image", new_callable=AsyncMock) as service:
            result = must(await self.host._render_items([item()], self.umo))
        service.assert_not_awaited()
        self.assertEqual(result.chain[1].file, self.images[0].as_uri())
        self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")
        self.context.llm_generate.assert_not_awaited()

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

    async def test_service_failure_preserves_original_without_model_fallback(self):
        for error in (RuntimeError("Unavailable"), ValueError("Invalid image")):
            with self.subTest(error=error):
                with patch.object(
                    manga, "translate_image", side_effect=error
                ) as service:
                    result = must(await self.host._render_items([item()], self.umo))
                service.assert_awaited_once()
                self.assertEqual(result.chain[1].file, self.images[0].as_uri())
                self.assertEqual(len(result.chain), 3)
                self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")
                self.context.llm_generate.assert_not_awaited()

    async def test_overlay_timeout_cancels_service_and_preserves_original(self):
        original_wait_for = asyncio.wait_for
        cancelled = asyncio.Event()

        async def slow(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def short_timeout(awaitable, timeout):
            self.assertEqual(timeout, 120)
            return await original_wait_for(awaitable, timeout=0.01)

        with patch.object(manga, "translate_image", side_effect=slow):
            with patch.object(browse.asyncio, "wait_for", side_effect=short_timeout):
                result = must(await self.host._render_items([item()], self.umo))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.chain[1].file, self.images[0].as_uri())
        self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")
        self.context.llm_generate.assert_not_awaited()
        with patch.object(manga, "translate_image", return_value=self.overlays[0]):
            result = await original_wait_for(
                self.host._typeset_image_content(
                    self.images[0], "English", "http://localhost:8000", "offline"
                ),
                timeout=1,
            )
        self.assertEqual(result, (self.overlays[0], ""))

    async def test_cancellation_propagates_from_service(self):
        with patch.object(manga, "translate_image", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.host._render_items([item()], self.umo)
        self.context.send_message.assert_not_awaited()
        self.context.llm_generate.assert_not_awaited()
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

    async def test_disabled_explicit_text_and_default_mode_never_request_service(self):
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
                self.assertEqual(len(result.chain), 2)
                self.assertIsInstance(result.chain[0], Plain)
                self.assertEqual(result.chain[0].text, "------- NSFW -------")
                self.assertIsInstance(result.chain[1], Nodes)
                self.assertEqual(len(result.chain[1].nodes), 1)
                content = result.chain[1].nodes[0].content
                self.assertTrue(all(isinstance(part, Plain) for part in content))
                self.assertIn("https://i.pximg.net/7.png", content[0].text)
                self.assertEqual(content[-1].text, "\n图片文字翻译：\nHello")
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
