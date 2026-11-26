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


def _local_placement(original, entry, index, boxes, bubbles, occupied, fonts, budget):
    """Search bounded interior and nearby rectangles without relaxing ink checks.

    Args:
        original: Immutable RGB source.
        entry: Validated translation.
        index: Current region index.
        boxes: All source text rectangles.
        bubbles: All declared bubble boundaries, including unrendered ones.
        occupied: Previously placed translations.
        fonts: Descending font sizes to try.
        budget: Remaining pixel inspection allowance.

    Returns:
        A placement tuple or None, and the remaining inspection allowance.
    """
    width, height = original.size
    box, bubble = boxes[index], bubbles[index]
    left, top, right, bottom = box
    candidates = []
    if bubble:
        # The model supplies a boundary, not proof that its corners are blank.
        # Shrink each axis independently while retaining source ink and padding.
        inner = (left - 4, top - 4, right + 4, bottom + 4)
        if all(
            (
                inner[0] >= bubble[0],
                inner[1] >= bubble[1],
                inner[2] <= bubble[2],
                inner[3] <= bubble[3],
            )
        ):
            interiors = set()
            for fx in (0, 0.25, 0.5, 0.75, 1):
                for fy in (0, 0.25, 0.5, 0.75, 1):
                    interiors.add(
                        (
                            round(bubble[0] + (inner[0] - bubble[0]) * fx),
                            round(bubble[1] + (inner[1] - bubble[1]) * fy),
                            round(bubble[2] + (inner[2] - bubble[2]) * fx),
                            round(bubble[3] + (inner[3] - bubble[3]) * fy),
                        )
                    )
            # Larger safe interiors can accommodate more text at readable sizes.
            candidates.extend(
                (rect, True)
                for rect in sorted(
                    interiors, key=lambda r: (-(r[2] - r[0]) * (r[3] - r[1]), r)
                )
            )
    vertical = entry["direction"] == "vertical"
    anchor = bubble or box
    nearby = {}
    source_span = (bottom - top) if vertical else (right - left)
    page_span = height if vertical else width
    spans = sorted(
        {
            max(32, source_span),
            *(max(32, round(page_span * ratio)) for ratio in (0.12, 0.2, 0.32, 0.5)),
        }
    )
    for span in spans:
        # Derive compact rectangles from actual wrapping at several readable
        # sizes. No large empty tail is included in the background inspection.
        for font in (fonts[0], fonts[8], fonts[14], fonts[-1]):
            limit = min(width if vertical else height, max(64, round(page_span * 0.6)))
            layout = _layout(
                entry["text"],
                font,
                limit - 8 if vertical else span - 8,
                span - 8 if vertical else limit - 8,
                vertical,
            )
            if layout is None:
                continue
            cell = font.size + 4
            if vertical:
                last_column = min(x + font.getbbox(char)[0] for x, _, char in layout)
                columns = (limit - 8 - cell - last_column) // cell + 1
                extent = columns * cell + 8
                # Right first, then left; grow columns and available height.
                for gap in sorted({4, max(8, width // 100), max(16, width // 50)}):
                    for edge in (anchor[2] + gap, anchor[0] - gap):
                        x = edge if edge > right else edge - extent
                        for y in (top, (top + bottom - span) // 2, bottom - span):
                            rect = (
                                max(0, x),
                                max(0, y),
                                min(width, x + extent),
                                min(height, y + span),
                            )
                            nearby[rect] = max(font.size, nearby.get(rect, 0))
            else:
                last_row = max(y + font.getbbox(char)[1] for _, y, char in layout)
                rows = last_row // cell + 1
                extent = rows * cell + 8
                for gap in sorted({4, max(8, height // 100), max(16, height // 50)}):
                    for x in (left, (left + right - span) // 2, right - span):
                        y = anchor[3] + gap
                        rect = (
                            max(0, x),
                            y,
                            min(width, x + span),
                            min(height, y + extent),
                        )
                        nearby[rect] = max(font.size, nearby.get(rect, 0))
    # Prefer readable fonts, then compact notes near the source at that size.
    candidates.extend(
        (rect, False)
        for rect in sorted(
            nearby,
            key=lambda r: (
                -nearby[r],
                int(vertical and r[2] <= anchor[0]),
                (r[2] - r[0]) * (r[3] - r[1]),
                abs(r[0] - right) if vertical else abs(r[0] - left),
                r,
            ),
        )
    )
    for rect, erase in candidates:
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w < 24 or h < 24 or w * h > budget:
            continue
        if any(_intersects(rect, other) for other in occupied):
            continue
        if any(
            _intersects(rect, other)
            for j, other in enumerate(boxes)
            if not erase or j != index
        ):
            continue
        if any(
            other and _intersects(rect, other)
            for j, other in enumerate(bubbles)
            if not erase or j != index
        ):
            continue
        # Layout before scanning prevents spending the budget on unusable sizes.
        for font in fonts:
            layout = _layout(entry["text"], font, w - 8, h - 8, vertical)
            if layout is not None:
                break
        else:
            continue
        # Cheap corner rejection avoids spending a full scan on oval outlines.
        # This only rejects candidates; acceptance still checks every pixel.
        corners = [
            original.getpixel((x, y))
            for x in (rect[0], rect[2] - 1)
            for y in (rect[1], rect[3] - 1)
        ]
        budget -= 4
        if any(min(c) < 180 or max(c) - min(c) > 20 for c in corners):
            continue
        if w * h > budget:
            continue
        budget -= w * h
        background = _flat_background(original, rect, box if erase else None)
        if background is not None:
            return (rect, erase, background, font, layout), budget
    return None, budget


def render_translation(image: Path, regions: list[dict], font_path: str = "") -> Path:
    """Render validated translations without overwriting the source image.

    Args:
        image: Existing local source image path.
        regions: At most 64 dictionaries with normalized 0..1000 bbox, nonempty
            text and horizontal/vertical direction. Optional bubble should enclose
            bbox and describe an interior rectangle, excluding artwork/borders.
            Non-enclosing bubbles use the margin without changing source pixels.
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
    margin_only = set()
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
                # Keep both rectangles as obstacles for other translations, but
                # never erase or place this uncertain region on the source image.
                margin_only.add(len(cleaned))
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
    digest.update(b"pixiv-typeset-v2")
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
    budget = 4_000_000
    fonts = [
        ImageFont.truetype(io.BytesIO(selected_font), size)
        for size in range(32, 11, -1)
    ]
    for index, entry in enumerate(cleaned):
        if index in margin_only:
            pending.append((index + 1, entry))
            continue
        placement, budget = _local_placement(
            original, entry, index, boxes, bubbles, occupied, fonts, budget
        )
        if placement is None:
            pending.append((index + 1, entry))
            continue
        rect, erase, background, font, layout = placement
        if erase:
            draw.rectangle(
                (rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=background
            )
        for x, y, char in layout:
            draw.text((rect[0] + 4 + x, rect[1] + 4 + y), char, font=font, fill="black")
        occupied.append(rect)
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
