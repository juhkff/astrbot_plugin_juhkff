"""Offline coverage for per-page OCR translation and delivery guards."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from juhkff_modules import pixiv_browse as browse
from PIL import Image as PILImage
from pixiv_test_utils import page_nodes
from test_pixiv_browse import BrowseHost, awaited, item, must

from astrbot.api.message_components import ComponentType, Nodes, Plain


class PixivContentTranslationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.images = []
        for index in range(4):
            image = self.folder / f"page {index}.png"
            PILImage.new("RGB", (2, 2), (index, 0, 0)).save(image)
            self.images.append(image)
        self.host = BrowseHost(self.images[0])
        self.host.cfg["image_transport"] = "file"
        self.host.cfg["content_translation"] = {
            "enable": True,
            "provider_id": "vision-model",
            "language": "English",
        }
        self.umo = "test:GroupMessage:1"
        self.context = self.host.plugin.context
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text='{"translation":"Translated text"}'
        )

    async def test_helper_selects_explicit_or_current_provider_and_passes_image(self):
        for provider, expected in (
            ("vision-model", "vision-model"),
            ("", "group-model"),
        ):
            with self.subTest(provider=provider):
                self.context.get_current_chat_provider_id.reset_mock()
                self.context.llm_generate.reset_mock()
                result = await self.host._translate_image_content(
                    self.images[0], self.umo, "日本語", provider
                )
                self.assertEqual(result, "Translated text")
                if provider:
                    self.context.get_current_chat_provider_id.assert_not_awaited()
                else:
                    self.context.get_current_chat_provider_id.assert_awaited_once_with(
                        self.umo
                    )
                call = awaited(self.context.llm_generate)
                self.assertEqual(call.kwargs["chat_provider_id"], expected)
                self.assertEqual(call.kwargs["image_urls"], [str(self.images[0])])
                self.assertEqual(
                    json.loads(call.kwargs["prompt"]), {"language": "日本語"}
                )
                self.assertTrue(call.kwargs["system_prompt"])

    async def test_helper_accepts_json_fences_and_no_readable_text(self):
        for payload, expected in (
            ('{"translation":"你好\\n世界"}', "你好\n世界"),
            ('```json\n{"translation":"Hello"}\n```', "Hello"),
            ('```\n{"translation":"Hello"}\n```', "Hello"),
            ('{"translation":""}', ""),
        ):
            with self.subTest(payload=payload):
                self.context.llm_generate.return_value.completion_text = payload
                self.assertEqual(
                    await self.host._translate_image_content(
                        self.images[0], self.umo, "中文", "vision-model"
                    ),
                    expected,
                )

    async def test_helper_rejects_invalid_json_and_non_string_translation(self):
        for payload in (
            "not json",
            "[]",
            "null",
            "{}",
            '{"translation":null}',
            '{"translation":42}',
            '{"translation":false}',
            '{"translation":[]}',
            'prefix {"translation":"Hello"}',
        ):
            with self.subTest(payload=payload):
                self.context.llm_generate.return_value.completion_text = payload
                with self.assertRaises(ValueError):
                    await self.host._translate_image_content(
                        self.images[0], self.umo, "中文", "vision-model"
                    )

    async def test_single_image_translation_follows_image_with_configured_arguments(
        self,
    ):
        self.host._translate_image_content = AsyncMock(return_value="Hello")
        result = must(await self.host._render_items([item()], self.umo))
        self.assertEqual(len(result.chain), 3)
        self.assertEqual(result.chain[1].type, ComponentType.Image)
        self.assertEqual(result.chain[2].text, "\n图片文字翻译：\nHello")
        self.host._translate_image_content.assert_awaited_once_with(
            self.images[0], self.umo, "English", "vision-model"
        )

    async def test_each_page_keeps_its_translation_in_nested_multi_work_forward(self):
        self.host._download.side_effect = self.images
        self.host._translate_image_content = AsyncMock(
            side_effect=[f"Translation {index}" for index in range(4)]
        )
        result = must(
            await self.host._render_items(
                [item("7", pages=2), item("8", pages=2)], self.umo, bot_id="123"
            )
        )
        self.assertIsInstance(result.chain[0], Nodes)
        self.assertEqual(len(result.chain[0].nodes), 2)
        pages = page_nodes(result.chain[0].nodes)
        self.assertEqual(len(pages), 4)
        for index, page in enumerate(pages):
            self.assertEqual(len(page.content), 2)
            self.assertEqual(page.content[0].type, ComponentType.Image)
            self.assertEqual(page.content[0].file, self.images[index].as_uri())
            self.assertEqual(
                page.content[1].text, f"\n图片文字翻译：\nTranslation {index}"
            )
        self.assertEqual(
            [
                call.args[0]
                for call in self.host._translate_image_content.await_args_list
            ],
            self.images,
        )

    async def test_explicit_page_only_translates_selected_original(self):
        self.host._download.return_value = self.images[2]
        self.host._translate_image_content = AsyncMock(return_value="Page three")
        work = {**item(pages=4), "_browse_page": 2}
        result = must(await self.host._render_items([work], self.umo))
        self.assertEqual(awaited(self.host._download).args[2], 2)
        self.assertEqual(
            awaited(self.host._translate_image_content).args[0], self.images[2]
        )
        self.assertEqual(result.chain[-1].text, "\n图片文字翻译：\nPage three")

    async def test_restricted_links_download_original_for_ocr_but_still_emit_links(
        self,
    ):
        self.host._content_policy.return_value = (False, "link")
        self.host._download.side_effect = self.images[:2]
        self.host._translate_image_content = AsyncMock(side_effect=["First", "Second"])
        result = must(
            await self.host._render_items([item(pages=2, restriction=1)], self.umo)
        )
        pages = page_nodes(result.chain[0].nodes)
        self.assertEqual(len(pages), 2)
        for index, page in enumerate(pages):
            self.assertTrue(all(isinstance(part, Plain) for part in page.content))
            self.assertIn("https://i.pximg.net/7.png", page.content[0].text)
            self.assertEqual(
                page.content[-1].text,
                "\n图片文字翻译：\n" + ("First" if index == 0 else "Second"),
            )
        self.assertEqual(self.host._download.await_count, 2)
        self.assertTrue(
            all(
                call.kwargs["source_only"]
                for call in self.host._download.await_args_list
            )
        )

    async def test_disabled_translation_adds_no_provider_calls_or_link_downloads(self):
        self.host.cfg["content_translation"]["enable"] = False
        self.host._translate_image_content = AsyncMock()
        self.host._content_policy.return_value = (False, "link")
        result = must(await self.host._render_items([item(restriction=1)], self.umo))
        self.assertEqual(len(result.chain), 1)
        self.host._download.assert_not_awaited()
        self.host._translate_image_content.assert_not_awaited()
        self.context.get_current_chat_provider_id.assert_not_awaited()
        self.context.llm_generate.assert_not_awaited()
        result = must(await self.host._render_items([item()], self.umo))
        self.assertEqual(len(result.chain), 2)
        self.host._download.assert_awaited_once()

    async def test_empty_translation_omits_extra_plain(self):
        self.context.llm_generate.return_value.completion_text = '{"translation":""}'
        result = must(await self.host._render_items([item()], self.umo))
        self.assertEqual(len(result.chain), 2)
        self.assertEqual(result.chain[-1].type, ComponentType.Image)

    async def test_provider_and_format_failures_degrade_per_page_and_continue(self):
        for failure in (
            RuntimeError("Provider unavailable"),
            ValueError("Invalid JSON"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.host._translate_image_content = AsyncMock(
                    side_effect=[failure, "OK"]
                )
                result = must(
                    await self.host._render_items(
                        [item(pages=2)], self.umo, bot_id="123"
                    )
                )
                pages = page_nodes(result.chain[0].nodes)
                self.assertEqual(pages[0].content[-1].text, "\n图片文字翻译暂不可用")
                self.assertEqual(pages[1].content[-1].text, "\n图片文字翻译：\nOK")
                self.host.log.warning.assert_called()
                self.assertTrue(
                    all(
                        ord(char) < 128
                        for char in self.host.log.warning.call_args.args[0]
                    )
                )

    async def test_link_ocr_download_failure_still_emits_original_link(self):
        self.host._content_policy.return_value = (False, "link")
        self.host._download.side_effect = RuntimeError("OCR download unavailable")
        self.host._translate_image_content = AsyncMock()
        result = must(await self.host._render_items([item(restriction=1)], self.umo))
        self.assertIn("https://i.pximg.net/7.png", result.chain[0].text)
        self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")
        self.host._translate_image_content.assert_not_awaited()

    async def test_link_download_await_rechecks_policy_before_model(self):
        self.host._content_policy.return_value = (False, "link")
        self.host._translate_image_content = AsyncMock(return_value="Must not run")

        async def change_policy(*args, **kwargs):
            self.host._content_policy.return_value = (True, "link")
            return self.images[0]

        self.host._download.side_effect = change_policy
        with self.assertRaisesRegex(RuntimeError, "policy changed"):
            await self.host._render_items([item(restriction=1)], self.umo)
        self.host._translate_image_content.assert_not_awaited()
        self.context.send_message.assert_not_awaited()

    async def test_real_provider_lookup_and_invalid_response_degrade_to_image(self):
        for stage in ("lookup", "generation", "json"):
            with self.subTest(stage=stage):
                self.host.cfg["content_translation"]["provider_id"] = ""
                self.context.get_current_chat_provider_id = AsyncMock(
                    return_value="group-model",
                    side_effect=RuntimeError("No provider")
                    if stage == "lookup"
                    else None,
                )
                self.context.llm_generate = AsyncMock(
                    return_value=SimpleNamespace(completion_text="invalid JSON"),
                    side_effect=RuntimeError("Model unavailable")
                    if stage == "generation"
                    else None,
                )
                result = must(await self.host._render_items([item()], self.umo))
                self.assertEqual(result.chain[1].type, ComponentType.Image)
                self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")

    async def test_timeout_covers_provider_lookup_and_generation(self):
        original_wait_for = asyncio.wait_for
        for stage in ("lookup", "generation"):
            with self.subTest(stage=stage):
                self.host.cfg["content_translation"]["provider_id"] = ""
                blocked = asyncio.Event()
                cancelled = asyncio.Event()

                async def slow(*args, **kwargs):
                    try:
                        await blocked.wait()
                    finally:
                        cancelled.set()

                self.context.get_current_chat_provider_id = AsyncMock(
                    side_effect=slow if stage == "lookup" else None,
                    return_value="group-model",
                )
                self.context.llm_generate = AsyncMock(
                    side_effect=slow if stage == "generation" else None
                )

                async def short_timeout(awaitable, timeout):
                    self.assertEqual(timeout, 60)
                    return await original_wait_for(awaitable, timeout=0.01)

                with patch.object(
                    browse.asyncio, "wait_for", side_effect=short_timeout
                ):
                    result = must(await self.host._render_items([item()], self.umo))
                self.assertTrue(cancelled.is_set())
                self.assertEqual(result.chain[-1].text, "\n图片文字翻译暂不可用")

    async def test_cancellation_propagates_without_fallback_message(self):
        self.host._translate_image_content = AsyncMock(
            side_effect=asyncio.CancelledError
        )
        with self.assertRaises(asyncio.CancelledError):
            await self.host._render_items([item()], self.umo)
        self.host.log.warning.assert_not_called()
        self.context.send_message.assert_not_awaited()

    async def test_translation_await_rechecks_delivery_and_policy(self):
        for mutation, error in (
            ("stopped", "delivery stopped"),
            ("disabled", "delivery stopped"),
            ("subscription", "delivery stopped"),
            ("policy", "policy changed"),
        ):
            with self.subTest(mutation=mutation):
                active = True
                self.host._stopped = False
                self.host.enabled = True
                self.host._content_policy.return_value = (True, "link")

                async def translate(*args):
                    nonlocal active
                    if mutation == "stopped":
                        self.host._stopped = True
                    elif mutation == "disabled":
                        self.host.enabled = False
                    elif mutation == "subscription":
                        active = False
                    else:
                        self.host._content_policy.return_value = (False, "image")
                    return "Must not send"

                self.host._translate_image_content = AsyncMock(side_effect=translate)
                with self.assertRaisesRegex(RuntimeError, error):
                    await self.host._render_items(
                        [item()], self.umo, delivery_active=lambda: active
                    )
                self.context.send_message.assert_not_awaited()
