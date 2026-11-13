"""Offline image-mode contracts; no Redis, network, or model is required."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_group_chat as scaffolding
from juhkff_modules import group_chat

from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.config.runtime import MessageConfig, current_message_config
from astrbot.core.config.snapshot import AstrBotConfigSnapshot


class GroupChatImageTests(scaffolding.GroupChatTests):
    # Reuse fixture/helpers without collecting the inherited original tests twice.
    for _name in tuple(dir(scaffolding.GroupChatTests)):
        if _name.startswith("test_"):
            locals()[_name] = None

    def setUp(self):
        super().setUp()
        self.snapshot = AstrBotConfigSnapshot(self.core_config, {})
        self.plugin.context.get_config = lambda **kwargs: self.snapshot
        self.plugin.context.get_using_provider_async = AsyncMock(
            return_value=SimpleNamespace(provider_config={"id": "offline-vision"})
        )
        self.module.cfg["image_caption_provider_id"] = "offline-caption"
        vision = SimpleNamespace(provider_config={"id": "offline-vision"})
        for target, kwargs in (
            (
                "select_chat_provider",
                {"new_callable": AsyncMock, "return_value": vision},
            ),
            ("resolve_fallback_providers", {"return_value": []}),
            ("select_image_provider", {"return_value": vision}),
            (
                "prepare_request_images",
                {"new_callable": AsyncMock, "return_value": None},
            ),
        ):
            patcher = patch.object(group_chat, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "astrbot.core.provider.capabilities.supports_modality", return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def event(self, *args, **kwargs):
        event = super().event(*args, **kwargs)
        token = current_message_config.set(
            MessageConfig(event.unified_msg_origin, self.snapshot, None, frozenset())
        )
        self.addCleanup(current_message_config.reset, token)
        return event

    @staticmethod
    def image(payload="aGVsbG8="):
        return Image.fromBase64(payload)

    @staticmethod
    def image_refs(req):
        refs = [
            p.image_url.url
            for p in req.extra_user_content_parts
            if isinstance(p, ImageURLPart)
        ]
        for message in req.contexts:
            if isinstance(message["content"], list):
                refs.extend(
                    p["image_url"]["url"]
                    for p in message["content"]
                    if p["type"] == "image_url"
                )
        return [
            "base64://" + ref.split(",", 1)[1] if ref.startswith("data:") else ref
            for ref in refs
        ]

    @staticmethod
    def text(req):
        text = [p.text for p in req.extra_user_content_parts if isinstance(p, TextPart)]
        for message in req.contexts:
            content = message["content"]
            text.extend(
                [content]
                if isinstance(content, str)
                else [p["text"] for p in content if p["type"] == "text"]
            )
        return "\n".join(text)

    async def caption(self, event, req, *args, **kwargs):
        self.assertEqual(req.image_urls, event.get_extra("juhkff_group_current_images"))
        self.before_caption = deepcopy(self.module.store.records)
        refs = set(req.image_urls)
        req.image_urls = []
        req.extra_user_content_parts.append(TextPart(text="CURRENT_CAPTION_TOKEN"))
        return refs

    def assert_caption_saved(self, before, event, caption="CURRENT_CAPTION_TOKEN"):
        expected = deepcopy(before)
        rows = expected[event.unified_msg_origin]
        row = next(
            row for row in rows if row[0] == event.get_extra("juhkff_group_record_id")
        )
        record = json.loads(row[2])
        record["caption"] = caption
        record["image_analysis"] = event.get_extra("juhkff_group_image_analysis")
        row[2] = json.dumps(record, ensure_ascii=False)
        actual = deepcopy(self.module.store.records)
        # JSON whitespace is not part of the persisted record contract.
        for records in (expected, actual):
            for entries in records.values():
                for entry in entries:
                    if entry[2].startswith("{"):
                        entry[2] = json.loads(entry[2])
        self.assertEqual(actual, expected)

    async def test_prepare_preserves_full_base64_json_and_plain_legacy_record(self):
        payload = "A" * 6000
        prior = self.event("plain legacy")
        await self.module.prepare(prior)
        event = self.event(
            "IMAGE_TEXT", components=[Plain("IMAGE_TEXT"), self.image(payload)]
        )
        with patch.object(
            group_chat, "caption_request_images", new_callable=AsyncMock
        ) as caption:
            await self.module.prepare(event)
        caption.assert_not_awaited()
        rows = self.module.store.records[event.unified_msg_origin]
        self.assertIsInstance(rows[0][2], str)
        self.assertIn("plain legacy", rows[0][2])
        self.assertEqual(json.loads(rows[0][2])["role"], "user")
        record = json.loads(rows[1][2])
        self.assertEqual(record["version"], 2)
        self.assertIn("IMAGE_TEXT", record["text"])
        self.assertEqual(record["images"], ["base64://" + payload])
        self.assertEqual(
            event.get_extra("juhkff_group_current_images"), record["images"]
        )

    async def test_prepare_retains_full_url_and_converts_only_local_image(self):
        payload = "B" * 6000
        url = "https://offline.invalid/image.png?" + "q" * 3000
        data_uri = "data:image/png;base64," + payload
        cases = (
            (Image.fromURL(url), url, False),
            (Image(file=data_uri), data_uri, False),
            (Image.fromFileSystem("/offline/image.png"), "base64://" + payload, True),
        )
        for component, expected, converted in cases:
            with self.subTest(source=component.file):
                with patch.object(
                    Image,
                    "convert_to_base64",
                    new_callable=AsyncMock,
                    return_value=payload,
                ) as convert:
                    event = self.event(components=[component])
                    await self.module.prepare(event)
                if converted:
                    convert.assert_awaited_once()
                else:
                    convert.assert_not_awaited()
                self.assertEqual(
                    event.get_extra("juhkff_group_current_images"), [expected]
                )
                record = json.loads(
                    self.module.store.records[event.unified_msg_origin][-1][2]
                )
                self.assertEqual(record["images"], [expected])

    async def test_caption_runs_before_probability_and_admit_does_not_repeat(self):
        self.module.cfg.update(image_mode="caption", probability=0, cooldown=999999)
        event = self.event("question", components=[self.image()])
        with patch.object(
            group_chat, "caption_request_images", side_effect=self.caption
        ) as caption:
            await self.module.prepare(event)
            caption.assert_awaited_once()
            self.assert_caption_saved(self.before_caption, event)
            await self.module.admit(event)
            self.assertFalse(event.is_at_or_wake_command)
            event.is_at_or_wake_command = True
            await self.module.admit(event)
            caption.assert_awaited_once()
        req = ProviderRequest(prompt="question", image_urls=["base64://RAW"])
        await self.module.decorate(event, req)
        self.assertIn("CURRENT_CAPTION_TOKEN", self.text(req))

    async def test_original_history_multiple_images_keep_sender_order_and_are_temporary(
        self,
    ):
        self.module.cfg["context_messages"] = 10
        first = self.event(
            "FIRST_TEXT",
            sender="sender-a",
            components=[Plain("FIRST_TEXT"), self.image("AAAA"), self.image("BBBB")],
        )
        second = self.event(
            "SECOND_TEXT",
            sender="sender-b",
            components=[Plain("SECOND_TEXT"), self.image("CCCC")],
        )
        await self.module.prepare(first)
        await self.module.prepare(second)
        current = self.event(
            "now", addressed=True, components=[self.image("DDDD"), self.image("EEEE")]
        )
        await self.module.prepare(current)
        current_refs = current.get_extra("juhkff_group_current_images")
        req = ProviderRequest(prompt="now", image_urls=list(current_refs))
        await self.module.decorate(current, req)
        self.assertEqual(req.image_urls, current_refs)
        self.assertEqual(
            self.image_refs(req), ["base64://" + p for p in ("AAAA", "BBBB", "CCCC")]
        )
        self.assertEqual(len(req.contexts), 2)
        for message, sender, expected in zip(
            req.contexts, ("sender-a", "sender-b"), (2, 1)
        ):
            self.assertEqual(message["role"], "user")
            self.assertTrue(message["_no_save"])
            content = message["content"]
            self.assertIn(sender, content[0]["text"])
            self.assertEqual(
                sum(part["type"] == "image_url" for part in content), expected
            )
        self.assertIn("AAAA", str(req.contexts[0]))
        self.assertNotIn("CCCC", str(req.contexts[0]))
        self.assertIn("CCCC", str(req.contexts[1]))
        self.assertNotIn("DDDD", str(req.contexts))
        before = deepcopy(req.contexts)
        await self.module.decorate(current, req)
        self.assertEqual(req.contexts, before)
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_quoted_image_keeps_original_author_in_its_own_history_message(self):
        quoted = Reply(
            id="quote-id",
            sender_id="original-author",
            sender_nickname="Alice",
            chain=[self.image("AAAA")],
        )
        prior = self.event(
            "quote discussion",
            sender="quoting-user",
            components=[quoted, Plain("quote discussion")],
        )
        await self.module.prepare(prior)
        current = self.event("latest", addressed=True, sender="another-user")
        await self.module.prepare(current)
        request = ProviderRequest(prompt="latest")
        await self.module.decorate(current, request)
        self.assertEqual(len(request.contexts), 1)
        content = request.contexts[0]["content"]
        text = "\\n".join(p["text"] for p in content if p["type"] == "text")
        self.assertIn("Alice", text)
        self.assertIn("original-author", text)
        self.assertIn("引用图片", text)
        self.assertEqual(self.image_refs(request), ["base64://AAAA"])
        self.assertEqual(request.image_urls, [])
        self.assertNotIn("original-author", request.prompt)

    async def test_caption_ignores_history_images_and_switch_restores_originals(self):
        history = self.event(
            "HISTORY_TEXT",
            sender="history-sender",
            components=[Plain("HISTORY_TEXT"), self.image("AAAA"), self.image("BBBB")],
        )
        await self.module.prepare(history)
        self.module.cfg["image_mode"] = "caption"
        current = self.event(
            "now", addressed=True, components=[self.image("CCCC"), self.image("DDDD")]
        )
        with patch.object(
            group_chat, "caption_request_images", side_effect=self.caption
        ):
            await self.module.prepare(current)
            before = self.before_caption
            await self.module.admit(current)
        req = ProviderRequest(
            prompt="now",
            image_urls=["current-image"],
            extra_user_content_parts=[
                ImageURLPart(image_url=ImageURLPart.ImageURL(url="extra-image"))
            ],
        )
        await self.module.decorate(current, req)
        await self.module.decorate(current, req)
        self.assertFalse(req.image_urls)
        self.assertFalse(self.image_refs(req))
        self.assertIn("HISTORY_TEXT", self.text(req))
        self.assertIn("history-sender", self.text(req))
        self.assertEqual(self.text(req).count("CURRENT_CAPTION_TOKEN"), 1)
        self.assert_caption_saved(before, current)
        saved = deepcopy(self.module.store.records)
        self.module.cfg["image_mode"] = "original"
        restored = ProviderRequest(prompt="now")
        await self.module.decorate(current, restored)
        self.assertEqual(self.image_refs(restored), ["base64://AAAA", "base64://BBBB"])
        self.assertEqual(self.module.store.records, saved)

    async def test_redis_failure_retains_current_multiple_images(self):
        event = self.event(
            "now", addressed=True, components=[self.image("AAAA"), self.image("BBBB")]
        )
        with patch.object(
            self.module.store,
            "snapshot_append",
            new_callable=AsyncMock,
            side_effect=ConnectionError("offline Redis"),
        ):
            await self.module.prepare(event)
        self.assertEqual(
            event.get_extra("juhkff_group_current_images"),
            ["base64://AAAA", "base64://BBBB"],
        )
        req = ProviderRequest(
            prompt="now",
            image_urls=list(event.get_extra("juhkff_group_current_images")),
        )
        await self.module.decorate(event, req)
        self.assertEqual(len(req.image_urls), 2)
        self.assertFalse(event.get_extra("juhkff_group_context"))
        self.assertFalse(self.module.store.records)

    async def test_current_and_reply_multiple_images_are_retained_without_duplicates(
        self,
    ):
        first, second = self.image("AAAA"), self.image("BBBB")
        event = self.event(
            components=[first, Reply(id="quoted", chain=[second, first]), second]
        )
        await self.module.prepare(event)
        self.assertEqual(
            event.get_extra("juhkff_group_current_images"),
            ["base64://AAAA", "base64://BBBB"],
        )

    async def test_admit_only_changes_matching_independent_config_snapshot(self):
        self.core_config["provider_settings"]["default_image_caption_provider_id"] = (
            "global-caption"
        )
        original = deepcopy(self.core_config)
        event = self.event(addressed=True, components=[self.image()])
        snapshot = AstrBotConfigSnapshot(self.core_config, {})
        self.plugin.context.get_config = lambda **kwargs: snapshot
        scope = MessageConfig(event.unified_msg_origin, snapshot, None, frozenset())
        token = current_message_config.set(scope)
        try:
            await self.module.prepare(event)
            self.assertEqual(self.core_config, original)
            self.assertEqual(
                snapshot["provider_settings"]["default_image_caption_provider_id"],
                "global-caption",
            )
            await self.module.admit(event)
        finally:
            current_message_config.reset(token)
        self.assertEqual(
            snapshot["provider_settings"]["default_image_caption_provider_id"], ""
        )
        self.assertEqual(self.core_config, original)
        self.assertEqual(
            event.get_extra("juhkff_group_caption_provider"), "global-caption"
        )

    async def test_caption_receives_prepared_montage_without_changing_raw_storage(self):
        self.module.cfg["image_mode"] = "caption"
        event = self.event("question", addressed=True, components=[self.image()])
        order = []

        async def prepare(req, owner, *, prepared, finalize, **kwargs):
            self.assertIs(owner, event)
            self.assertFalse(finalize)
            order.append("prepare")
            req.image_urls = ["/offline/prepared-montage.png"]
            prepared["raw"] = {"path": req.image_urls[0], "montage": True}

        async def caption(owner, req, settings, context, provider_id, *, montage_refs):
            self.before_caption = deepcopy(self.module.store.records)
            order.append("caption")
            self.assertEqual(req.image_urls, ["/offline/prepared-montage.png"])
            self.assertEqual(montage_refs, {"/offline/prepared-montage.png"})
            req.extra_user_content_parts.append(TextPart(text="MONTAGE_CAPTION"))
            return set(req.image_urls)

        with (
            patch.object(group_chat, "prepare_request_images", side_effect=prepare),
            patch.object(group_chat, "caption_request_images", side_effect=caption),
        ):
            await self.module.prepare(event)
            before = self.before_caption
            await self.module.admit(event)
        self.assertEqual(order, ["prepare", "caption"])
        self.assert_caption_saved(before, event, "MONTAGE_CAPTION")
        self.assertEqual(
            event.get_extra("juhkff_group_current_images"), ["base64://aGVsbG8="]
        )

    async def test_caption_without_provider_delegates_failure_handling_to_core(self):
        self.module.cfg["image_mode"] = "caption"
        self.module.cfg["image_caption_provider_id"] = ""
        self.snapshot["provider_settings"]["default_image_caption_provider_id"] = ""
        self.plugin.context.get_using_provider_async.return_value = None
        event = self.event("question", addressed=True, components=[self.image()])
        with patch.object(
            group_chat, "caption_request_images", side_effect=self.caption
        ) as caption:
            await self.module.prepare(event)
            await self.module.admit(event)
        self.assertEqual(caption.call_args.args[4], "")

    async def test_quoted_platform_fallback_images_are_stored_and_captioned(self):
        self.module.cfg["image_mode"] = "caption"
        refs = ["https://offline.invalid/quoted.png", "base64://AAAA"]
        event = self.event("reply", addressed=True, components=[Reply(id="quoted")])
        with (
            patch.object(
                group_chat,
                "extract_quoted_message_images",
                new_callable=AsyncMock,
                return_value=refs,
            ) as extract,
            patch.object(
                group_chat, "caption_request_images", side_effect=self.caption
            ) as caption,
        ):
            await self.module.prepare(event)
            await self.module.admit(event)
        extract.assert_awaited_once()
        caption.assert_awaited_once()
        self.assertEqual(event.get_extra("juhkff_group_current_images"), refs)
        self.assertEqual(caption.call_args.kwargs["montage_refs"], set())
        self.assert_caption_saved(self.before_caption, event)
        req = ProviderRequest(prompt="reply")
        await self.module.decorate(event, req)
        self.assertIn("CURRENT_CAPTION_TOKEN", self.text(req))

    async def test_real_image_preparation_preserves_temporary_history_image_parts(self):
        from astrbot.core.utils.image_input import prepare_request_images

        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a40sAAAAASUVORK5CYII="
        prior = self.event("tiny image", components=[self.image(png)])
        await self.module.prepare(prior)
        current = self.event("question", addressed=True)
        await self.module.prepare(current)
        before = deepcopy(self.module.store.records)
        req = ProviderRequest(prompt="question")
        with TemporaryDirectory() as directory:

            async def prepare(history_req, event, **kwargs):
                await prepare_request_images(
                    history_req, event, output_dir=Path(directory), **kwargs
                )

            with patch.object(
                group_chat, "prepare_request_images", side_effect=prepare
            ):
                await self.module.decorate(current, req)
        self.assertEqual(len(req.contexts), 1)
        message = req.contexts[0]
        self.assertTrue(message["_no_save"])
        images = [part for part in message["content"] if part["type"] == "image_url"]
        self.assertEqual(len(images), 1)
        self.assertTrue(
            images[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(self.module.store.records, before)

    async def test_later_consumed_events_keep_inbound_caption_without_snapshot_override(
        self,
    ):
        self.module.cfg["image_mode"] = "caption"
        for consumed in ("stopped", "sent", "provider_request"):
            with self.subTest(consumed=consumed):
                event = self.event(addressed=True, components=[self.image()])
                self.snapshot["provider_settings"][
                    "default_image_caption_provider_id"
                ] = "keep"
                with patch.object(
                    group_chat, "caption_request_images", side_effect=self.caption
                ) as caption:
                    await self.module.prepare(event)
                    self.assert_caption_saved(self.before_caption, event)
                    if consumed == "stopped":
                        event.stop_event()
                    elif consumed == "sent":
                        event._has_send_oper = True
                    else:
                        event.set_extra(
                            "provider_request", ProviderRequest(prompt="other plugin")
                        )
                    await self.module.admit(event)
                    caption.assert_awaited_once()
                self.assertEqual(
                    self.snapshot["provider_settings"][
                        "default_image_caption_provider_id"
                    ],
                    "keep",
                )

    async def test_original_admission_selects_vision_fallback_for_history_images(self):
        await self.module.prepare(self.event(components=[self.image()]))
        event = self.event("text question", addressed=True)
        await self.module.prepare(event)
        text_provider = SimpleNamespace(provider_config={"id": "text"})
        vision_provider = SimpleNamespace(provider_config={"id": "vision"})
        with (
            patch.object(
                group_chat,
                "select_chat_provider",
                new_callable=AsyncMock,
                return_value=text_provider,
            ),
            patch.object(
                group_chat, "resolve_fallback_providers", return_value=[vision_provider]
            ) as fallback,
            patch.object(
                group_chat, "select_image_provider", return_value=vision_provider
            ) as select,
            patch(
                "astrbot.core.provider.capabilities.supports_modality",
                return_value=True,
            ),
            patch.object(
                group_chat, "caption_request_images", new_callable=AsyncMock
            ) as caption,
        ):
            await self.module.admit(event)
        fallback.assert_called_once()
        self.assertTrue(select.call_args.args[1].image_urls)
        self.assertEqual(event.get_extra("selected_provider"), "vision")
        caption.assert_not_awaited()

    async def test_original_admission_requires_vision_for_current_images(self):
        event = self.event("question", addressed=True, components=[self.image()])
        await self.module.prepare(event)
        provider = SimpleNamespace(provider_config={"id": "text"})
        with (
            patch.object(
                group_chat,
                "select_chat_provider",
                new_callable=AsyncMock,
                return_value=provider,
            ),
            patch.object(group_chat, "resolve_fallback_providers", return_value=[]),
            patch.object(group_chat, "select_image_provider", return_value=provider),
            patch(
                "astrbot.core.provider.capabilities.supports_modality",
                return_value=False,
            ),
        ):
            self.assertIsNone(await self.module.admit(event))
        self.assertTrue(event.call_llm)
        self.assertIsNone(event.get_extra("selected_provider"))

    async def test_original_text_only_does_not_resolve_image_provider(self):
        event = self.event("question", addressed=True)
        await self.module.prepare(event)
        with patch.object(
            group_chat, "select_chat_provider", new_callable=AsyncMock
        ) as select:
            await self.module.admit(event)
        select.assert_not_awaited()

    async def test_admit_does_not_change_unrelated_or_inactive_snapshot(self):
        for matching, active in ((False, True), (True, False)):
            with self.subTest(matching=matching, active=active):
                event = self.event(addressed=True, components=[self.image()])
                snapshot = AstrBotConfigSnapshot(self.core_config, {})
                snapshot["provider_settings"]["default_image_caption_provider_id"] = (
                    "keep"
                )
                self.plugin.context.get_config = lambda **kwargs: snapshot
                scope = MessageConfig(
                    event.unified_msg_origin if matching else "other-group",
                    snapshot,
                    None,
                    frozenset(),
                    active,
                )
                token = current_message_config.set(scope)
                try:
                    await self.module.prepare(event)
                    await self.module.admit(event)
                finally:
                    current_message_config.reset(token)
                self.assertEqual(
                    snapshot["provider_settings"]["default_image_caption_provider_id"],
                    "keep",
                )

    async def test_admit_never_mutates_global_config_even_if_scope_references_it(self):
        self.core_config["provider_settings"]["default_image_caption_provider_id"] = (
            "global-caption"
        )
        before = deepcopy(self.core_config)
        self.plugin.context.get_config = lambda **kwargs: self.core_config
        event = self.event(addressed=True, components=[self.image()])
        scope = MessageConfig(
            event.unified_msg_origin, self.core_config, None, frozenset()
        )
        token = current_message_config.set(scope)
        try:
            await self.module.prepare(event)
            await self.module.admit(event)
        finally:
            current_message_config.reset(token)
        self.assertEqual(self.core_config, before)

    async def test_saved_caption_is_history_next_turn_and_original_mode_keeps_raw_images(
        self,
    ):
        self.module.cfg["image_mode"] = "caption"
        first = self.event(
            "PICTURE_CONTEXT",
            addressed=True,
            sender="image-sender",
            components=[
                Plain("PICTURE_CONTEXT"),
                self.image("AAAA"),
                self.image("BBBB"),
            ],
        )
        with patch.object(
            group_chat, "caption_request_images", side_effect=self.caption
        ):
            await self.module.prepare(first)
            before = self.before_caption
            await self.module.admit(first)
        self.assert_caption_saved(before, first)
        first_request = ProviderRequest(prompt="PICTURE_CONTEXT")
        await self.module.decorate(first, first_request)
        self.assertEqual(self.text(first_request).count("CURRENT_CAPTION_TOKEN"), 1)

        follow_up = self.event("what was in that picture?", addressed=True)
        await self.module.prepare(follow_up)
        with patch.object(
            group_chat, "caption_request_images", new_callable=AsyncMock
        ) as caption:
            await self.module.admit(follow_up)
        caption.assert_not_awaited()
        request = ProviderRequest(prompt=follow_up.message_str)
        await self.module.decorate(follow_up, request)
        text = self.text(request)
        self.assertIn("image-sender", text)
        self.assertIn("PICTURE_CONTEXT", text)
        self.assertIn("[图片描述] CURRENT_CAPTION_TOKEN", text)
        self.assertEqual(text.count("CURRENT_CAPTION_TOKEN"), 1)
        self.assertFalse(request.image_urls)
        self.assertFalse(self.image_refs(request))

        saved = deepcopy(self.module.store.records)
        self.module.cfg["image_mode"] = "original"
        original_request = ProviderRequest(prompt=follow_up.message_str)
        await self.module.decorate(follow_up, original_request)
        self.assertEqual(
            self.image_refs(original_request), ["base64://AAAA", "base64://BBBB"]
        )
        self.assertNotIn("CURRENT_CAPTION_TOKEN", self.text(original_request))
        self.assertEqual(self.module.store.records, saved)

    async def test_failed_caption_does_not_persist_failure_as_history(self):
        self.module.cfg["image_mode"] = "caption"
        event = self.event("question", addressed=True, components=[self.image()])

        async def fail_caption(owner, req, *args, **kwargs):
            self.before_caption = deepcopy(self.module.store.records)
            req.image_urls = []
            req.extra_user_content_parts.append(
                TextPart(text="[Image Captioning Failed]")
            )
            return set()

        with patch.object(
            group_chat, "caption_request_images", side_effect=fail_caption
        ):
            await self.module.prepare(event)
            await self.module.admit(event)
        self.assertEqual(self.module.store.records, self.before_caption)
        record = json.loads(self.module.store.records[event.unified_msg_origin][-1][2])
        self.assertNotIn("caption", record)
        self.assertNotIn("image_analysis", record)
        request = ProviderRequest(prompt="question")
        await self.module.decorate(event, request)
        self.assertIn("[Image Captioning Failed]", self.text(request))
        next_event = self.event("next", addressed=True)
        await self.module.prepare(next_event)
        next_request = ProviderRequest(prompt="next")
        await self.module.decorate(next_event, next_request)
        self.assertNotIn("[Image Captioning Failed]", self.text(next_request))

    async def test_caption_persistence_failure_keeps_current_caption_without_recreating_record(
        self,
    ):
        self.module.cfg["image_mode"] = "caption"
        for deleted, failure in (
            (False, False),
            (True, False),
            (False, ConnectionError("offline Redis")),
        ):
            with self.subTest(deleted=deleted, failure=failure):
                event = self.event(
                    "question", addressed=True, components=[self.image()]
                )
                original_replace = self.module.store.replace
                before = None

                async def replace(umo, record_id, record):
                    nonlocal before
                    if "caption" not in json.loads(record):
                        return await original_replace(umo, record_id, record)
                    if deleted:
                        self.module.store.records[umo].clear()
                    before = deepcopy(self.module.store.records)
                    if isinstance(failure, Exception):
                        raise failure
                    return False

                with (
                    patch.object(
                        group_chat, "caption_request_images", side_effect=self.caption
                    ),
                    patch.object(
                        self.module.store, "replace", side_effect=replace
                    ) as update,
                    patch.object(
                        self.module.store,
                        "snapshot_append",
                        wraps=self.module.store.snapshot_append,
                    ) as append,
                ):
                    await self.module.prepare(event)
                    await self.module.admit(event)
                append.assert_awaited_once()
                self.assertEqual(update.await_count, 2)
                self.assertEqual(
                    update.call_args.args[:2],
                    (
                        event.unified_msg_origin,
                        event.get_extra("juhkff_group_record_id"),
                    ),
                )
                self.assertEqual(
                    json.loads(update.call_args.args[2])["caption"],
                    "CURRENT_CAPTION_TOKEN",
                )
                self.assertEqual(self.module.store.records, before)
                request = ProviderRequest(prompt="question")
                await self.module.decorate(event, request)
                self.assertIn("CURRENT_CAPTION_TOKEN", self.text(request))
                self.assertFalse(request.image_urls)
                self.assertFalse(self.image_refs(request))

    async def test_disabled_meme_detection_keeps_plain_caption_and_raw_images_across_turns(
        self,
    ):
        self.module.cfg.update(
            image_mode="caption",
            probability=0,
            caption_settings={"detect_meme": False, "provider_id": ""},
        )
        original_settings = deepcopy(self.snapshot["provider_settings"])
        original_settings["image_caption_prompt"] = "Describe visible image contents."
        self.snapshot["provider_settings"] = deepcopy(original_settings)
        for raw in (
            "A cat sitting next to a terminal.",
            json.dumps(
                {"images": [{"is_meme": True, "description": "JSON stays literal"}]}
            ),
        ):
            with self.subTest(caption=raw):
                event = self.event(
                    "PICTURE_CONTEXT",
                    sender="image-sender",
                    components=[Plain("PICTURE_CONTEXT"), self.image("AAAA")],
                )

                async def caption(owner, req, settings, *args, **kwargs):
                    self.assertEqual(settings, original_settings)
                    self.assertEqual(req.image_urls, ["base64://AAAA"])
                    req.extra_user_content_parts.append(TextPart(text=raw))
                    return set(req.image_urls)

                with patch.object(
                    group_chat, "caption_request_images", side_effect=caption
                ) as describe:
                    await self.module.prepare(event)
                    describe.assert_awaited_once()
                    await self.module.admit(event)
                    self.assertFalse(event.is_at_or_wake_command)
                    self.assertFalse(event.get_extra("juhkff_group_image_analysis"))
                    record = json.loads(
                        self.module.store.records[event.unified_msg_origin][-1][2]
                    )
                    self.assertEqual(record["caption"], raw)
                    self.assertEqual(record["images"], ["base64://AAAA"])
                    self.assertNotIn("image_analysis", record)
                    current = ProviderRequest(prompt=event.message_str)
                    await self.module.decorate(event, current)
                    self.assertIn(raw, self.text(current))
                    self.assertNotIn("[图片1·", self.text(current))

                    follow_up = self.event("what was in that image?", addressed=True)
                    await self.module.prepare(follow_up)
                    await self.module.admit(follow_up)
                    describe.assert_awaited_once()
                request = ProviderRequest(prompt=follow_up.message_str)
                await self.module.decorate(follow_up, request)
                self.assertIn("image-sender", self.text(request))
                self.assertIn(raw, self.text(request))
                self.assertNotIn("[图片1·", self.text(request))
                self.assertFalse(request.image_urls)
                self.assertFalse(self.image_refs(request))
                self.assertEqual(self.snapshot["provider_settings"], original_settings)

                self.module.cfg["image_mode"] = "original"
                restored = ProviderRequest(prompt=follow_up.message_str)
                await self.module.decorate(follow_up, restored)
                self.assertIn("base64://AAAA", self.image_refs(restored))
                self.module.cfg["image_mode"] = "caption"

    async def test_original_mode_never_analyzes_images_even_when_meme_detection_enabled(
        self,
    ):
        self.module.cfg.update(
            image_mode="original",
            caption_settings={"detect_meme": True, "provider_id": "nested-caption"},
        )
        event = self.event("original image", addressed=True, components=[self.image()])
        with patch.object(
            group_chat, "caption_request_images", new_callable=AsyncMock
        ) as describe:
            await self.module.prepare(event)
            await self.module.admit(event)
            request = ProviderRequest(
                prompt=event.message_str,
                image_urls=list(event.get_extra("juhkff_group_current_images")),
            )
            await self.module.decorate(event, request)
        describe.assert_not_awaited()
        self.assertEqual(request.image_urls, ["base64://aGVsbG8="])
        self.assertIsNone(event.get_extra("juhkff_group_image_analysis"))
        record = json.loads(self.module.store.records[event.unified_msg_origin][-1][2])
        self.assertNotIn("caption", record)
        self.assertNotIn("image_analysis", record)

    async def test_mixed_image_classification_is_ordered_and_reused_next_turn(self):
        self.module.cfg["image_mode"] = "caption"
        self.module.cfg["caption_settings"] = {"detect_meme": True}
        analysis = [
            {"is_meme": True, "description": "捂脸表情，表达无奈"},
            {"is_meme": False, "description": "终端报错截图"},
            {"is_meme": None, "description": "模糊的卡通头像"},
        ]
        expected = "\n".join(
            [
                "[图片1·表情包] 捂脸表情，表达无奈",
                "[图片2·普通图片] 终端报错截图",
                "[图片3·图片，类型未确定] 模糊的卡通头像",
            ]
        )
        event = self.event(
            "mixed",
            components=[self.image("AAAA"), self.image("BBBB"), self.image("CCCC")],
        )
        original_settings = deepcopy(self.snapshot["provider_settings"])

        async def caption(owner, req, settings, *args, **kwargs):
            self.before_caption = deepcopy(self.module.store.records)
            self.assertIn('"is_meme"', settings["image_caption_prompt"])
            self.assertIn("按输入顺序", settings["image_caption_prompt"])
            refs = set(req.image_urls)
            req.extra_user_content_parts.append(
                TextPart(
                    text="<image_caption>"
                    + json.dumps({"images": analysis}, ensure_ascii=False)
                    + "</image_caption>"
                )
            )
            return refs

        with patch.object(
            group_chat, "caption_request_images", side_effect=caption
        ) as describe:
            await self.module.prepare(event)
            describe.assert_awaited_once()
            self.assertEqual(self.snapshot["provider_settings"], original_settings)
            self.assertEqual(event.get_extra("juhkff_group_image_analysis"), analysis)
            self.assert_caption_saved(self.before_caption, event, expected)
            current = ProviderRequest(prompt="mixed")
            await self.module.decorate(event, current)
            self.assertIn(expected, self.text(current))
            next_event = self.event("what about those images?", addressed=True)
            await self.module.prepare(next_event)
            await self.module.admit(next_event)
            describe.assert_awaited_once()
        next_request = ProviderRequest(prompt=next_event.message_str)
        await self.module.decorate(next_event, next_request)
        self.assertIn(expected, self.text(next_request))
        self.assertFalse(self.image_refs(next_request))

    async def test_invalid_analysis_preserves_raw_description_without_guessing_class(
        self,
    ):
        self.module.cfg["image_mode"] = "caption"
        valid = {"is_meme": True, "description": "looks funny"}
        payloads = [
            "not JSON: a photo with text",
            "{broken JSON",
            json.dumps({"images": []}),
            json.dumps({"images": [valid, valid]}),
            json.dumps({"images": {"0": valid}}),
            json.dumps({"images": [{"is_meme": "false", "description": "photo"}]}),
            json.dumps({"images": [{"is_meme": 1, "description": "photo"}]}),
            json.dumps({"images": [{"is_meme": 0, "description": "photo"}]}),
            json.dumps({"images": [{"is_meme": False, "description": 123}]}),
            json.dumps({"images": [{"is_meme": False, "description": "   "}]}),
            json.dumps({"images": [{"description": "no classifier"}]}),
            json.dumps({"images": [None]}),
            json.dumps([valid]),
            json.dumps(None),
        ]
        for raw in payloads:
            with self.subTest(raw=raw):
                event = self.event(
                    "question", addressed=True, components=[self.image()]
                )

                async def caption(owner, req, *args, **kwargs):
                    self.before_caption = deepcopy(self.module.store.records)
                    req.extra_user_content_parts.append(
                        TextPart(text="<image_caption>" + raw + "</image_caption>")
                    )
                    return set(req.image_urls)

                with patch.object(
                    group_chat, "caption_request_images", side_effect=caption
                ):
                    await self.module.prepare(event)
                    await self.module.admit(event)
                self.assertEqual(
                    event.get_extra("juhkff_group_image_analysis"),
                    [{"is_meme": None, "description": ""}],
                )
                self.assert_caption_saved(self.before_caption, event, raw)
                req = ProviderRequest(prompt="question")
                await self.module.decorate(event, req)
                self.assertIn(raw, self.text(req))
                self.assertNotIn("[图片1·表情包]", self.text(req))
                self.assertNotIn("[图片1·普通图片]", self.text(req))

    async def test_json_fence_and_single_unknown_class_have_numbered_label(self):
        self.module.cfg["image_mode"] = "caption"
        event = self.event(components=[self.image()])

        async def caption(owner, req, *args, **kwargs):
            self.before_caption = deepcopy(self.module.store.records)
            req.extra_user_content_parts.append(
                TextPart(
                    text='```json\n{"images":[{"is_meme":null,"description":"  unclear image  "}]}\n```'
                )
            )
            return set(req.image_urls)

        with patch.object(group_chat, "caption_request_images", side_effect=caption):
            await self.module.prepare(event)
        expected = "[图片1·图片，类型未确定] unclear image"
        self.assert_caption_saved(self.before_caption, event, expected)
        self.assertEqual(
            event.get_extra("juhkff_group_image_analysis"),
            [{"is_meme": None, "description": "unclear image"}],
        )

    async def test_inbound_caption_provider_priority_uses_current_config_before_admit(
        self,
    ):
        self.module.cfg["image_mode"] = "caption"
        for nested_id, module_id, default_id, expected in (
            ("nested", "module", "default", "nested"),
            ("nested", "", "default", "nested"),
            ("", "module", "default", "module"),
            ("", "", "default", "default"),
            ("", "", "", "offline-vision"),
            (None, "module", "default", "module"),
            (None, "", "default", "default"),
            (None, "", "", "offline-vision"),
        ):
            with self.subTest(
                nested_id=nested_id, module_id=module_id, default_id=default_id
            ):
                if nested_id is None:
                    self.module.cfg.pop("caption_settings", None)
                else:
                    self.module.cfg["caption_settings"] = {"provider_id": nested_id}
                self.module.cfg["image_caption_provider_id"] = module_id
                self.snapshot["provider_settings"][
                    "default_image_caption_provider_id"
                ] = default_id
                event = self.event(components=[self.image()])
                with patch.object(
                    group_chat, "caption_request_images", side_effect=self.caption
                ) as describe:
                    await self.module.prepare(event)
                self.assertEqual(describe.call_args.args[4], expected)
                self.assertEqual(
                    self.snapshot["provider_settings"][
                        "default_image_caption_provider_id"
                    ],
                    default_id,
                )

    async def test_old_cooldown_setting_cannot_delay_inbound_analysis_or_reply(self):
        self.module.cfg.update(image_mode="caption", probability=1, cooldown=999999)
        with patch.object(
            group_chat, "caption_request_images", side_effect=self.caption
        ) as describe:
            for index in range(2):
                event = self.event(f"image {index}", components=[self.image()])
                await self.module.prepare(event)
                self.assertEqual(describe.await_count, index + 1)
                self.assert_caption_saved(self.before_caption, event)
                await self.module.admit(event)
                self.assertTrue(event.is_at_or_wake_command)
                self.assertEqual(describe.await_count, index + 1)

    async def test_inbound_analysis_exception_keeps_message_usable(self):
        self.module.cfg["image_mode"] = "caption"
        event = self.event(
            "still help with text", addressed=True, components=[self.image()]
        )
        with patch.object(
            group_chat,
            "caption_request_images",
            side_effect=RuntimeError("offline model"),
        ):
            await self.module.prepare(event)
        await self.module.admit(event)
        self.assertTrue(event.is_at_or_wake_command)
        record = json.loads(self.module.store.records[event.unified_msg_origin][-1][2])
        self.assertEqual(record["images"], ["base64://aGVsbG8="])
        self.assertNotIn("caption", record)
        req = ProviderRequest(prompt=event.message_str)
        await self.module.decorate(event, req)
        self.assertIn("[Image Captioning Failed]", self.text(req))

    async def test_redis_unavailable_still_analyzes_current_message(self):
        self.module.cfg["image_mode"] = "caption"
        event = self.event("question", addressed=True, components=[self.image()])
        with (
            patch.object(
                self.module.store,
                "snapshot_append",
                side_effect=ConnectionError("offline Redis"),
            ),
            patch.object(
                group_chat, "caption_request_images", side_effect=self.caption
            ) as describe,
        ):
            await self.module.prepare(event)
            await self.module.admit(event)
        describe.assert_awaited_once()
        self.assertFalse(self.module.store.records)
        req = ProviderRequest(prompt="question")
        await self.module.decorate(event, req)
        self.assertIn("CURRENT_CAPTION_TOKEN", self.text(req))
