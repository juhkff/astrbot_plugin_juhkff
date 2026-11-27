"""Small synchronous Pillow renderer for translations beside source text."""

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MIN_FONT_SIZE = 20
MAX_FONT_SIZE = 128
MAX_PIXELS = 25_000_000
MAX_REGIONS = 64
MAX_REGION_CHARACTERS = 2000
MAX_TOTAL_CHARACTERS = 12000
GAP = 8
PADDING = 6
TEXT_COLOR = (24, 55, 105)
STROKE_COLOR = (255, 255, 255)
_LINEAR_RGB = tuple(
    value / 3294.6 if value <= 10 else ((value / 255 + 0.055) / 1.055) ** 2.4
    for value in range(256)
)
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


def _place_translation(
    text, font, box, direction, image_size, source_boxes=(), placed_boxes=()
):
    """Wrap glyphs and choose the requested side using their actual extent.

    Args:
        text: Validated translation string.
        font: Loaded Pillow font at its final bounded size.
        box: Source text rectangle in pixels.
        direction: Horizontal lines or vertical right-to-left columns.
        image_size: Source image width and height.
        source_boxes: All recognized source rectangles to avoid.
        placed_boxes: Previously placed translation label rectangles to avoid.

    Returns:
        A translation rectangle and glyph coordinates relative to that rectangle.

    Raises:
        ValueError: The translation cannot fit beside its source on this image.
    """
    width, height = image_size
    left, top, right, bottom = box
    vertical = direction == "vertical"
    padding = max(PADDING, round(font.size * 0.25))
    gap = max(GAP, round(font.size * 0.4))
    # Wrap along the source span, with a modest minimum for very small boxes.
    span = min(
        height if vertical else width,
        max(bottom - top if vertical else right - left, (font.size + 4) * 4),
    )
    limit = span - 2 * padding
    cell = font.size + 4
    positions = []
    along = cross = 0
    extent_along = extent_cross = 0
    for char in text:
        if char == "\n":
            along, cross = 0, cross + cell
            continue
        bounds = font.getbbox(char, stroke_width=2)
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
    text_width += 2 * padding
    text_height += 2 * padding
    if text_width > width or text_height > height:
        raise ValueError("wrapped translation exceeds image dimensions")
    if vertical:
        # The first vertical column is nearest the original text on the left.
        positions = [(extent_cross - cell + px, py, c) for px, py, c in positions]
    positions = [(padding + px, padding + py, c) for px, py, c in positions]
    obstacles = [box, *source_boxes, *placed_boxes]
    # Try the preferred side first, jumping past every intersecting rectangle.
    # Each jump clears at least one obstacle, so work stays bounded by the input.
    for preferred in (True, False):
        if vertical:
            x = left - gap - text_width if preferred else right + gap
            y = min(max(0, top), height - text_height)
        else:
            x = min(max(0, left), width - text_width)
            y = bottom + gap if preferred else top - gap - text_height
        for _ in range(len(obstacles) + 1):
            if x < 0 or y < 0 or x + text_width > width or y + text_height > height:
                break
            blockers = [
                obstacle
                for obstacle in obstacles
                if x < obstacle[2] + gap
                and x + text_width > obstacle[0] - gap
                and y < obstacle[3] + gap
                and y + text_height > obstacle[1] - gap
            ]
            if not blockers:
                return (x, y, x + text_width, y + text_height), positions
            if vertical:
                x = (
                    min(obstacle[0] for obstacle in blockers) - gap - text_width
                    if preferred
                    else max(obstacle[2] for obstacle in blockers) + gap
                )
            else:
                y = (
                    max(obstacle[3] for obstacle in blockers) + gap
                    if preferred
                    else min(obstacle[1] for obstacle in blockers) - gap - text_height
                )
    raise ValueError("translation has no collision-free room beside source")


def _choose_text_colors(
    image: Image.Image, rect: tuple[int, int, int, int]
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Choose outlined text from at most 256 local background samples.

    Prefer blue when its lower contrast quintile reaches 4.5:1; otherwise use
    the white/black pair with the stronger lower quintile and mean contrast.
    The outline contrasts with the fill to help on mixed backgrounds. Samples
    with transparency are evaluated over both black and white viewing mattes,
    because the eventual chat viewer's background is unknown. No pixels change.

    Args:
        image: Unmodified RGB or RGBA canvas.
        rect: Positive-area translation rectangle within the canvas.

    Returns:
        RGB fill and outline colors with contrasting lightness.
    """
    left, top, right, bottom = rect
    columns = min(16, right - left)
    rows = min(16, bottom - top)
    luminances = []
    for row in range(rows):
        y = top + (2 * row + 1) * (bottom - top) // (2 * rows)
        for column in range(columns):
            x = left + (2 * column + 1) * (right - left) // (2 * columns)
            pixel = image.getpixel((x, y))
            alpha = pixel[3] if len(pixel) == 4 else 255
            for matte in (0, 255) if alpha < 255 else (0,):
                channels = tuple(
                    (channel * alpha + matte * (255 - alpha) + 127) // 255
                    for channel in pixel[:3]
                )
                luminances.append(
                    0.2126 * _LINEAR_RGB[channels[0]]
                    + 0.7152 * _LINEAR_RGB[channels[1]]
                    + 0.0722 * _LINEAR_RGB[channels[2]]
                )
    candidates = (
        (TEXT_COLOR, STROKE_COLOR),
        ((255, 255, 255), (0, 0, 0)),
        ((0, 0, 0), (255, 255, 255)),
    )
    scores = []
    for fill, _ in candidates:
        luminance = (
            0.2126 * _LINEAR_RGB[fill[0]]
            + 0.7152 * _LINEAR_RGB[fill[1]]
            + 0.0722 * _LINEAR_RGB[fill[2]]
        )
        contrasts = sorted(
            (max(luminance, value) + 0.05) / (min(luminance, value) + 0.05)
            for value in luminances
        )
        scores.append((contrasts[len(contrasts) // 5], sum(contrasts) / len(contrasts)))
    if scores[0][0] >= 4.5:
        return candidates[0]
    return candidates[max(range(1, len(candidates)), key=scores.__getitem__)]


def render_translation(image: Path, regions: list[dict]) -> Path:
    """Render translations beside original text without erasing source pixels.

    All image and filesystem operations are synchronous. Callers in async code
    should use asyncio.to_thread. Labels avoid recognized source rectangles and
    earlier labels. Sparse local samples choose contrasting ink; artwork is not
    scanned for placement or erased. Insufficient space raises ValueError rather
    than shrinking or clipping text.

    Args:
        image: Local source image, preserved unchanged.
        regions: Up to 64 dictionaries containing a normalized bbox, direction
            (horizontal or vertical), translation, and optional positive finite
            font_size in source pixels. Coordinates are clipped to 0..1000.
            Font size is 90 percent of the estimate, bounded by image-adaptive
            limits (20..64 pixel floor and up to 128 pixel ceiling).

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
        short_edge = min(width, height)
        minimum = max(MIN_FONT_SIZE, min(64, round(short_edge * 0.024)))
        maximum = max(minimum, min(MAX_FONT_SIZE, round(short_edge * 0.06)))
        source_boxes = [
            (
                math.floor(box[0] * width / 1000),
                math.floor(box[1] * height / 1000),
                math.ceil(box[2] * width / 1000),
                math.ceil(box[3] * height / 1000),
            )
            for _, _, box, _ in cleaned
        ]
        labels = []
        placed_boxes = []
        for (text, direction, _, estimate), box in zip(cleaned, source_boxes):
            if estimate is None:
                estimate = (
                    box[2] - box[0] if direction == "vertical" else box[3] - box[1]
                )
            size = max(
                minimum,
                round(min(estimate, maximum / 0.90) * 0.90),
            )
            font = ImageFont.truetype(font_path, size)
            rect, positions = _place_translation(
                text, font, box, direction, rendered.size, source_boxes, placed_boxes
            )
            fill, stroke = _choose_text_colors(rendered, rect)
            labels.append((font, rect, positions, fill, stroke))
            placed_boxes.append(rect)
        draw = ImageDraw.Draw(rendered)
        for font, rect, positions, fill, stroke in labels:
            for x, y, char in positions:
                draw.text(
                    (rect[0] + x, rect[1] + y),
                    char,
                    font=font,
                    fill=fill,
                    stroke_width=2,
                    stroke_fill=stroke,
                )
                # Thicken the solid ink inside a one-pixel outer contour.
                # Regular CJK fonts otherwise look hollow after downscaling.
                draw.text(
                    (rect[0] + x, rect[1] + y),
                    char,
                    font=font,
                    fill=fill,
                    stroke_width=1,
                    stroke_fill=fill,
                )
        digest = hashlib.sha256(image.read_bytes())
        digest.update(json.dumps(cleaned, ensure_ascii=True).encode())
        digest.update(f"solid-adaptive-ink-v5:{font_path}".encode())
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
