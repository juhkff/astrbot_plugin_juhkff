"""Regressions for original-image validation and bounded processing waits."""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
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
            "description": "",
            "author": "Artist",
            "page_count": 1,
            "x_restrict": 0,
            "original": "https://i.pximg.net/7.png",
            "regular": "https://i.pximg.net/preview/7.png",
        }

    async def asyncTearDown(self):
        await self.module.stop()

    async def test_source_and_derivative_cache_io_runs_outside_event_loop(self):
        loop_thread = threading.get_ident()
        reads, writes = [], []
        read_bytes = Path.read_bytes
        write_cache = pixiv._write_image_cache

        def observed_read(path):
            reads.append(threading.get_ident())
            return read_bytes(path)

        def observed_write(target, payload):
            writes.append(threading.get_ident())
            return write_cache(target, payload)

        with (
            patch.object(
                pixiv, "fetch_bytes", AsyncMock(return_value=self.payload)
            ) as fetch,
            patch.object(Path, "read_bytes", observed_read),
            patch.object(pixiv, "_write_image_cache", observed_write),
        ):
            first = await self.module._download(self.item, self.item, 0)
            self.assertIsNotNone(first)
            second = await self.module._download(self.item, self.item, 0)
            self.assertEqual(first, second)
            fetch.assert_awaited_once()
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(writes), 2)
        self.assertNotIn(loop_thread, reads + writes)

    async def test_source_only_keeps_original_after_compatible_download(self):
        self.module.cfg["max_image_mb"] = 0.000001
        with patch.object(
            pixiv, "fetch_bytes", AsyncMock(return_value=self.payload)
        ) as fetch:
            original = await self.module._download(
                self.item, self.item, 0, source_only=True
            )
            self.assertIsNotNone(original)
            assert original is not None
            self.assertEqual(original.suffix, ".png")
            self.assertEqual(original.read_bytes(), self.payload)
            # A cached JPEG derivative must never replace the source-only result.
            self.module.cfg["max_image_mb"] = 0
            compatible = await self.module._download(self.item, self.item, 0)
            assert compatible is not None
            self.assertEqual(compatible.suffix, ".jpg")
            again = await self.module._download(
                self.item, self.item, 0, source_only=True
            )
            self.assertEqual(again, original)
            self.assertEqual(original.read_bytes(), self.payload)
            fetch.assert_awaited_once()

    def test_failed_cache_publish_keeps_previous_file_and_removes_temporary(self):
        target = self.folder / "original.png"
        target.write_bytes(self.payload)
        with patch.object(Path, "replace", side_effect=OSError("Publish failed")):
            with self.assertRaisesRegex(OSError, "Publish failed"):
                pixiv._write_image_cache(target, b"replacement")
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

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

    def test_compatibility_reencodes_static_formats_even_below_the_limit(self):
        for format in ("PNG", "JPEG", "BMP", "WEBP"):
            buffer = io.BytesIO()
            Image.new("RGB", (32, 16), "red").save(buffer, format=format)
            payload = buffer.getvalue()
            with Image.open(io.BytesIO(payload)) as source:
                expected = io.BytesIO()
                source.convert("RGB").save(
                    expected, format="JPEG", quality=90, subsampling=0, optimize=True
                )
            for limit in (0, 65536):
                with self.subTest(format=format, limit=limit):
                    prepared = pixiv._prepare_image(payload, limit, compatible=True)
                    assert prepared is not None
                    self.assertEqual(prepared, (expected.getvalue(), ".jpg"))
                    with Image.open(io.BytesIO(prepared[0])) as image:
                        self.assertEqual(image.format, "JPEG")
                        self.assertEqual(image.size, (32, 16))

    def test_compatibility_applies_exif_orientation_and_white_transparency(self):
        source = Image.new("RGB", (32, 16), "red")
        source.paste("blue", (16, 0, 32, 16))
        exif = Image.Exif()
        exif[274] = 6
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", exif=exif)
        prepared = pixiv._prepare_image(buffer.getvalue(), 0, compatible=True)
        assert prepared is not None
        with Image.open(io.BytesIO(prepared[0])) as image:
            self.assertEqual(image.size, (16, 32))
            self.assertIn(image.getexif().get(274), (None, 1))
            top, bottom = image.getpixel((8, 4)), image.getpixel((8, 28))
            self.assertGreater(top[0], 240)
            self.assertGreater(bottom[2], 240)

        for mode in ("RGBA", "LA", "P"):
            with self.subTest(mode=mode):
                source = Image.new("RGBA", (32, 16), (0, 0, 0, 0))
                source.paste((255, 0, 0, 255), (16, 0, 32, 16))
                if mode == "LA":
                    source = source.convert("LA")
                elif mode == "P":
                    source = source.convert("P")
                buffer = io.BytesIO()
                source.save(buffer, format="PNG")
                prepared = pixiv._prepare_image(buffer.getvalue(), 0, compatible=True)
                assert prepared is not None
                with Image.open(io.BytesIO(prepared[0])) as image:
                    self.assertEqual(image.mode, "RGB")
                    self.assertTrue(all(c >= 250 for c in image.getpixel((4, 8))))
                    self.assertLess(min(image.getpixel((28, 8))), 100)

    def test_compatibility_preserves_animation_and_rejects_oversized_multiframe(self):
        frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue")]
        for format, suffix in (("GIF", ".gif"), ("PNG", ".png"), ("TIFF", ".tiff")):
            buffer = io.BytesIO()
            frames[0].save(
                buffer, format=format, save_all=True, append_images=frames[1:]
            )
            payload = buffer.getvalue()
            for limit in (0, len(payload), len(payload) + 1):
                with self.subTest(format=format, limit=limit):
                    prepared = pixiv._prepare_image(payload, limit, compatible=True)
                    self.assertEqual(prepared, (payload, suffix))
                    assert prepared is not None
                    with Image.open(io.BytesIO(prepared[0])) as image:
                        self.assertEqual(image.n_frames, 2)
            self.assertIsNone(
                pixiv._prepare_image(payload, len(payload) - 1, compatible=True)
            )

    def test_compatibility_compresses_and_scales_to_positive_limits(self):
        buffer = io.BytesIO()
        Image.effect_noise((128, 128), 100).convert("RGB").save(buffer, format="PNG")
        payload = buffer.getvalue()
        for limit in (4096, 512):
            with self.subTest(limit=limit):
                prepared = pixiv._prepare_image(payload, limit, compatible=True)
                assert prepared is not None
                self.assertEqual(prepared[1], ".jpg")
                self.assertLessEqual(len(prepared[0]), limit)
                with Image.open(io.BytesIO(prepared[0])) as image:
                    image.load()
                    if limit == 512:
                        self.assertLess(image.width, 128)
                        self.assertLess(image.height, 128)
        self.assertIsNone(pixiv._prepare_image(payload, 1, compatible=True))
        self.assertIsNone(pixiv._prepare_image(b"not an image", 0, compatible=True))

    async def test_download_timeout_can_fall_back_to_a_working_mirror(self):
        self.module.cfg["image_format_compatibility"] = False
        with (
            patch.object(
                pixiv,
                "fetch_bytes",
                AsyncMock(side_effect=[asyncio.TimeoutError(), self.payload]),
            ) as fetch,
            patch.object(
                pixiv.asyncio,
                "to_thread",
                AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
            ),
        ):
            path = await self.module._download(self.item, self.item, 0)
        assert path is not None
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(
            [call.args[0] for call in fetch.await_args_list],
            [self.item["original"], "https://i.pixiv.re/7.png"],
        )

    async def test_compatibility_default_derives_from_original_and_reuses_valid_cache(
        self,
    ):
        self.assertNotIn("image_format_compatibility", self.module.cfg)
        original = self.folder / "pixiv" / "images" / "7_p0" / "original.png"
        original.parent.mkdir(parents=True)
        original.write_bytes(self.payload)
        with (
            patch.object(pixiv, "fetch_bytes", AsyncMock()) as fetch,
            patch.object(
                pixiv, "_prepare_image", wraps=pixiv._prepare_image
            ) as prepare,
        ):
            path = await self.module._download(self.item, self.item, 0)
            assert path is not None
            self.assertNotEqual(path, original)
            self.assertEqual(path.suffix, ".jpg")
            self.assertIn(hashlib.sha256(self.payload).hexdigest(), path.name)
            self.assertEqual(original.read_bytes(), self.payload)
            with Image.open(path) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (8, 8))
            conversions = sum(
                bool(call.kwargs.get("compatible")) for call in prepare.call_args_list
            )
            self.assertEqual(conversions, 1)
            self.assertEqual(await self.module._download(self.item, self.item, 0), path)
            self.assertEqual(
                sum(bool(c.kwargs.get("compatible")) for c in prepare.call_args_list),
                conversions,
            )
            path.write_bytes(b"corrupted derivative")
            rebuilt = await self.module._download(self.item, self.item, 0)
            self.assertEqual(rebuilt, path)
            with Image.open(path) as image:
                image.verify()
            self.assertEqual(
                sum(bool(c.kwargs.get("compatible")) for c in prepare.call_args_list),
                conversions + 1,
            )
            fetch.assert_not_awaited()
        self.assertEqual(original.read_bytes(), self.payload)

    async def test_compatibility_caches_already_matching_jpeg_bytes(self):
        prepared = pixiv._prepare_image(self.payload, 0, compatible=True)
        assert prepared is not None
        payload = prepared[0]
        self.assertEqual(pixiv._prepare_image(payload, 0, compatible=True), prepared)
        with (
            patch.object(pixiv, "fetch_bytes", AsyncMock(return_value=payload)),
            patch.object(
                pixiv, "_prepare_image", wraps=pixiv._prepare_image
            ) as prepare,
        ):
            first = await self.module._download(self.item, self.item, 0)
            assert first is not None
            self.assertNotEqual(first.name, "original.jpg")
            self.assertEqual(first.read_bytes(), payload)
            self.assertEqual(
                await self.module._download(self.item, self.item, 0), first
            )
            self.assertEqual(
                sum(bool(c.kwargs.get("compatible")) for c in prepare.call_args_list), 1
            )

    async def test_compatibility_cache_invalidates_when_source_or_limit_changes(self):
        with patch.object(
            pixiv, "fetch_bytes", AsyncMock(return_value=self.payload)
        ) as fetch:
            first = await self.module._download(self.item, self.item, 0)
            assert first is not None
            original = first.parent / "original.png"
            self.assertEqual(original.read_bytes(), self.payload)
            buffer = io.BytesIO()
            Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
            changed = buffer.getvalue()
            original.write_bytes(changed)
            second = await self.module._download(self.item, self.item, 0)
            assert second is not None
            self.assertNotEqual(first, second)
            self.assertIn(hashlib.sha256(changed).hexdigest(), second.name)
            with Image.open(second) as image:
                self.assertGreater(image.getpixel((4, 4))[2], 240)
            self.module.cfg["max_image_mb"] = 1
            third = await self.module._download(self.item, self.item, 0)
            assert third is not None
            self.assertNotEqual(second, third)
            self.assertIn(str(1024 * 1024), third.name)
            self.assertEqual(original.read_bytes(), changed)
            fetch.assert_awaited_once()

    async def test_source_only_compatibility_ignores_limit_and_preserves_source(self):
        self.module.cfg.update(image_format_compatibility=True, max_image_mb=0.000001)
        with patch.object(
            pixiv, "fetch_bytes", AsyncMock(return_value=self.payload)
        ) as fetch:
            path = await self.module._download(
                self.item, self.item, 0, source_only=True
            )
        assert path is not None
        self.assertEqual(path.suffix, ".png")
        self.assertEqual(path.read_bytes(), self.payload)
        with Image.open(path) as image:
            self.assertEqual(image.size, (8, 8))
        fetch.assert_awaited_once()
        self.assertEqual(fetch.await_args.args[0], self.item["original"])

    async def test_processing_timeouts_stop_without_queueing_alternate_images(self):
        for index, stage in enumerate(("validation", "compression", "compatibility")):
            with self.subTest(stage=stage):
                self.module.cfg["image_format_compatibility"] = stage == "compatibility"
                self.module.cfg["max_image_mb"] = (
                    0.000001 if stage == "compression" else 0
                )
                cancelled = asyncio.Event()

                async def stalled_worker(fn, *args, **kwargs):
                    if fn is not pixiv._prepare_image:
                        return fn(*args, **kwargs)
                    payload, limit = args
                    if (
                        stage != "validation"
                        and limit == 0
                        and not kwargs.get("compatible")
                    ):
                        return payload, ".png"
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                # Keep each case independent from the cached original of the other.
                item: pixiv.Illustration = {
                    **self.item,
                    "id": str(7 + index),
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
                preparation_calls = sum(
                    call.args[0] is pixiv._prepare_image
                    for call in worker.await_args_list
                )
                self.assertEqual(preparation_calls, 1 if stage == "validation" else 2)
