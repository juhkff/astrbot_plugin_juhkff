"""Focused tests using real Pillow glyph measurement and rendered pixels."""

import math
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from juhkff_modules import pixiv_typeset as typeset
from PIL import Image, ImageChops, ImageDraw, ImageFont


class PixivTypesetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "original.png"
        Image.new("RGB", (400, 400), "white").save(self.source)
        for candidate in typeset.FONT_CANDIDATES:
            try:
                self.font = ImageFont.truetype(candidate, 20)
                break
            except OSError:
                continue
        else:
            self.skipTest("an existing CJK font is required for real Pillow tests")

    def test_actual_pixels_appear_on_all_four_requested_sides(self):
        cases = (
            ("horizontal", [300, 300, 600, 400], "below"),
            ("horizontal", [300, 900, 600, 975], "above"),
            ("vertical", [600, 300, 700, 600], "left"),
            ("vertical", [5, 300, 75, 600], "right"),
        )
        for direction, bbox, side in cases:
            with self.subTest(side=side):
                original = Image.new("RGB", (400, 400), "white")
                box = tuple(round(n * 400 / 1000) for n in bbox)
                ImageDraw.Draw(original).rectangle(
                    (box[0], box[1], box[2] - 1, box[3] - 1), fill="red"
                )
                original.save(self.source)
                before = self.source.read_bytes()
                output = typeset.render_translation(
                    self.source,
                    [
                        {
                            "bbox": bbox,
                            "direction": direction,
                            "translation": "中文翻译",
                            "font_size": 24,
                        }
                    ],
                )
                self.assertNotEqual(output, self.source)
                self.assertEqual(output.parent, self.source.parent)
                self.assertEqual(output.suffix, ".png")
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)
                self.assertIn(".translated-", output.name)
                self.assertEqual(self.source.read_bytes(), before)
                with Image.open(output) as rendered:
                    self.assertEqual(rendered.size, original.size)
                    changed = ImageChops.difference(original, rendered).getbbox()
                    self.assertIsNotNone(changed)
                    self.assertIsNone(
                        ImageChops.difference(
                            original.crop(box), rendered.crop(box)
                        ).getbbox()
                    )
                if side == "below":
                    self.assertGreaterEqual(changed[1], box[3] + typeset.GAP)
                elif side == "above":
                    self.assertLessEqual(changed[3], box[1] - typeset.GAP)
                elif side == "left":
                    self.assertLessEqual(changed[2], box[0] - typeset.GAP)
                else:
                    self.assertGreaterEqual(changed[0], box[2] + typeset.GAP)

    def test_alpha_and_palette_transparency_preserved_outside_translation(self):
        for mode in ("RGBA", "P"):
            with self.subTest(mode=mode):
                if mode == "RGBA":
                    original = Image.new("RGBA", (400, 400), (30, 60, 90, 70))
                else:
                    original = Image.new("P", (400, 400), 0)
                    original.putpalette([30, 60, 90] + [0] * 765)
                    original.info["transparency"] = 0
                original.save(self.source)
                expected = original.convert("RGBA")
                output = typeset.render_translation(
                    self.source,
                    [
                        {
                            "bbox": [100, 100, 300, 200],
                            "direction": "horizontal",
                            "translation": "中文",
                            "font_size": 24,
                        }
                    ],
                )
                rect, _ = typeset._place_translation(
                    "中文", self.font, (40, 40, 120, 80), "horizontal", (400, 400)
                )
                with Image.open(output) as rendered:
                    self.assertEqual(rendered.mode, "RGBA")
                    self.assertNotEqual(rendered.tobytes(), expected.tobytes())
                    actual = rendered.copy()
                for canvas in (actual, expected):
                    ImageDraw.Draw(canvas).rectangle(
                        (rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=(0, 0, 0, 0)
                    )
                self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_wrapped_extent_selects_opposite_side(self):
        for direction, box, size in (
            ("horizontal", (50, 250, 150, 270), (400, 300)),
            ("vertical", (30, 50, 50, 150), (400, 300)),
        ):
            with self.subTest(direction=direction):
                rect, glyphs = typeset._place_translation(
                    "中文翻译" * 5, self.font, box, direction, size
                )
                self.assertEqual(len(glyphs), 20)
                if direction == "horizontal":
                    self.assertLessEqual(rect[3], box[1] - typeset.GAP)
                    self.assertGreater(rect[3] - rect[1], self.font.size * 2)
                else:
                    self.assertGreaterEqual(rect[0], box[2] + typeset.GAP)
                    self.assertGreater(rect[2] - rect[0], self.font.size * 2)
                # Reconstruct the real glyph mask to verify every wrapped glyph
                # lies inside the measured rectangle including its white stroke.
                layer = Image.new("RGB", size, "white")
                draw = ImageDraw.Draw(layer)
                for x, y, char in glyphs:
                    draw.text(
                        (rect[0] + typeset.PADDING + x, rect[1] + typeset.PADDING + y),
                        char,
                        font=self.font,
                        fill="black",
                        stroke_width=1,
                        stroke_fill="black",
                    )
                ink = ImageChops.difference(
                    layer, Image.new("RGB", size, "white")
                ).getbbox()
                self.assertGreaterEqual(ink[0], rect[0])
                self.assertGreaterEqual(ink[1], rect[1])
                self.assertLessEqual(ink[2], rect[2])
                self.assertLessEqual(ink[3], rect[3])

    def test_translation_font_is_smaller_and_hard_bounded(self):
        real_place = typeset._place_translation
        for estimate, expected in ((1, 14), (24, 20), (1000, 32), (10**400, 32)):
            with self.subTest(estimate=estimate):
                with patch.object(
                    typeset, "_place_translation", wraps=real_place
                ) as place:
                    typeset.render_translation(
                        self.source,
                        [
                            {
                                "bbox": [250, 250, 500, 350],
                                "direction": "horizontal",
                                "translation": "中文",
                                "font_size": estimate,
                            }
                        ],
                    )
                self.assertEqual(place.call_args.args[1].size, expected)

    def test_missing_font_size_uses_source_short_axis(self):
        with patch.object(
            typeset, "_place_translation", wraps=typeset._place_translation
        ) as place:
            typeset.render_translation(
                self.source,
                [
                    {
                        "bbox": [250, 250, 500, 300],
                        "direction": "horizontal",
                        "translation": "中文",
                    }
                ],
            )
        self.assertEqual(place.call_args.args[1].size, 17)

    def test_empty_regions_return_source_without_io(self):
        missing = self.folder / "does-not-exist.png"
        self.assertEqual(typeset.render_translation(missing, []), missing)

    def test_invalid_payloads_are_rejected(self):
        valid = {
            "bbox": [100, 100, 400, 200],
            "direction": "horizontal",
            "translation": "中文",
        }
        invalid = [None, {}, [None], [valid] * 65]
        for field, values in (
            (
                "bbox",
                (
                    None,
                    [1, 2, 3],
                    [0, 0, math.nan, 1],
                    [0, 0, math.inf, 1],
                    [True, 0, 4, 4],
                    [5, 0, 1, 1],
                    [1001, 0, 1100, 10],
                    [0, 0, "100", 20],
                ),
            ),
            ("direction", (None, [], "diagonal")),
            ("translation", (None, "", "   ", "a\x00b", "x" * 2001)),
            ("font_size", (None, True, "20", 0, -1, math.nan, math.inf)),
        ):
            invalid.extend([{**valid, field: value}] for value in values)
        invalid.append([{**valid, "translation": "x" * 2000}] * 7)
        for payload in invalid:
            with self.subTest(payload=str(payload)[:80]):
                with self.assertRaises(ValueError):
                    typeset.render_translation(self.source, payload)
        self.assertEqual(list(self.folder.iterdir()), [self.source])

    def test_clipped_finite_bbox_renders(self):
        output = typeset.render_translation(
            self.source,
            [
                {
                    "bbox": [-(10**400), -50, 300, 200],
                    "direction": "horizontal",
                    "translation": "中文",
                }
            ],
        )
        self.assertTrue(output.is_file())

    def test_insufficient_space_fails_without_output(self):
        for direction in ("horizontal", "vertical"):
            with self.subTest(direction=direction), self.assertRaises(ValueError):
                typeset.render_translation(
                    self.source,
                    [
                        {
                            "bbox": [0, 0, 1000, 1000],
                            "direction": direction,
                            "translation": "中文",
                        }
                    ],
                )
        self.assertEqual(list(self.folder.iterdir()), [self.source])

    def test_oversized_image_rejected_before_pixel_decode(self):
        Image.new("1", (8200, 2)).save(self.source)
        with self.assertRaisesRegex(ValueError, "dimension/pixel"):
            typeset.render_translation(
                self.source,
                [
                    {
                        "bbox": [100, 100, 300, 200],
                        "direction": "horizontal",
                        "translation": "中文",
                    }
                ],
            )

    def test_pixel_limit_rejected_before_pixel_decode(self):
        Image.new("1", (5001, 5000)).save(self.source)
        with self.assertRaisesRegex(ValueError, "dimension/pixel"):
            typeset.render_translation(
                self.source,
                [
                    {
                        "bbox": [100, 100, 300, 200],
                        "direction": "horizontal",
                        "translation": "中文",
                    }
                ],
            )

    def test_file_size_limit_and_corrupt_source_fail_cleanly(self):
        with self.source.open("wb") as handle:
            handle.truncate(64_000_001)
        region = {
            "bbox": [100, 100, 300, 200],
            "direction": "horizontal",
            "translation": "中文",
        }
        with self.assertRaisesRegex(ValueError, "64 MB"):
            typeset.render_translation(self.source, [region])
        self.source.write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "Cannot render translation"):
            typeset.render_translation(self.source, [region])

    def test_wrapped_text_that_exceeds_canvas_fails_cleanly(self):
        with self.assertRaisesRegex(ValueError, "exceeds image dimensions"):
            typeset.render_translation(
                self.source,
                [
                    {
                        "bbox": [100, 100, 300, 200],
                        "direction": "horizontal",
                        "translation": "中文" * 1000,
                    }
                ],
            )
        self.assertEqual(list(self.folder.iterdir()), [self.source])

    def test_missing_font_fails_cleanly(self):
        with patch.object(typeset, "FONT_CANDIDATES", ("/missing/font.ttc",)):
            with self.assertRaisesRegex(ValueError, "CJK font"):
                typeset.render_translation(
                    self.source,
                    [
                        {
                            "bbox": [100, 100, 300, 200],
                            "direction": "horizontal",
                            "translation": "中文",
                        }
                    ],
                )

    def test_atomic_save_failure_removes_temporary_file(self):
        before = self.source.read_bytes()
        with patch.object(typeset.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(ValueError, "disk failure"):
                typeset.render_translation(
                    self.source,
                    [
                        {
                            "bbox": [100, 100, 300, 200],
                            "direction": "horizontal",
                            "translation": "中文",
                        }
                    ],
                )
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.folder.iterdir()), [self.source])


if __name__ == "__main__":
    unittest.main()
