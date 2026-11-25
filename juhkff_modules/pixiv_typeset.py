"""Conservative Pillow translation placement, not complex artwork inpainting.

Only locally verified flat light rectangles may be erased. Ambiguous artwork is
preserved and translations are placed in an added margin with locator previews.
"""

import hashlib
import io
import json
import math
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MAX_PIXELS = 25_000_000
MAX_OUTPUT_PIXELS = 40_000_000
MAX_REGIONS = 64
FONT_CANDIDATES = (
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


def _intersects(a, b):
    """Return whether two half-open pixel rectangles overlap.

    Args:
        a: First rectangle.
        b: Second rectangle.

    Returns:
        Whether their interiors overlap.
    """
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _layout(text, font, width, height, vertical=False):
    """Fit wrapped glyphs without clipping, including font bearing offsets.

    Args:
        text: Translation to wrap.
        font: Pillow font instance.
        width: Available pixel width.
        height: Available pixel height.
        vertical: Use top-to-bottom columns ordered right-to-left.

    Returns:
        A list of glyph positions, or None when text cannot fit.
    """
    cell = font.size + 4
    if width < cell or height < cell:
        return None
    positions = []
    if vertical:
        x, y = width - cell, 0
        for char in text:
            if char == "\n" or y + cell > height:
                x, y = x - cell, 0
            if char == "\n":
                continue
            if x < 0:
                return None
            bounds = font.getbbox(char)
            if bounds[2] - bounds[0] > cell or bounds[3] - bounds[1] > cell:
                return None
            positions.append((x - bounds[0], y - bounds[1], char))
            y += cell
        return positions
    x, y = 0, 0
    for char in text:
        if char == "\n":
            x, y = 0, y + cell
            continue
        advance = max(1, math.ceil(font.getlength(char)))
        bounds = font.getbbox(char)
        glyph_width = bounds[2] - bounds[0]
        advance = max(advance, glyph_width)
        if x + advance > width:
            x, y = 0, y + cell
        if y + cell > height or advance > width or bounds[3] - bounds[1] > cell:
            return None
        positions.append((x - bounds[0], y - bounds[1], char))
        x += advance
    return positions


def _flat_background(image, rect, text_rect=None):
    """Check every pixel for a flat neutral background with sparse dark text.

    Args:
        image: Immutable RGB source image.
        rect: Candidate safe rectangle.
        text_rect: Only this source rectangle may contain dark pixels.

    Returns:
        Background color when safe, otherwise None.
    """
    left, top, right, bottom = rect
    if right - left < 8 or bottom - top < 8:
        return None
    pixels = image.load()
    corners = [
        pixels[x, y]
        for x, y in (
            (left, top),
            (right - 1, top),
            (left, bottom - 1),
            (right - 1, bottom - 1),
        )
    ]
    bg = tuple(sorted(c[i] for c in corners)[2] for i in range(3))
    if min(bg) < 180 or max(bg) - min(bg) > 20:
        return None
    dark = 0
    for y in range(top, bottom):
        for x in range(left, right):
            color = pixels[x, y]
            if max(abs(color[i] - bg[i]) for i in range(3)) <= 12:
                continue
            # Border pixels must match; all non-background ink must be neutral,
            # sparse and inside the declared original text rectangle.
            if (
                text_rect is None
                or x < left + 2
                or x >= right - 2
                or y < top + 2
                or y >= bottom - 2
                or not (
                    text_rect[0] <= x < text_rect[2]
                    and text_rect[1] <= y < text_rect[3]
                )
                or max(color) - min(color) > 18
                or max(color) > min(bg) - 12
            ):
                return None
            dark += 1
            if dark > (right - left) * (bottom - top) * 0.16:
                return None
    return bg


def render_translation(image: Path, regions: list[dict], font_path: str = "") -> Path:
    """Render validated translations without overwriting the source image.

    Args:
        image: Existing local source image path.
        regions: At most 64 dictionaries with normalized 0..1000 bbox, nonempty
            text and horizontal/vertical direction. Optional bubble must enclose
            bbox and describe an interior rectangle, excluding artwork/borders.
        font_path: Optional local TrueType/OpenType font; otherwise probe common
            Noto, WenQuanYi, macOS and Windows CJK font locations.

    Returns:
        Atomically written PNG beside image, named by input and font content hash.

    Raises:
        ValueError: Invalid regions, unsafe resource limits, unreadable image or
            unavailable font/glyphs. Callers may fall back to plain translation.
    """
    if not isinstance(image, Path) or not isinstance(font_path, str):
        raise ValueError("image must be a Path and font_path must be a string")
    if not isinstance(regions, list) or not 1 <= len(regions) <= MAX_REGIONS:
        raise ValueError("regions must contain 1..64 entries")
    cleaned = []
    total_text = 0
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("each region must be a dictionary")
        text = region.get("text")
        direction = region.get("direction")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("region text must contain 1..2000 characters")
        if any(ord(c) < 32 and c != "\n" for c in text):
            raise ValueError("region text contains unsupported control characters")
        if direction not in ("horizontal", "vertical"):
            raise ValueError("direction must be horizontal or vertical")
        total_text += len(text)
        entry = {"text": text, "direction": direction}
        for key in ("bbox", "bubble"):
            if key == "bubble" and key not in region:
                continue
            box = region.get(key)
            if (
                not isinstance(box, (list, tuple))
                or len(box) != 4
                or any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not 0 <= v <= 1000
                    for v in box
                )
                or box[0] >= box[2]
                or box[1] >= box[3]
            ):
                raise ValueError(
                    f"{key} must be a positive normalized 0..1000 rectangle"
                )
            entry[key] = list(box)
        if "bubble" in entry:
            b, t = entry["bubble"], entry["bbox"]
            if not (b[0] <= t[0] and b[1] <= t[1] and b[2] >= t[2] and b[3] >= t[3]):
                raise ValueError("bubble must enclose bbox")
        cleaned.append(entry)
    if total_text > 12000:
        raise ValueError("total translation length exceeds 12000 characters")
    try:
        if image.stat().st_size > 64_000_000:
            raise ValueError("source image exceeds 64 MB")
        source_bytes = image.read_bytes()
        with Image.open(io.BytesIO(source_bytes)) as source:
            width, height = source.size
            if max(width, height) > 8192 or width * height > MAX_PIXELS:
                raise ValueError("source image exceeds dimension/pixel limits")
            original = source.convert("RGB")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Cannot read source image: {exc}") from exc
    texts = "".join(r["text"] for r in cleaned) + "0123456789[](), -:"
    selected_font = None
    for candidate in [font_path] if font_path else FONT_CANDIDATES:
        try:
            if Path(candidate).stat().st_size > 64_000_000:
                continue
            font_bytes = Path(candidate).read_bytes()
            font = ImageFont.truetype(io.BytesIO(font_bytes), 20)
            missing = font.getmask(chr(0x10FFFF))
            missing_signature = (missing.size, bytes(missing))
            if any(
                (mask.size, bytes(mask)) == missing_signature
                for char in set(texts)
                if not char.isspace()
                for mask in [font.getmask(char)]
            ):
                continue
            selected_font = font_bytes
            break
        except (OSError, ValueError):
            continue
    if selected_font is None:
        raise ValueError(
            "No usable font with all translation glyphs; configure content_translation.font_path with a CJK font (Noto/WenQuanYi)"
        )
    digest = hashlib.sha256(source_bytes)
    digest.update(json.dumps(cleaned, ensure_ascii=False, sort_keys=True).encode())
    digest.update(selected_font)
    digest.update(b"pixiv-typeset-v1")
    output = image.parent / f"{image.stem}.translated-{digest.hexdigest()}.png"
    # Even an input named like a previous output must never be replaced.
    if output == image:
        raise ValueError("output path must differ from source")
    boxes = []
    bubbles = []
    for entry in cleaned:
        for key, target in (("bbox", boxes), ("bubble", bubbles)):
            if key not in entry:
                target.append(None)
                continue
            b = entry[key]
            rect = (
                math.floor(b[0] * width / 1000),
                math.floor(b[1] * height / 1000),
                math.ceil(b[2] * width / 1000),
                math.ceil(b[3] * height / 1000),
            )
            target.append(rect)
    rendered = original.copy()
    draw = ImageDraw.Draw(rendered)
    occupied = []
    pending = []
    checked_pixels = 0
    for index, entry in enumerate(cleaned):
        box, bubble = boxes[index], bubbles[index]
        candidates = []
        if (
            bubble
            and not any(
                _intersects(bubble, other)
                for j, other in enumerate(boxes)
                if j != index
            )
            and not any(
                other and _intersects(bubble, other)
                for j, other in enumerate(bubbles)
                if j != index
            )
        ):
            candidates.append((bubble, True))
        left, top, right, bottom = box
        if entry["direction"] == "horizontal":
            candidates.append(
                (
                    (left, min(height, bottom + 4), right, min(height, bottom + 244)),
                    False,
                )
            )
        else:
            candidates.extend(
                [
                    ((right + 4, top, min(width, right + 204), bottom), False),
                    ((max(0, left - 204), top, max(0, left - 4), bottom), False),
                ]
            )
        placed = False
        for rect, erase in candidates:
            if rect[0] >= rect[2] or rect[1] >= rect[3]:
                continue
            if any(_intersects(rect, other) for other in occupied):
                continue
            if not erase and any(_intersects(rect, other) for other in boxes):
                continue
            checked_pixels += (rect[2] - rect[0]) * (rect[3] - rect[1])
            if checked_pixels > 4_000_000:
                raise ValueError(
                    "translation background inspection exceeds pixel budget"
                )
            background = _flat_background(original, rect, box if erase else None)
            if background is None:
                continue
            for size in range(32, 11, -1):
                font = ImageFont.truetype(io.BytesIO(selected_font), size)
                layout = _layout(
                    entry["text"],
                    font,
                    rect[2] - rect[0] - 8,
                    rect[3] - rect[1] - 8,
                    entry["direction"] == "vertical",
                )
                if layout is not None:
                    if erase:
                        draw.rectangle(
                            (rect[0], rect[1], rect[2] - 1, rect[3] - 1),
                            fill=background,
                        )
                    for x, y, char in layout:
                        draw.text(
                            (rect[0] + 4 + x, rect[1] + 4 + y),
                            char,
                            font=font,
                            fill="black",
                        )
                    occupied.append(rect)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            pending.append((index + 1, entry))
    if pending:
        sidebar_width = 480
        font = ImageFont.truetype(io.BytesIO(selected_font), 20)
        rows, y = [], 12
        preview = original.copy()
        preview.thumbnail((120, 120))
        for number, entry in pending:
            # Locator previews live entirely in the new margin, never on artwork.
            thumbnail = preview.copy()
            box = boxes[number - 1]
            locator = ImageDraw.Draw(thumbnail)
            locator.rectangle(
                (
                    box[0] * thumbnail.width / width,
                    box[1] * thumbnail.height / height,
                    max(
                        box[0] * thumbnail.width / width,
                        box[2] * thumbnail.width / width - 1,
                    ),
                    max(
                        box[1] * thumbnail.height / height,
                        box[3] * thumbnail.height / height - 1,
                    ),
                ),
                outline="red",
                width=2,
            )
            label = f"[{number}] {entry['text']}"
            layout = _layout(label, font, sidebar_width - 24, 30000)
            if layout is None:
                raise ValueError("translation cannot fit output margin")
            row_height = (
                max((p[1] + font.getbbox(p[2])[3] for p in layout), default=0) + 20
            )
            rows.append((y, thumbnail, layout))
            y += thumbnail.height + 8 + row_height
        out_height = max(height, math.ceil(y + 12))
        if (
            out_height > 16384
            or (width + sidebar_width) * out_height > MAX_OUTPUT_PIXELS
        ):
            raise ValueError("translation output exceeds dimension/pixel limits")
        extended = Image.new("RGB", (width + sidebar_width, out_height), "white")
        extended.paste(rendered, (0, 0))
        rendered = extended
        draw = ImageDraw.Draw(rendered)
        for row_y, thumbnail, layout in rows:
            rendered.paste(thumbnail, (width + 12, row_y))
            for x, y, char in layout:
                draw.text(
                    (width + 12 + x, row_y + thumbnail.height + 8 + y),
                    char,
                    font=font,
                    fill="black",
                )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=image.parent, prefix=".pixiv-typeset-", suffix=".png", delete=False
        ) as handle:
            temporary = Path(handle.name)
            rendered.save(handle, format="PNG")
        os.replace(temporary, output)
    except OSError as exc:
        raise ValueError(f"Cannot save translated image: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output
