"""Emoji regressions with temporary galleries and mocked events and models."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from juhkff_modules import emoji_steal


def make_event(umo="test:GroupMessage:123", message_type="GroupMessage", images=()):
    """Build a group or private event without any platform connection.

    Args:
        umo: Raw platform message origin to expose.
        message_type: Message type returned by the event.
        images: Image mocks to include in the message.

    Returns:
        A mock event with only local message data.
    """
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_message_type=Mock(return_value=SimpleNamespace(value=message_type)),
        message_obj=SimpleNamespace(message=list(images)),
    )


class EmojiStealTests(unittest.IsolatedAsyncioTestCase):
    """Check exact verdicts, fail-closed group detection, and storage isolation."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.context = SimpleNamespace(llm_generate=AsyncMock())
        self.config = {}
        self.module = emoji_steal.EmojiStealModule(
            SimpleNamespace(config={"emoji_steal": self.config}, context=self.context)
        )
        self.module._root = self.base / "emoji"
        self.module._root.mkdir()
        self.module._save = Mock()
        self.module._rng = Mock()
        self.module._rng.random.return_value = 1.0
        self.paths = patch.object(emoji_steal, "data_dir", return_value=self.base)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.logs = patch.object(emoji_steal, "logger")
        self.logs.start()
        self.addCleanup(self.logs.stop)
        self.module.debug = Mock()
        self.source = self.base / "source.png"
        self.data = b"\x89PNG\r\n\x1a\nlocal image bytes"
        self.source.write_bytes(self.data)
        self.image = Mock(spec=emoji_steal.Image)
        self.image.convert_to_file_path = AsyncMock(return_value=str(self.source))

    async def test_only_exact_normalized_verdicts_are_accepted(self):
        self.config.update(judge_emoji=True, judge_provider="test-provider")
        cases = {
            "是": True,
            "是。": True,
            " YES! ": True,
            "Ｔｒｕｅ！": True,
            "否": False,
            " 否。 ": False,
            "NO.": False,
            "false": False,
            "是一张普通图片，不是表情包": None,
            "是的，但不是表情包": None,
            "不是表情包": None,
            "没有足够信息": None,
            "yes, but not an emoji": None,
            "true story": None,
            "no idea": None,
            "not sure": None,
            "yesterday": None,
            "y": None,
            "n": None,
            "是/否": None,
            "": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.context.llm_generate.return_value = SimpleNamespace(
                    completion_text=text
                )
                self.assertIs(
                    await self.module._judge(self.source, "test:GroupMessage:123"),
                    expected,
                )
        self.assertEqual(self.context.llm_generate.await_count, len(cases))
        self.assertEqual(
            self.context.llm_generate.await_args.kwargs["chat_provider_id"],
            "test-provider",
        )

    async def test_ambiguous_verdict_preserves_repeat_collection(self):
        self.config.update(judge_emoji=True, judge_provider="test-provider")
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text="是一张普通图片，不是表情包"
        )
        event = make_event(images=[self.image])
        await self.module._collect(event, "123", event.unified_msg_origin)
        digest = hashlib.sha256(self.data).hexdigest()
        self.assertNotIn(digest, self.module._index.get("123", {}))
        self.assertNotIn("emoji", self.module._prints["123"][digest])
        await self.module._collect(event, "123", event.unified_msg_origin)
        files = self.module._gallery_files("123")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), self.data)
        self.assertEqual(self.context.llm_generate.await_count, 2)

    async def test_exact_negative_clears_repeat_and_positive_is_reused(self):
        self.config.update(judge_emoji=True, judge_provider="test-provider")
        event = make_event(images=[self.image])
        digest = hashlib.sha256(self.data).hexdigest()
        self.module._prints = {"123": {digest: {"first": 1e20}}}
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="否")
        await self.module._collect(event, "123", event.unified_msg_origin)
        self.assertNotIn(digest, self.module._prints["123"])
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="是")
        await self.module._collect(event, "123", event.unified_msg_origin)
        await self.module._collect(event, "123", event.unified_msg_origin)
        self.assertEqual(len(self.module._gallery_files("123")), 1)
        self.assertEqual(self.context.llm_generate.await_count, 2)

    async def test_defaults_and_unconfigured_model_behavior_are_preserved(self):
        self.assertEqual(self.module._expire, 72 * 3600)
        self.assertEqual(self.module._size_limit, 2 * 1024**2)
        self.assertEqual(self.module._max_per_group, 30)
        self.assertEqual(self.module._rates("123", "test:GroupMessage:123"), 0.2)
        self.assertFalse(self.module._judge_enabled)
        self.assertIsNone(
            await self.module._judge(self.source, "test:GroupMessage:123")
        )
        self.context.llm_generate.assert_not_awaited()
        self.config.update(judge_provider="test-provider", judge_emoji=False)
        self.assertIsNone(
            await self.module._judge(self.source, "test:GroupMessage:123")
        )
        self.context.llm_generate.assert_not_awaited()

    async def test_model_failure_keeps_repeat_fallback(self):
        self.config.update(judge_emoji=True, judge_provider="test-provider")
        self.context.llm_generate.side_effect = RuntimeError("provider unavailable")
        self.assertIsNone(
            await self.module._judge(self.source, "test:GroupMessage:123")
        )

    async def test_private_or_broken_event_never_collects_or_chooses_gallery(self):
        self.module._collect = AsyncMock()
        self.module._local_gallery = Mock()
        private = make_event("test:FriendMessage:123", "FriendMessage")
        broken = make_event()
        broken.get_message_type.side_effect = RuntimeError("message type unavailable")
        for event in (private, broken):
            with self.subTest(event=event):
                self.assertIsNone(await self.module.handle(event))
        self.module._collect.assert_not_awaited()
        self.module._local_gallery.assert_not_called()
        self.module._rng.random.assert_not_called()

    async def test_invalid_event_origins_skip_before_collection(self):
        self.module._collect = AsyncMock()
        origins = (
            None,
            123,
            "123",
            "test:GroupMessage",
            ":GroupMessage:123",
            "test::123",
            "test:GroupMessage:",
            "test:GroupMessage:.",
            "test:GroupMessage:..",
            "test:GroupMessage:../escape",
            "test:GroupMessage:..\\escape",
            "test:GroupMessage:C:\\escape",
            "test:GroupMessage:C:",
            "test:GroupMessage:safe:extra",
            "test:GroupMessage:bad\x00id",
        )
        for origin in origins:
            with self.subTest(origin=origin):
                self.assertIsNone(await self.module.handle(make_event(origin)))
        self.module._collect.assert_not_awaited()
        self.module._rng.random.assert_not_called()

    async def test_benign_group_ids_preserve_raw_probability_and_index_identity(self):
        self.module._collect = AsyncMock()
        for group in (
            "123",
            "-1001234567890",
            "room_42-abc",
            "room.name",
            "abc@thread.v2",
        ):
            with self.subTest(group=group):
                umo = f"test:GroupMessage:{group}"
                self.config["group_rates"] = {group: {"emoji_rate": 0.35}}
                self.assertEqual(self.module._group_key(umo), group)
                self.assertEqual(self.module._rates(group, umo), 0.35)
                self.assertEqual(
                    self.module._gallery_dir(group), self.module._root / group
                )
                self.assertIsNone(await self.module.handle(make_event(umo)))
                self.module._collect.assert_awaited_with(unittest.mock.ANY, group, umo)

    async def test_direct_collection_rejects_unsafe_group_before_reading_images(self):
        event = make_event(images=[self.image])
        for group in ("..", "../escape", "/absolute", "C:", "a/b", "a\\b", "bad\x00id"):
            with self.subTest(group=group):
                await self.module._collect(event, group, event.unified_msg_origin)
        self.image.convert_to_file_path.assert_not_awaited()
        self.assertEqual(self.module._index, {})
        self.assertEqual(self.module._prints, {})
        self.assertEqual(list(self.module._root.iterdir()), [])

    async def test_negative_group_id_collects_without_rewriting_keys(self):
        group = "-1001234567890"
        event = make_event(f"telegram:GroupMessage:{group}", images=[self.image])
        await self.module._collect(event, group, event.unified_msg_origin)
        await self.module._collect(event, group, event.unified_msg_origin)
        self.assertEqual(list(self.module._index), [group])
        self.assertEqual(self.module._gallery_files(group)[0].parent.name, group)

    async def test_invalid_persisted_groups_and_filenames_are_skipped_and_pruned(self):
        outside = self.base / "outside.png"
        outside.write_bytes(b"outside")
        directory = self.module._root / "123"
        directory.mkdir()
        valid = directory / "valid.png"
        valid.write_bytes(b"safe")
        invalid_files = (
            "../outside.png",
            "../../outside.png",
            str(outside),
            "..\\outside.png",
            "C:\\outside.png",
            "C:outside.png",
            ".",
            "..",
            "bad\x00file.png",
            "",
        )
        self.module._index = {
            "123": {
                **{
                    str(index): {"file": file}
                    for index, file in enumerate(invalid_files)
                },
                "good": {"file": "valid.png"},
                "malformed": None,
            },
            "..": {"escape": {"file": "outside.png"}},
            "../escape": {"escape": {"file": "outside.png"}},
            "C:": {"escape": {"file": "outside.png"}},
            "bad-shape": [],
        }
        self.module._prints = {
            "123": {"good": {"first": 1e20}, "malformed": None},
            "..": {"escape": {"first": 1e20}},
            "../escape": {"escape": {"first": 1e20}},
        }
        self.assertEqual(self.module._gallery_files("123"), [valid])
        self.assertEqual(self.module._gallery_files(".."), [])
        self.module._prune()
        self.assertEqual(self.module._index, {"123": {"good": {"file": "valid.png"}}})
        self.assertEqual(self.module._prints, {"123": {"good": {"first": 1e20}}})
        self.assertEqual(outside.read_bytes(), b"outside")

    async def test_trimming_never_deletes_metadata_paths_outside_group(self):
        self.config["max_per_group"] = 1
        outside = self.base / "outside.png"
        outside.write_bytes(b"outside")
        directory = self.module._root / "123"
        directory.mkdir()
        old, new = directory / "old.png", directory / "new.png"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        self.module._index = {
            "123": {
                "escape": {"file": "../../outside.png", "saved_at": -1},
                "absolute": {"file": str(outside), "saved_at": -1},
                "old": {"file": "old.png", "saved_at": 1},
                "new": {"file": "new.png", "saved_at": 2},
            },
            "..": {"escape": {"file": "outside.png", "saved_at": 0}},
        }
        self.module._trim_group("123")
        self.module._trim_group("..")
        self.assertEqual(
            self.module._index, {"123": {"new": {"file": "new.png", "saved_at": 2}}}
        )
        self.assertFalse(old.exists())
        self.assertTrue(new.is_file())
        self.assertEqual(outside.read_bytes(), b"outside")

    async def test_symlink_group_and_root_cannot_escape_collection_or_gallery(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sticker.png").write_bytes(b"outside")
        (self.module._root / "linked").symlink_to(outside, target_is_directory=True)
        self.module._index = {"linked": {"x": {"file": "sticker.png"}}}
        self.module._prints = {"linked": {"x": {"first": 1e20}}}
        self.assertIsNone(self.module._gallery_dir("linked"))
        self.assertEqual(self.module._gallery_files("linked"), [])
        await self.module._collect(make_event(images=[self.image]), "linked", "origin")
        self.image.convert_to_file_path.assert_not_awaited()
        self.module._prune()
        self.assertEqual(self.module._index, {})
        self.assertEqual(self.module._prints, {})
        root_link = self.base / "linked-root"
        root_link.symlink_to(outside, target_is_directory=True)
        self.module._root = root_link
        self.assertIsNone(self.module._gallery_dir("123"))
        self.assertEqual((outside / "sticker.png").read_bytes(), b"outside")

    async def test_symlink_image_is_not_read_overwritten_or_deleted(self):
        outside = self.base / "outside.png"
        outside.write_bytes(b"outside")
        directory = self.module._root / "123"
        directory.mkdir()
        digest = hashlib.sha256(self.data).hexdigest()
        linked = directory / f"{digest[:16]}.png"
        linked.symlink_to(outside)
        self.module._index = {"123": {"unsafe": {"file": linked.name, "saved_at": 0}}}
        self.assertEqual(self.module._gallery_files("123"), [])
        self.module._trim_group("123")
        self.assertTrue(linked.is_symlink())
        event = make_event(images=[self.image])
        await self.module._collect(event, "123", event.unified_msg_origin)
        await self.module._collect(event, "123", event.unified_msg_origin)
        self.assertNotIn(digest, self.module._index["123"])
        self.assertTrue(linked.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")

    async def test_local_gallery_fallback_remains_available(self):
        self.config.update(default_emoji_rate=1.0, gallery_path="gallery")
        local = self.base / "gallery"
        local.mkdir()
        sticker = local / "sticker.png"
        sticker.write_bytes(b"local")
        self.module._index = {"123": {"bad": {"file": "../../outside.png"}}}
        self.module._rng.random.return_value = 0.0
        self.module._rng.uniform.return_value = 0.0
        self.module._rng.choice.side_effect = lambda files: files[0]
        result = await self.module.handle(make_event())
        self.assertEqual(result.chain[0].path, str(sticker))
        self.context.llm_generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
