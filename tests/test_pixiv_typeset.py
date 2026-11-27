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
                    "中文",
                    ImageFont.truetype(self.font.path, 22),
                    (40, 40, 120, 80),
                    "horizontal",
                    (400, 400),
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
                # lies inside the measured rectangle including its stroke.
                layer = Image.new("RGB", size, "white")
                draw = ImageDraw.Draw(layer)
                for x, y, char in glyphs:
                    draw.text(
                        (rect[0] + x, rect[1] + y),
                        char,
                        font=self.font,
                        fill="black",
                        stroke_width=2,
                        stroke_fill="black",
                    )
                ink = ImageChops.difference(
                    layer, Image.new("RGB", size, "white")
                ).getbbox()
                self.assertGreaterEqual(ink[0], rect[0])
                self.assertGreaterEqual(ink[1], rect[1])
                self.assertLessEqual(ink[2], rect[2])
                self.assertLessEqual(ink[3], rect[3])

    def test_large_glyphs_and_punctuation_stay_inside_label_padding(self):
        for size in (48, 128):
            font = ImageFont.truetype(self.font.path, size)
            padding = max(typeset.PADDING, round(size * 0.25))
            for direction in ("horizontal", "vertical"):
                with self.subTest(size=size, direction=direction):
                    rect, positions = typeset._place_translation(
                        "中文g（！）…" * 2,
                        font,
                        (800, 600, 1200, 1000),
                        direction,
                        (2000, 2000),
                    )
                    layer = Image.new("L", (2000, 2000), 0)
                    draw = ImageDraw.Draw(layer)
                    for x, y, char in positions:
                        draw.text(
                            (rect[0] + x, rect[1] + y),
                            char,
                            font=font,
                            fill=255,
                            stroke_width=2,
                            stroke_fill=255,
                        )
                    ink = layer.getbbox()
                    self.assertGreaterEqual(ink[0], rect[0] + padding)
                    self.assertGreaterEqual(ink[1], rect[1] + padding)
                    self.assertLessEqual(ink[2], rect[2] - padding)
                    self.assertLessEqual(ink[3], rect[3] - padding)

    def test_translation_font_is_adaptive_and_hard_bounded(self):
        real_place = typeset._place_translation
        for estimate, expected in ((1, 20), (24, 22), (1000, 24), (10**400, 24)):
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
        self.assertEqual(place.call_args.args[1].size, 20)

    def test_large_image_uses_readable_adaptive_font_floor(self):
        Image.new("RGB", (2000, 2400), "white").save(self.source)
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
                        "font_size": 1,
                    }
                ],
            )
        self.assertEqual(place.call_args.args[1].size, 48)

    def test_color_pairs_contrast_with_light_dark_and_midgray_backgrounds(self):
        cases = (
            ((255, 255, 255), (typeset.TEXT_COLOR, (255, 255, 255))),
            ((10, 10, 10), ((255, 255, 255), (0, 0, 0))),
            ((128, 128, 128), ((0, 0, 0), (255, 255, 255))),
        )
        for background, expected in cases:
            with self.subTest(background=background):
                image = Image.new("RGB", (400, 400), background)
                self.assertEqual(
                    typeset._choose_text_colors(image, (50, 50, 200, 100)), expected
                )

    def test_solid_ink_remains_visible_on_midgray_after_downscaling(self):
        original = Image.new("RGB", (400, 400), (128, 128, 128))
        original.save(self.source)
        translation = "中灰背景的中文译文"
        output = typeset.render_translation(
            self.source,
            [
                {
                    "bbox": [250, 250, 750, 350],
                    "direction": "horizontal",
                    "translation": translation,
                    "font_size": 24,
                }
            ],
        )
        font = ImageFont.truetype(self.font.path, 22)
        rect, positions = typeset._place_translation(
            translation, font, (100, 100, 300, 140), "horizontal", original.size
        )
        thin = original.copy()
        draw = ImageDraw.Draw(thin)
        for x, y, char in positions:
            draw.text(
                (rect[0] + x, rect[1] + y),
                char,
                font=font,
                fill="black",
                stroke_width=1,
                stroke_fill="white",
            )
        with Image.open(output) as solid:
            for scale in (0.5, 0.375):
                with self.subTest(scale=scale):
                    size = (round(400 * scale), round(400 * scale))
                    before = thin.resize(size, Image.Resampling.LANCZOS).convert("L")
                    after = solid.resize(size, Image.Resampling.LANCZOS).convert("L")
                    before_dark = sum(value < 80 for value in before.getdata())
                    after_dark = sum(value < 80 for value in after.getdata())
                    self.assertGreater(before_dark, 0)
                    self.assertGreater(after_dark, before_dark * 1.3)

    def test_separate_labels_sample_their_own_background_before_drawing(self):
        original = Image.new("RGB", (400, 400), "white")
        ImageDraw.Draw(original).rectangle((200, 0, 399, 399), fill="black")
        original.save(self.source)
        regions = [
            {
                "bbox": bbox,
                "direction": "horizontal",
                "translation": "中文",
                "font_size": 24,
            }
            for bbox in ([250, 250, 500, 350], [625, 250, 875, 350])
        ]
        sampled_images = []
        real_choose = typeset._choose_text_colors

        def choose_colors(canvas, rect):
            sampled_images.append(canvas.tobytes())
            return real_choose(canvas, rect)

        with patch.object(typeset, "_choose_text_colors", side_effect=choose_colors):
            output = typeset.render_translation(self.source, regions)
        # Both selections see the untouched original, rather than earlier ink.
        self.assertEqual(sampled_images, [original.tobytes(), original.tobytes()])
        with Image.open(output) as rendered:
            left_colors = {
                color for _, color in rendered.crop((0, 140, 200, 200)).getcolors(40000)
            }
            right_colors = {
                color
                for _, color in rendered.crop((200, 140, 400, 200)).getcolors(40000)
            }
        self.assertIn(typeset.TEXT_COLOR, left_colors)
        self.assertIn((255, 255, 255), right_colors)

    def test_transparent_samples_ignore_hidden_rgb_and_sampling_stays_local(self):
        rect = (63, 71, 319, 287)
        pairs = []
        for hidden in (0, 255):
            image = Image.new("RGBA", (400, 400), (hidden, hidden, hidden, 0))
            with patch.object(image, "getpixel", wraps=image.getpixel) as sample:
                pairs.append(typeset._choose_text_colors(image, rect))
            self.assertEqual(sample.call_count, 256)
            for call in sample.call_args_list:
                x, y = call.args[0]
                self.assertTrue(rect[0] <= x < rect[2] and rect[1] <= y < rect[3])
        self.assertEqual(pairs[0], pairs[1])
        self.assertEqual(set(pairs[0]), {(0, 0, 0), (255, 255, 255)})

    def test_only_adaptive_glyphs_and_outlines_change_source_pixels(self):
        font = ImageFont.truetype(self.font.path, 22)
        rect, positions = typeset._place_translation(
            "中文", font, (100, 100, 200, 140), "horizontal", (400, 400)
        )
        mask = Image.new("L", (400, 400), 0)
        draw = ImageDraw.Draw(mask)
        for x, y, char in positions:
            draw.text(
                (rect[0] + x, rect[1] + y),
                char,
                font=font,
                fill=255,
                stroke_width=2,
                stroke_fill=255,
            )
        mask = mask.point(lambda value: 255 if value else 0)
        for mode, background in (("RGB", (70, 100, 150)), ("RGBA", (30, 60, 90, 70))):
            with self.subTest(mode=mode):
                original = Image.new(mode, (400, 400), background)
                original.save(self.source)
                output = typeset.render_translation(
                    self.source,
                    [
                        {
                            "bbox": [250, 250, 500, 350],
                            "direction": "horizontal",
                            "translation": "中文",
                            "font_size": 24,
                        }
                    ],
                )
                with Image.open(output) as rendered:
                    expected = original.copy()
                    expected.paste(rendered, mask=mask)
                    self.assertEqual(rendered.tobytes(), expected.tobytes())
                    self.assertNotEqual(rendered.tobytes(), original.tobytes())
                    self.assertEqual(rendered.getpixel((rect[0], rect[1])), background)
                    colors = {
                        color[:3]
                        for _, color in rendered.getcolors(maxcolors=400 * 400)
                    }
                    fill, stroke = typeset._choose_text_colors(original, rect)
                    self.assertIn(fill, colors)
                    self.assertIn(stroke, colors)

    def test_labels_skip_all_source_boxes_and_previous_labels(self):
        regions = [
            {
                "bbox": [250, 200, 500, 300],
                "direction": "horizontal",
                "translation": "中文",
                "font_size": 20,
            },
            {
                "bbox": [250, 310, 500, 410],
                "direction": "horizontal",
                "translation": "中文",
                "font_size": 20,
            },
        ]
        original = Image.new("RGB", (400, 400), "white")
        boxes = [(100, 80, 200, 120), (100, 124, 200, 164)]
        for box in boxes:
            ImageDraw.Draw(original).rectangle(
                (box[0], box[1], box[2] - 1, box[3] - 1), fill="red"
            )
        original.save(self.source)
        with patch.object(
            typeset, "_place_translation", wraps=typeset._place_translation
        ) as place:
            output = typeset.render_translation(self.source, regions)
        rects = []
        for call in place.call_args_list:
            # Reconstruct in rendering order without mock-retained mutable lists.
            args = call.args[:5]
            rect, _ = typeset._place_translation(*args, boxes, rects)
            rects.append(rect)
        self.assertGreaterEqual(rects[0][1], boxes[1][3] + typeset.GAP)
        self.assertGreaterEqual(rects[1][1], rects[0][3] + typeset.GAP)
        with Image.open(output) as rendered:
            for box in boxes:
                self.assertIsNone(
                    ImageChops.difference(
                        original.crop(box), rendered.crop(box)
                    ).getbbox()
                )

    def test_blockers_on_each_side_have_font_scaled_clearance(self):
        font = ImageFont.truetype(self.font.path, 40)
        gap = round(font.size * 0.4)
        for direction, box, blocker, side in (
            ("horizontal", (100, 100, 300, 150), (100, 155, 300, 200), "below"),
            ("horizontal", (100, 750, 300, 800), (100, 680, 300, 745), "above"),
            ("vertical", (650, 100, 700, 300), (580, 100, 645, 300), "left"),
            ("vertical", (0, 100, 50, 300), (55, 100, 120, 300), "right"),
        ):
            with self.subTest(side=side):
                rect, _ = typeset._place_translation(
                    "中文", font, box, direction, (800, 800), [box, blocker]
                )
                if side == "below":
                    self.assertGreaterEqual(rect[1], blocker[3] + gap)
                elif side == "above":
                    self.assertLessEqual(rect[3], blocker[1] - gap)
                elif side == "left":
                    self.assertLessEqual(rect[2], blocker[0] - gap)
                else:
                    self.assertGreaterEqual(rect[0], blocker[2] + gap)

    def test_collision_failure_creates_no_output(self):
        regions = [
            {
                "bbox": [0, 0, 1000, 450],
                "direction": "horizontal",
                "translation": "中文",
            },
            {
                "bbox": [0, 450, 1000, 1000],
                "direction": "horizontal",
                "translation": "中文",
            },
        ]
        with self.assertRaisesRegex(ValueError, "collision-free"):
            typeset.render_translation(self.source, regions)
        self.assertEqual(list(self.folder.iterdir()), [self.source])

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
