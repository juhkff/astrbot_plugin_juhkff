"""Help retention regressions using temporary files and mocked rendering."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from juhkff_modules import help_gen, renderer

from astrbot.api.message_components import Image, Plain


class HelpGenTests(unittest.IsolatedAsyncioTestCase):
    """Protect current and recent replies while reclaiming old generated files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.target = self.base / "help"
        self.target.mkdir()
        self.now = 1_000_000.0
        plugin = SimpleNamespace(
            config={"help": {}},
            version="test",
            modules={
                "sample": SimpleNamespace(
                    enabled=True,
                    key="sample",
                    title="Sample",
                    help_entries=lambda: [("#sample", "Description")],
                )
            },
        )
        self.module = help_gen.HelpGenModule(cast(Any, plugin))
        self.paths = patch.object(help_gen, "data_dir", return_value=self.base)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.clock = patch.object(help_gen.time, "time", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.logs = patch.object(help_gen, "logger")
        self.logs.start()
        self.addCleanup(self.logs.stop)

    def create_image(self, name: str, modified: float) -> Path:
        """Create a temporary file with an explicit age.

        Args:
            name: File component below the temporary help directory.
            modified: Modification timestamp for deterministic retention.

        Returns:
            The created file path.
        """
        path = self.target / name
        path.write_bytes(b"old png")
        os.utime(path, (modified, modified))
        return path

    async def test_only_old_generated_files_above_twenty_are_deleted(self):
        old = [
            self.create_image(f"help_{index}.png", self.now - 7200 + index)
            for index in range(30)
        ]
        unrelated = [
            self.create_image(name, self.now - 8000)
            for name in (
                "help_manual.png",
                "help_12.png.bak",
                "help_１２.png",
                "photo.png",
            )
        ]
        directory = self.target / "help_9999.png"
        directory.mkdir()
        current = self.create_image("help_1000000.png", self.now)
        self.module._prune_images(current)
        self.assertTrue(current.is_file())
        self.assertTrue(all(path.is_file() for path in old[11:]))
        self.assertTrue(all(not path.exists() for path in old[:11]))
        self.assertTrue(all(path.is_file() for path in unrelated))
        self.assertTrue(directory.is_dir())

    async def test_hour_grace_preserves_queued_sends_then_eventually_caps(self):
        recent = [
            self.create_image(f"help_{index}.png", self.now - 100 + index)
            for index in range(35)
        ]
        self.module._prune_images(recent[-1])
        self.assertTrue(all(path.exists() for path in recent))
        with patch.object(help_gen.time, "time", return_value=self.now + 7200):
            self.module._prune_images(recent[-1])
        self.assertEqual(sum(path.exists() for path in recent), 20)
        self.assertTrue(recent[-1].is_file())

    async def test_current_image_is_protected_even_with_old_timestamp(self):
        current = self.create_image("help_1.png", self.now - 10000)
        newer = [
            self.create_image(f"help_{index}.png", self.now - 7200 + index)
            for index in range(2, 32)
        ]
        self.module._prune_images(current)
        self.assertTrue(current.is_file())
        self.assertEqual(sum(path.exists() for path in newer), 20)

    async def test_generated_symlink_is_never_deleted_or_followed(self):
        outside = self.base / "outside.png"
        outside.write_bytes(b"unrelated")
        symlink = self.target / "help_0.png"
        symlink.symlink_to(outside)
        for index in range(1, 32):
            self.create_image(f"help_{index}.png", self.now - 7200 + index)
        current = self.create_image("help_999.png", self.now)
        self.module._prune_images(current)
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(outside.read_bytes(), b"unrelated")

    async def test_cleanup_rejects_a_different_directory(self):
        other = self.base / "unrelated"
        other.mkdir()
        current = other / "help_1.png"
        current.write_bytes(b"unrelated")
        old = [
            self.create_image(f"help_{index}.png", self.now - 7200)
            for index in range(30)
        ]
        self.module._prune_images(current)
        self.assertTrue(all(path.exists() for path in old))
        self.assertTrue(current.is_file())

    async def test_cleanup_rejects_a_symlinked_help_directory(self):
        outside = self.base / "outside"
        outside.mkdir()
        for index in range(30):
            path = outside / f"help_{index}.png"
            path.write_bytes(b"unrelated")
            os.utime(path, (self.now - 7200, self.now - 7200))
        # The original directory is empty and its exact temporary path is known.
        self.assertEqual(self.target.resolve(), self.base / "help")
        self.target.rmdir()
        self.target.symlink_to(outside, target_is_directory=True)
        self.module._prune_images(self.target / "help_29.png")
        self.assertEqual(len(list(outside.iterdir())), 30)

    async def test_concurrent_replies_render_fresh_images_and_preserve_both(self):
        for index in range(30):
            self.create_image(f"help_{index}.png", self.now - 7200 + index)
        event: Any = SimpleNamespace(plain_result=Mock())
        render = AsyncMock(side_effect=[b"first png", b"second png"])
        with (
            patch.object(renderer, "render_template", new=render),
            patch.object(help_gen.time, "time_ns", side_effect=[1_000_001, 1_000_002]),
        ):
            first, second = await asyncio.gather(
                self.module._reply(event), self.module._reply(event)
            )
        first_image, second_image = first.chain[0], second.chain[0]
        assert isinstance(first_image, Image)
        assert isinstance(second_image, Image)
        assert first_image.path is not None
        assert second_image.path is not None
        paths = [Path(first_image.path), Path(second_image.path)]
        self.assertNotEqual(*paths)
        self.assertEqual(
            [path.read_bytes() for path in paths], [b"first png", b"second png"]
        )
        self.assertEqual(render.await_count, 2)
        assert render.await_args is not None
        self.assertEqual(
            render.await_args.kwargs,
            {"scale": 2, "viewport": {"width": 880, "height": 900}},
        )
        text = first.chain[1]
        assert isinstance(text, Plain)
        self.assertEqual(text.text, "这是当前已启用功能的使用说明。")
        event.plain_result.assert_not_called()

    async def test_render_failure_keeps_existing_error_reply(self):
        event: Any = SimpleNamespace(plain_result=Mock(return_value="render error"))
        with patch.object(
            renderer, "render_template", new=AsyncMock(return_value=None)
        ):
            self.assertEqual(await self.module._reply(event), "render error")
        event.plain_result.assert_called_once_with(
            "帮助图生成失败，详细原因看 AstrBot 日志（[juhkff] 开头）"
        )
        self.assertEqual(list(self.target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
