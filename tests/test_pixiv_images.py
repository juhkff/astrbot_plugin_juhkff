"""Regressions for original-image validation and bounded processing waits."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from juhkff_modules import pixiv_push as pixiv
from PIL import Image, ImageFile


class PixivImageTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        patcher = patch.object(pixiv, "data_dir", return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        plugin: Any = SimpleNamespace(
            global_enabled=True,
            config={"pixiv_push": {"enable": True, "print_log": False}},
        )
        self.module = pixiv.PixivPushModule(plugin)
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
        self.payload = buffer.getvalue()
        self.item: pixiv.Illustration = {
            "id": "7",
            "title": "Work",
            "author": "Artist",
            "page_count": 1,
            "x_restrict": 0,
            "original": "https://i.pximg.net/7.png",
            "regular": "https://i.pximg.net/preview/7.png",
        }

    async def asyncTearDown(self):
        await self.module.stop()

    def test_preserving_original_bytes_never_decodes_pixel_buffers(self):
        with patch.object(
            ImageFile.ImageFile, "load", side_effect=AssertionError("Pixel decode")
        ):
            for limit in (0, len(self.payload) + 1):
                with self.subTest(limit=limit):
                    self.assertEqual(
                        pixiv._prepare_image(self.payload, limit),
                        (self.payload, ".png"),
                    )

    def test_structure_validation_still_rejects_corrupt_png_data(self):
        corrupted = bytearray(self.payload)
        corrupted[self.payload.index(b"IDAT") + 4] ^= 0xFF
        self.assertIsNone(pixiv._prepare_image(bytes(corrupted), 0))
        self.assertIsNone(pixiv._prepare_image(b"not an image", 0))

    def test_explicit_compression_still_decodes_and_respects_the_limit(self):
        buffer = io.BytesIO()
        Image.effect_noise((128, 128), 100).convert("RGB").save(buffer, format="PNG")
        prepared = pixiv._prepare_image(buffer.getvalue(), 4096)
        assert prepared is not None
        self.assertEqual(prepared[1], ".jpg")
        self.assertLessEqual(len(prepared[0]), 4096)
        with Image.open(io.BytesIO(prepared[0])) as image:
            image.load()

    async def test_download_timeout_can_fall_back_to_a_working_mirror(self):
        with (
            patch.object(
                pixiv,
                "fetch_bytes",
                AsyncMock(side_effect=[asyncio.TimeoutError(), self.payload]),
            ) as fetch,
            patch.object(
                pixiv.asyncio,
                "to_thread",
                AsyncMock(side_effect=lambda fn, *args: fn(*args)),
            ),
        ):
            path = await self.module._download(self.item, self.item, 0)
        assert path is not None
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(
            [call.args[0] for call in fetch.await_args_list],
            [self.item["original"], "https://i.pixiv.re/7.png"],
        )

    async def test_processing_timeouts_stop_without_queueing_alternate_images(self):
        for stage in ("validation", "compression"):
            with self.subTest(stage=stage):
                self.module.cfg["max_image_mb"] = (
                    0 if stage == "validation" else 0.000001
                )
                cancelled = asyncio.Event()

                async def stalled_worker(fn, payload, limit):
                    if stage == "compression" and limit == 0:
                        return payload, ".png"
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                # Keep each case independent from the cached original of the other.
                item: pixiv.Illustration = {
                    **self.item,
                    "id": "7" if stage == "validation" else "8",
                }
                with (
                    patch.object(pixiv, "_IMAGE_PROCESS_TIMEOUT", 0.01),
                    patch.object(
                        pixiv, "fetch_bytes", AsyncMock(return_value=self.payload)
                    ) as fetch,
                    patch.object(
                        pixiv.asyncio,
                        "to_thread",
                        AsyncMock(side_effect=stalled_worker),
                    ) as worker,
                ):
                    self.assertIsNone(await self.module._download(item, self.item, 0))
                self.assertTrue(cancelled.is_set())
                fetch.assert_awaited_once()
                self.assertEqual(worker.await_count, 1 if stage == "validation" else 2)
