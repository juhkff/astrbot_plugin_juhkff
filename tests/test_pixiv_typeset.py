"""Local image regressions for conservative translation placement."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from juhkff_modules import pixiv_typeset as typeset
from PIL import Image, ImageChops, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


@unittest.skipUnless(Path(FONT).is_file(), "English fixture font unavailable")
class PixivTypesetTests(unittest.TestCase):
    """Exercise real Pillow rendering with a portable English fixture font."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "source.png"
        self.source = Image.new("RGB", (400, 400), "white")
        self.region = {
            "bbox": [250, 250, 500, 450],
            "text": "Hello world",
            "direction": "horizontal",
        }

    def render(self, regions=None):
        """Save and render the fixture.

        Args:
            regions: Optional region list.

        Returns:
            The decoded RGB output.
        """
        self.source.save(self.path)
        output = typeset.render_translation(self.path, regions or [self.region], FONT)
        with Image.open(output) as image:
            return image.convert("RGB")

    def test_safe_bubble_erases_only_interior_and_preserves_source(self):
        draw = ImageDraw.Draw(self.source)
        draw.rectangle((110, 115, 120, 125), fill="black")
        draw.rectangle((70, 70, 230, 210), outline="black", width=3)
        self.source.save(self.path)
        original_bytes = self.path.read_bytes()
        region = dict(self.region, text="Hi", bubble=[200, 200, 550, 500])
        output = typeset.render_translation(self.path, [region], FONT)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        with Image.open(output) as result:
            self.assertEqual(result.size, self.source.size)
            self.assertEqual(result.getpixel((115, 120)), (255, 255, 255))
            self.assertEqual(result.getpixel((70, 70)), (0, 0, 0))
            self.assertIsNotNone(ImageChops.difference(result, self.source).getbbox())

    def test_antialiased_text_is_erased_in_safe_bubble(self):
        font = ImageFont.truetype(FONT, 20)
        ImageDraw.Draw(self.source).text((110, 140), "Old", font=font, fill="black")
        region = dict(self.region, text="Hi", bubble=[200, 200, 550, 500])
        result = self.render([region])
        old_area = result.crop((105, 130, 200, 175))
        self.assertIsNone(
            ImageChops.difference(
                old_area, Image.new("RGB", old_area.size, "white")
            ).getbbox()
        )

    def test_nonblank_art_uses_margin_and_preserves_every_source_pixel(self):
        self.source = Image.new("RGB", (400, 400), (90, 30, 150))
        result = self.render([dict(self.region, bubble=[200, 200, 550, 500])])
        self.assertGreater(result.width, self.source.width)
        self.assertIsNone(
            ImageChops.difference(result.crop((0, 0, 400, 400)), self.source).getbbox()
        )
        self.assertIsNotNone(
            ImageChops.difference(
                result.crop((400, 0, result.width, result.height)),
                Image.new("RGB", (result.width - 400, result.height), "white"),
            ).getbbox()
        )

    def test_horizontal_blank_below_keeps_original_text(self):
        ImageDraw.Draw(self.source).rectangle((110, 110, 120, 120), fill="black")
        result = self.render()
        self.assertEqual(result.size, (400, 400))
        self.assertEqual(result.getpixel((115, 115)), (0, 0, 0))
        changed = ImageChops.difference(result, self.source).getbbox()
        self.assertGreaterEqual(changed[1], 184)

    def test_vertical_blank_side_keeps_original_text(self):
        self.region.update(direction="vertical", text="ABC")
        ImageDraw.Draw(self.source).rectangle((110, 110, 120, 120), fill="black")
        result = self.render()
        self.assertEqual(result.size, (400, 400))
        changed = ImageChops.difference(result, self.source).getbbox()
        self.assertGreaterEqual(changed[0], 204)
        self.assertEqual(result.getpixel((115, 115)), (0, 0, 0))

    def test_other_source_boxes_block_candidate(self):
        self.region.update(text="First")
        second = {
            "bbox": [250, 460, 500, 1000],
            "text": "Second",
            "direction": "horizontal",
        }
        result = self.render([self.region, second])
        self.assertGreater(result.width, 400)
        self.assertIsNone(
            ImageChops.difference(result.crop((0, 0, 400, 400)), self.source).getbbox()
        )

    def test_dense_or_colored_bubble_is_not_erased(self):
        for color in ("black", "red"):
            with self.subTest(color=color):
                self.source = Image.new("RGB", (400, 400), "white")
                ImageDraw.Draw(self.source).rectangle((100, 100, 199, 179), fill=color)
                region = dict(self.region, bubble=[200, 200, 550, 500])
                result = self.render([region])
                self.assertIsNone(
                    ImageChops.difference(
                        result.crop((100, 100, 200, 180)),
                        self.source.crop((100, 100, 200, 180)),
                    ).getbbox()
                )

    def test_large_oval_boundary_is_preserved_when_interior_is_used(self):
        self.source = Image.new("RGB", (1200, 1200), (80, 80, 80))
        draw = ImageDraw.Draw(self.source)
        draw.ellipse((120, 120, 1079, 1079), fill="white", outline="black", width=5)
        draw.rectangle((560, 560, 570, 570), fill="black")
        region = dict(
            self.region,
            bbox=[400, 400, 600, 600],
            bubble=[100, 100, 900, 900],
            text="Hi",
        )
        result = self.render([region])
        self.assertEqual(result.size, self.source.size)
        self.assertEqual(result.getpixel((565, 565)), (255, 255, 255))
        # Every pixel on and outside the oval outline survives the erase.
        mask = Image.new("L", self.source.size, 255)
        ImageDraw.Draw(mask).ellipse((125, 125, 1074, 1074), fill=0)
        self.assertIsNone(
            ImageChops.multiply(
                ImageChops.difference(result, self.source).convert("L"), mask
            ).getbbox()
        )

    def test_overwide_bubble_shrinks_away_from_art(self):
        draw = ImageDraw.Draw(self.source)
        draw.rectangle((40, 80, 65, 200), fill="red")
        draw.rectangle((110, 140, 120, 150), fill="black")
        region = dict(self.region, bubble=[100, 200, 800, 500], text="Hi")
        result = self.render([region])
        self.assertEqual(result.size, self.source.size)
        self.assertEqual(result.getpixel((115, 145)), (255, 255, 255))
        self.assertIsNone(
            ImageChops.difference(
                result.crop((40, 80, 66, 201)), self.source.crop((40, 80, 66, 201))
            ).getbbox()
        )

    def test_small_note_below_avoids_art_in_old_fixed_height(self):
        ImageDraw.Draw(self.source).rectangle((0, 240, 399, 399), fill="red")
        result = self.render([dict(self.region, text="Hi")])
        self.assertEqual(result.size, self.source.size)
        changed = ImageChops.difference(result, self.source).getbbox()
        self.assertGreaterEqual(changed[1], 184)
        self.assertLess(changed[3], 240)
        self.assertIsNone(
            ImageChops.difference(
                result.crop((0, 240, 400, 400)), self.source.crop((0, 240, 400, 400))
            ).getbbox()
        )

    def test_narrow_source_can_use_wider_compact_note(self):
        ImageDraw.Draw(self.source).rectangle((0, 240, 399, 399), fill="red")
        region = dict(self.region, bbox=[450, 250, 475, 450], text="Wide note here")
        result = self.render([region])
        self.assertEqual(result.size, self.source.size)
        changed = ImageChops.difference(result, self.source).getbbox()
        self.assertGreater(changed[2] - changed[0], 10)
        self.assertGreaterEqual(changed[1], 184)
        self.assertLess(changed[3], 240)

    def test_vertical_note_chooses_left_when_right_has_art(self):
        ImageDraw.Draw(self.source).rectangle((204, 0, 399, 399), fill="red")
        region = dict(self.region, direction="vertical", text="ABC")
        result = self.render([region])
        self.assertEqual(result.size, self.source.size)
        changed = ImageChops.difference(result, self.source).getbbox()
        self.assertLessEqual(changed[2], 96)
        self.assertIsNone(
            ImageChops.difference(
                result.crop((204, 0, 400, 400)), self.source.crop((204, 0, 400, 400))
            ).getbbox()
        )

    def test_unrendered_bubble_blocks_other_notes(self):
        # The first note's entire available space belongs to a later bubble.
        second = dict(
            self.region,
            bbox=[700, 700, 800, 800],
            bubble=[0, 460, 1000, 1000],
            text="Later",
        )
        result = self.render([self.region, second])
        self.assertGreater(result.width, 400)
        # The later bubble may render its own text; the first must be in margin.
        self.assertIsNone(
            ImageChops.difference(
                result.crop((100, 184, 200, 260)),
                self.source.crop((100, 184, 200, 260)),
            ).getbbox()
        )

    def test_occupied_note_rectangles_do_not_overlap(self):
        second = dict(self.region, bbox=[260, 250, 510, 450], text="Second")
        self.source.save(self.path)
        placements = []
        search = typeset._local_placement

        def tracked(*args):
            result = search(*args)
            if result[0]:
                rect = result[0][0]
                self.assertFalse(
                    any(typeset._intersects(rect, old) for old in placements)
                )
                placements.append(rect)
            return result

        with patch.object(typeset, "_local_placement", side_effect=tracked):
            typeset.render_translation(self.path, [self.region, second], FONT)
        self.assertTrue(placements)

    def test_bad_font_and_missing_chinese_glyphs_fail_clearly(self):
        self.source.save(self.path)
        with self.assertRaisesRegex(ValueError, "No usable font"):
            typeset.render_translation(self.path, [self.region], "/missing/font.ttf")
        with self.assertRaisesRegex(ValueError, "No usable font"):
            typeset.render_translation(
                self.path, [dict(self.region, text="中文")], FONT
            )
        with patch.object(typeset, "FONT_CANDIDATES", ("/missing/font.ttf",)):
            with self.assertRaisesRegex(ValueError, "No usable font"):
                typeset.render_translation(self.path, [self.region])

    def test_non_enclosing_bubble_uses_margin_and_preserves_source(self):
        # Model coordinates can differ by a fraction or miss the text entirely.
        for bubble in ([250.1, 250, 550, 500], [300, 300, 400, 400], [0, 0, 100, 100]):
            with self.subTest(bubble=bubble):
                ImageDraw.Draw(self.source).rectangle(
                    (110, 115, 120, 125), fill="black"
                )
                result = self.render([dict(self.region, bubble=bubble)])
                self.assertGreater(result.width, self.source.width)
                self.assertIsNone(
                    ImageChops.difference(
                        result.crop((0, 0, 400, 400)), self.source
                    ).getbbox()
                )
                self.assertIsNotNone(
                    ImageChops.difference(
                        result.crop((400, 0, result.width, result.height)),
                        Image.new("RGB", (result.width - 400, result.height), "white"),
                    ).getbbox()
                )

    def test_non_enclosing_bubble_still_blocks_other_notes(self):
        uncertain = dict(
            self.region,
            bbox=[700, 700, 800, 800],
            bubble=[0, 460, 650, 1000],
            text="Uncertain",
        )
        result = self.render([self.region, uncertain])
        self.assertGreater(result.width, 400)
        self.assertIsNone(
            ImageChops.difference(result.crop((0, 0, 400, 400)), self.source).getbbox()
        )

    def test_bad_bubble_keeps_other_valid_regions(self):
        uncertain = dict(self.region, bubble=[200, 460, 550, 1000], text="Uncertain")
        valid = dict(
            self.region,
            bbox=[700, 250, 800, 450],
            bubble=[650, 200, 850, 500],
            text="Hi",
        )
        ImageDraw.Draw(self.source).rectangle((285, 115, 290, 125), fill="black")
        result = self.render([uncertain, valid])
        self.assertGreater(result.width, 400)
        self.assertEqual(result.getpixel((287, 120)), (255, 255, 255))
        self.assertIsNone(
            ImageChops.difference(
                result.crop((80, 100, 220, 400)),
                self.source.crop((80, 100, 220, 400)),
            ).getbbox()
        )

    def test_invalid_contract_is_rejected(self):
        self.source.save(self.path)
        bad = [
            dict(self.region, bbox=[0, 0, 1001, 10]),
            dict(self.region, bbox=[0, 0, 0, 10]),
            dict(self.region, bbox=[True, 0, 20, 30]),
            dict(self.region, bbox=[float("nan"), 0, 20, 30]),
            dict(self.region, bbox=[0, 0, 20]),
            dict(self.region, bubble=None),
            dict(self.region, direction="diagonal"),
            dict(self.region, text=""),
            dict(self.region, text="x" * 2001),
            dict(self.region, text="bad\x00text"),
        ]
        for entry in bad:
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    typeset.render_translation(self.path, [entry], FONT)
        for regions in ([], [self.region] * 65, [None]):
            with self.assertRaises(ValueError):
                typeset.render_translation(self.path, regions, FONT)

    def test_hash_changes_with_image_text_and_font_bytes(self):
        self.source.save(self.path)
        first = typeset.render_translation(self.path, [self.region], FONT)
        repeat = typeset.render_translation(self.path, [self.region], FONT)
        text = typeset.render_translation(
            self.path, [dict(self.region, text="Different")], FONT
        )
        self.source.putpixel((0, 0), (5, 5, 5))
        self.source.save(self.path)
        changed = typeset.render_translation(self.path, [self.region], FONT)
        self.assertEqual(first, repeat)
        self.assertEqual(len({first, text, changed}), 3)
        other_font = Path(FONT).with_name("DejaVuSansMono.ttf")
        if other_font.is_file():
            font_output = typeset.render_translation(
                self.path, [self.region], str(other_font)
            )
            self.assertNotEqual(changed, font_output)

    def test_concurrent_writes_are_atomic_and_leave_no_temporary_files(self):
        self.source.save(self.path)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(
                pool.map(
                    lambda _: typeset.render_translation(
                        self.path, [self.region], FONT
                    ),
                    range(4),
                )
            )
        self.assertEqual(len(set(outputs)), 1)
        with Image.open(outputs[0]) as result:
            result.verify()
        self.assertEqual(list(self.path.parent.glob(".pixiv-typeset-*")), [])

    def test_background_inspection_budget_is_bounded(self):
        Image.new("RGB", (2200, 2200), "white").save(self.path)
        region = dict(self.region, bubble=[0, 0, 1000, 1000])
        inspected = 0
        check = typeset._flat_background

        def counted(image, rect, text_rect=None):
            nonlocal inspected
            inspected += (rect[2] - rect[0]) * (rect[3] - rect[1])
            return check(image, rect, text_rect)

        # Oversized candidates are skipped so smaller safe interiors still work.
        with patch.object(typeset, "_flat_background", side_effect=counted):
            output = typeset.render_translation(self.path, [region], FONT)
        self.assertLessEqual(inspected, 4_000_000)
        with Image.open(output) as result:
            self.assertEqual(result.size, (2200, 2200))

    def test_exhausted_candidate_search_preserves_sidebar_fallback(self):
        self.source = Image.new("RGB", (2200, 2200), "white")
        draw = ImageDraw.Draw(self.source)
        for y in range(16, 2200, 24):
            draw.line((0, y, 2199, y), fill="black")
        self.source.save(self.path)
        inspected = 0
        check = typeset._flat_background

        def counted(image, rect, text_rect=None):
            nonlocal inspected
            inspected += (rect[2] - rect[0]) * (rect[3] - rect[1])
            return check(image, rect, text_rect)

        region = dict(self.region, bubble=[0, 0, 1000, 1000])
        with patch.object(typeset, "_flat_background", side_effect=counted):
            output = typeset.render_translation(self.path, [region], FONT)
        self.assertGreater(inspected, 0)
        self.assertLessEqual(inspected, 4_000_000)
        with Image.open(output) as result:
            self.assertGreater(result.width, 2200)
            self.assertIsNone(
                ImageChops.difference(
                    result.crop((0, 0, 2200, 2200)), self.source
                ).getbbox()
            )

    def test_blank_note_prefers_readable_font_over_smallest_rectangle(self):
        self.source.save(self.path)
        selected = []
        search = typeset._local_placement

        def tracked(*args):
            result = search(*args)
            if result[0]:
                selected.append(result[0][3].size)
            return result

        with patch.object(typeset, "_local_placement", side_effect=tracked):
            typeset.render_translation(self.path, [self.region], FONT)
        self.assertEqual(selected, [32])

    def test_auto_detected_cjk_font_renders_chinese_and_japanese(self):
        if not any(Path(p).is_file() for p in typeset.FONT_CANDIDATES):
            self.skipTest("No system CJK font installed")
        self.source.save(self.path)
        output = typeset.render_translation(
            self.path, [dict(self.region, text="中文翻译 こんにちは")]
        )
        with Image.open(output) as result:
            self.assertIsNotNone(ImageChops.difference(result, self.source).getbbox())

    def test_oversize_source_is_rejected_before_rendering(self):
        Image.new("RGB", (8193, 1)).save(self.path)
        with self.assertRaisesRegex(ValueError, "limits"):
            typeset.render_translation(self.path, [self.region], FONT)


if __name__ == "__main__":
    unittest.main()
