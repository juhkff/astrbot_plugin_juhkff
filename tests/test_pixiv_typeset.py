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

    def test_invalid_contract_is_rejected(self):
        self.source.save(self.path)
        bad = [
            dict(self.region, bbox=[0, 0, 1001, 10]),
            dict(self.region, bbox=[0, 0, 0, 10]),
            dict(self.region, bbox=[True, 0, 20, 30]),
            dict(self.region, bbox=[float("nan"), 0, 20, 30]),
            dict(self.region, bbox=[0, 0, 20]),
            dict(self.region, bubble=[300, 300, 400, 400]),
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
        with self.assertRaisesRegex(ValueError, "inspection exceeds pixel budget"):
            typeset.render_translation(self.path, [region], FONT)

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
