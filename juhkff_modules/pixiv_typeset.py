"""Small synchronous Pillow renderer for translations beside source text."""

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MIN_FONT_SIZE = 14
MAX_FONT_SIZE = 32
MAX_PIXELS = 25_000_000
MAX_REGIONS = 64
MAX_REGION_CHARACTERS = 2000
MAX_TOTAL_CHARACTERS = 12000
GAP = 4
PADDING = 2
FONT_CANDIDATES = (
    str(Path(__file__).parent / "fonts" / "NotoSansCJK-Regular.ttc"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/local/share/fonts/NotoSansCJK-Regular.ttc",
    "/Library/Fonts/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)


def _place_translation(text, font, box, direction, image_size):
    """Wrap glyphs and choose the requested side using their actual extent.

    Args:
        text: Validated translation string.
        font: Loaded Pillow font at its final bounded size.
        box: Source text rectangle in pixels.
        direction: Horizontal lines or vertical right-to-left columns.
        image_size: Source image width and height.

    Returns:
        A translation rectangle and glyph coordinates relative to that rectangle.

    Raises:
        ValueError: The translation cannot fit beside its source on this image.
    """
    width, height = image_size
    left, top, right, bottom = box
    vertical = direction == "vertical"
    # Wrap along the source span, with a modest minimum for very small boxes.
    span = min(
        height if vertical else width,
        max(bottom - top if vertical else right - left, (font.size + 4) * 4),
    )
    limit = span - 2 * PADDING
    cell = font.size + 4
    positions = []
    along = cross = 0
    extent_along = extent_cross = 0
    for char in text:
        if char == "\n":
            along, cross = 0, cross + cell
            continue
        bounds = font.getbbox(char, stroke_width=1)
        glyph_width = bounds[2] - bounds[0]
        glyph_height = bounds[3] - bounds[1]
        advance = (
            max(cell, glyph_height)
            if vertical
            else max(1, math.ceil(font.getlength(char)), glyph_width)
        )
        if along and along + advance > limit:
            along, cross = 0, cross + cell
        if advance > limit or (glyph_width if vertical else glyph_height) > cell:
            raise ValueError("translation glyph cannot fit image")
        x, y = (-cross, along) if vertical else (along, cross)
        positions.append((x - bounds[0], y - bounds[1], char))
        extent_along = max(extent_along, along + advance)
        extent_cross = max(extent_cross, cross + cell)
        along += advance
    text_width, text_height = (
        (extent_cross, extent_along) if vertical else (extent_along, extent_cross)
    )
    text_width += 2 * PADDING
    text_height += 2 * PADDING
    if text_width > width or text_height > height:
        raise ValueError("wrapped translation exceeds image dimensions")
    if vertical:
        if left - GAP >= text_width:
            x = left - GAP - text_width
        elif width - right - GAP >= text_width:
            x = right + GAP
        else:
            raise ValueError("translation has no room left or right of source")
        y = min(max(0, top), height - text_height)
        # The first vertical column is nearest the original text on the left.
        positions = [(extent_cross - cell + px, py, c) for px, py, c in positions]
    else:
        if height - bottom - GAP >= text_height:
            y = bottom + GAP
        elif top - GAP >= text_height:
            y = top - GAP - text_height
        else:
            raise ValueError("translation has no room below or above source")
        x = min(max(0, left), width - text_width)
    return (x, y, x + text_width, y + text_height), positions


def render_translation(image: Path, regions: list[dict]) -> Path:
    """Render translations beside original text without erasing source pixels.

    All image and filesystem operations are synchronous. Callers in async code
    should use asyncio.to_thread. No artwork or other-region collision detection
    is performed. Insufficient space raises ValueError rather than clipping text.

    Args:
        image: Local source image, preserved unchanged.
        regions: Up to 64 dictionaries containing a normalized bbox, direction
            (horizontal or vertical), translation, and optional positive finite
            font_size in source pixels. Coordinates are clipped to 0..1000.
            Font size is 85 percent of the estimate, bounded to 14..32 pixels.

    Returns:
        A separate atomic PNG beside the source, or image for empty regions.

    Raises:
        ValueError: Malformed regions, resource limits, missing font, invalid
            image, insufficient space, or failure to write the rendered image.
    """
    if not isinstance(image, Path):
        raise ValueError("image must be a Path")
    if not isinstance(regions, list) or len(regions) > MAX_REGIONS:
        raise ValueError("regions must be a list of at most 64 entries")
    if not regions:
        return image
    cleaned = []
    total = 0
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("each region must be a dictionary")
        text = region.get("translation")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_REGION_CHARACTERS
            or any(ord(c) < 32 and c != "\n" for c in text)
        ):
            raise ValueError("translation must contain 1..2000 printable characters")
        direction = region.get("direction")
        if direction not in ("horizontal", "vertical"):
            raise ValueError("direction must be horizontal or vertical")
        box = region.get("bbox")
        if (
            not isinstance(box, (list, tuple))
            or len(box) != 4
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or (isinstance(v, float) and not math.isfinite(v))
                for v in box
            )
        ):
            raise ValueError("bbox must contain four finite numbers")
        box = tuple(max(0, min(1000, v)) for v in box)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("bbox must have positive clipped area")
        estimate = region.get("font_size")
        if "font_size" in region and (
            isinstance(estimate, bool)
            or not isinstance(estimate, (int, float))
            or estimate <= 0
            or (isinstance(estimate, float) and not math.isfinite(estimate))
        ):
            raise ValueError("font_size must be a positive finite number")
        total += len(text)
        cleaned.append((text.strip(), direction, box, estimate))
    if total > MAX_TOTAL_CHARACTERS:
        raise ValueError("total translation length exceeds 12000 characters")
    temporary = None
    rendered = None
    try:
        if image.stat().st_size > 64_000_000:
            raise ValueError("source image exceeds 64 MB")
        with Image.open(image) as source:
            width, height = source.size
            if max(width, height) > 8192 or width * height > MAX_PIXELS:
                raise ValueError("source image exceeds dimension/pixel limits")
            mode = (
                "RGBA"
                if "A" in source.getbands() or "transparency" in source.info
                else "RGB"
            )
            rendered = source.convert(mode)
        font_path = None
        for candidate in FONT_CANDIDATES:
            try:
                ImageFont.truetype(candidate, MIN_FONT_SIZE)
                font_path = candidate
                break
            except OSError:
                continue
        if font_path is None:
            raise ValueError("no existing CJK font available for translation")
        draw = ImageDraw.Draw(rendered)
        for text, direction, normalized_box, estimate in cleaned:
            box = (
                math.floor(normalized_box[0] * width / 1000),
                math.floor(normalized_box[1] * height / 1000),
                math.ceil(normalized_box[2] * width / 1000),
                math.ceil(normalized_box[3] * height / 1000),
            )
            if estimate is None:
                estimate = (
                    box[2] - box[0] if direction == "vertical" else box[3] - box[1]
                )
            size = max(
                MIN_FONT_SIZE,
                round(min(estimate, MAX_FONT_SIZE / 0.85) * 0.85),
            )
            font = ImageFont.truetype(font_path, size)
            rect, positions = _place_translation(
                text, font, box, direction, rendered.size
            )
            for x, y, char in positions:
                draw.text(
                    (rect[0] + PADDING + x, rect[1] + PADDING + y),
                    char,
                    font=font,
                    fill="black",
                    stroke_width=1,
                    stroke_fill="white",
                )
        digest = hashlib.sha256(image.read_bytes())
        digest.update(json.dumps(cleaned, ensure_ascii=True).encode())
        digest.update(f"simple-overlay-v1:{font_path}".encode())
        output = image.parent / f"{image.stem}.translated-{digest.hexdigest()}.png"
        with tempfile.NamedTemporaryFile(
            dir=image.parent, prefix=".pixiv-typeset-", suffix=".png", delete=False
        ) as handle:
            temporary = Path(handle.name)
            rendered.save(handle, format="PNG")
        temporary.chmod(0o644)
        os.replace(temporary, output)
        return output
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Cannot render translation: {exc}") from exc
    finally:
        if rendered is not None:
            rendered.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
