"""Draw answers and the label PDF. The form artwork is never included."""

from __future__ import annotations

import sys
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

from form_layout import CHECKS, PAGE_H_MM, PAGE_W_MM, SIGNATURE, TEXT_FIELDS

FONT_PATH = Path(r"C:\Windows\Fonts\arial.ttf")
FONT_BOLD_PATH = Path(r"C:\Windows\Fonts\arialbd.ttf")
_fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def mm_to_px(mm: float, dpi: float) -> int:
    return int(round(mm / 25.4 * dpi))


def _font(size_px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    size_px = max(4, int(size_px))
    key = (size_px, bold)
    hit = _fonts.get(key)
    if hit is not None:
        return hit
    path = FONT_BOLD_PATH if bold and FONT_BOLD_PATH.exists() else FONT_PATH
    if path.exists():
        font = ImageFont.truetype(str(path), size_px)
    else:
        font = ImageFont.load_default()
    _fonts[key] = font
    return font


def _pt_to_px(pt: float, dpi: float) -> int:
    return max(6, int(round(pt * dpi / 72.0)))


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    return draw.textlength(text, font=font)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, pt: float, dpi: float):
    px = _pt_to_px(pt, dpi)
    font = _font(px)
    while px > 6 and text and _text_width(draw, text, font) > max_w:
        px -= 1
        font = _font(px)
    return font


def _wrap(draw: ImageDraw.ImageDraw, text: str, widths: list[int], pt: float, dpi: float) -> list[str]:
    """Wrap words onto the given line widths. Explicit newlines start a new line."""
    font = _font(_pt_to_px(pt, dpi))
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    words: list[str] = []
    for i, para in enumerate(paragraphs):
        words.extend(para.split())
        if i < len(paragraphs) - 1:
            words.append("\n")

    lines = [""] * len(widths)
    idx = 0
    current: list[str] = []
    for word in words:
        if word == "\n":
            if idx < len(lines):
                lines[idx] = " ".join(current)
            current = []
            idx += 1
            continue
        if idx >= len(widths):
            break
        trial = " ".join(current + [word])
        if current and _text_width(draw, trial, font) > widths[idx]:
            lines[idx] = " ".join(current)
            current = [word]
            idx += 1
            if idx >= len(widths):
                break
        else:
            current.append(word)
    if idx < len(lines):
        lines[idx] = " ".join(current)
    elif current and lines:
        # Last line keeps the overflow and is shrunk when drawn.
        extra = " ".join(current)
        lines[-1] = (lines[-1] + " " + extra).strip()
    return lines


def normalize_flow(text: str) -> str:
    """One paragraph. Line breaks become spaces so the block can wrap."""
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").split())


def flow_lines(text: str, widths: list[float], measure) -> list[str] | None:
    """Wrap words onto the line widths. None when the text does not fit."""
    words = normalize_flow(text).split()
    if not words:
        return []
    if not widths or any(width <= 0 for width in widths):
        return None
    lines: list[list[str]] = []
    index = 0
    current: list[str] = []
    for word in words:
        if index >= len(widths):
            return None
        if not current and measure(word) > widths[index]:
            return None
        if current and measure(" ".join(current + [word])) > widths[index]:
            lines.append(current)
            current = []
            index += 1
            if index >= len(widths) or measure(word) > widths[index]:
                return None
        current.append(word)
    if current:
        lines.append(current)
    return [" ".join(parts) for parts in lines]


def _flow_force(text: str, widths: list[float], measure) -> list[str]:
    """Break long words so the text still lands on the given lines."""
    raw = normalize_flow(text)
    if not raw or not widths:
        return []
    lines: list[str] = []
    current = ""
    index = 0
    cursor = 0
    while cursor < len(raw) and index < len(widths):
        char = raw[cursor]
        if char == " " and not current:
            cursor += 1
            continue
        trial = current + char
        if current and measure(trial) > widths[index]:
            lines.append(current.strip())
            current = ""
            index += 1
            continue
        if not current and char != " " and measure(char) > widths[index]:
            lines.append(char)
            index += 1
            cursor += 1
            continue
        current = trial
        cursor += 1
    if current.strip() and index < len(widths):
        lines.append(current.strip())
    elif current.strip() and lines:
        lines[-1] = (lines[-1] + " " + current.strip()).strip()
    return lines


def fit_flow(
    text: str,
    widths: list[float],
    max_size: int,
    measure_for,
    linespace_for,
    gap: float,
    floor: int = 4,
) -> tuple[int, list[str]]:
    """Largest size at or below max_size that wraps inside the lines.

    A single line may use the full chosen size. Further lines also have to
    fit in the gap between one underline and the next.
    """
    cleaned = normalize_flow(text)
    size = max(floor, int(max_size))
    if not cleaned:
        return size, []

    def fits(at: int) -> list[str] | None:
        lines = flow_lines(cleaned, widths, measure_for(at))
        if lines is None:
            return None
        if len(lines) > 1 and linespace_for(at) > gap + 0.5:
            return None
        return lines

    while size > floor:
        placed = fits(size)
        if placed is not None:
            return size, placed
        size -= 1
    placed = fits(floor)
    if placed is not None:
        return floor, placed
    return floor, _flow_force(cleaned, widths, measure_for(floor))


def _shrink_font(draw: ImageDraw.ImageDraw, text: str, font, max_w: int):
    px = getattr(font, "size", 6)
    while px > 4 and text and _text_width(draw, text, font) > max_w:
        px -= 1
        font = _font(px)
    return font


def bold_flags(length: int, ranges) -> list[bool]:
    """True for each character covered by a half-open [start, end] range."""
    flags = [False] * max(0, int(length))
    for item in ranges or []:
        try:
            start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        start = max(0, min(len(flags), start))
        end = max(start, min(len(flags), end))
        for index in range(start, end):
            flags[index] = True
    return flags


def bold_ranges(flags: list[bool]) -> list[list[int]]:
    ranges: list[list[int]] = []
    start = None
    for index, bit in enumerate(flags):
        if bit and start is None:
            start = index
        elif not bit and start is not None:
            ranges.append([start, index])
            start = None
    if start is not None:
        ranges.append([start, len(flags)])
    return ranges


def _slice_width(text: str, flags: list[bool], measure_plain, measure_bold) -> float:
    if not text:
        return 0.0
    if not flags or not any(flags[: len(text)]):
        return float(measure_plain(text))
    total = 0.0
    index = 0
    while index < len(text):
        bit = bool(flags[index]) if index < len(flags) else False
        end = index + 1
        while end < len(text) and (bool(flags[end]) if end < len(flags) else False) == bit:
            end += 1
        measure = measure_bold if bit else measure_plain
        total += float(measure(text[index:end]))
        index = end
    return total


def _word_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index] == " ":
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] != " ":
            end += 1
        spans.append((index, end))
        index = end
    return spans


def flow_marked_lines(text: str, flags: list[bool], widths: list[float], measure) -> list[tuple[str, list[bool]]] | None:
    """Wrap on spaces. measure(start, end) is the width of text[start:end]."""
    spans = _word_spans(text)
    if not spans:
        return []
    if not widths or any(width <= 0 for width in widths):
        return None
    lines: list[tuple[int, int]] = []
    index = 0
    start = None
    end = None
    for word_start, word_end in spans:
        if index >= len(widths):
            return None
        if start is None:
            if measure(word_start, word_end) > widths[index]:
                return None
            start, end = word_start, word_end
            continue
        if measure(start, word_end) > widths[index]:
            lines.append((start, end))
            index += 1
            if index >= len(widths) or measure(word_start, word_end) > widths[index]:
                return None
            start, end = word_start, word_end
        else:
            end = word_end
    if start is not None:
        lines.append((start, end))
    return [(text[left:right], list(flags[left:right])) for left, right in lines]


def _flow_force_marked(text: str, flags: list[bool], widths: list[float], measure) -> list[tuple[str, list[bool]]]:
    """Break long words so the text still lands on the given lines."""
    if not text or not widths:
        return []
    lines: list[tuple[int, int]] = []
    index = 0
    start = 0
    cursor = 0
    while cursor < len(text) and index < len(widths):
        if text[cursor] == " " and cursor == start:
            cursor += 1
            start = cursor
            continue
        if measure(start, cursor + 1) > widths[index] and cursor > start:
            lines.append((start, cursor))
            index += 1
            if text[cursor] == " ":
                cursor += 1
            start = cursor
            continue
        if measure(start, cursor + 1) > widths[index] and cursor == start:
            cursor += 1
            lines.append((start, cursor))
            index += 1
            start = cursor
            continue
        cursor += 1
    if start < len(text):
        if index < len(widths):
            lines.append((start, len(text)))
        elif lines:
            left, _right = lines[-1]
            lines[-1] = (left, len(text))
    placed: list[tuple[str, list[bool]]] = []
    for left, right in lines:
        while left < right and text[left] == " ":
            left += 1
        while right > left and text[right - 1] == " ":
            right -= 1
        if left < right:
            placed.append((text[left:right], list(flags[left:right])))
    return placed


def fit_marked_flow(
    text: str,
    flags: list[bool],
    widths: list[float],
    max_size: int,
    measure_for,
    linespace_for,
    gap: float,
    floor: int = 4,
) -> tuple[int, list[str], list[list[bool]]]:
    """Like fit_flow, but bold characters use the bold measure."""
    size = max(floor, int(max_size))
    if len(flags) != len(text):
        flags = (list(flags) + [False] * len(text))[: len(text)]
    if not text:
        return size, [], []

    def fits(at: int):
        measure_run = measure_for(at)

        def measure(start: int, end: int, measure_run=measure_run) -> float:
            return _slice_width(
                text[start:end],
                flags[start:end],
                lambda sample: measure_run(sample, False),
                lambda sample: measure_run(sample, True),
            )

        placed = flow_marked_lines(text, flags, widths, measure)
        if placed is None:
            return None
        if len(placed) > 1 and linespace_for(at) > gap + 0.5:
            return None
        return placed

    while size > floor:
        placed = fits(size)
        if placed is not None:
            return size, [line for line, _bits in placed], [bits for _line, bits in placed]
        size -= 1
    placed = fits(floor)
    if placed is None:
        measure_run = measure_for(floor)

        def measure(start: int, end: int, measure_run=measure_run) -> float:
            return _slice_width(
                text[start:end],
                flags[start:end],
                lambda sample: measure_run(sample, False),
                lambda sample: measure_run(sample, True),
            )

        placed = _flow_force_marked(text, flags, widths, measure)
    return floor, [line for line, _bits in placed], [bits for _line, bits in placed]


def _draw_runs(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, flags: list[bool], regular, bold, ink):
    index = 0
    while index < len(text):
        bit = bool(flags[index]) if index < len(flags) else False
        end = index + 1
        while end < len(text) and (bool(flags[end]) if end < len(flags) else False) == bit:
            end += 1
        chunk = text[index:end]
        font = bold if bit else regular
        draw.text((x, y), chunk, font=font, fill=ink, anchor="ls")
        x += _text_width(draw, chunk, font)
        index = end


def _draw_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    lines_spec: list[dict],
    pt: float,
    dpi_x: float,
    dpi_y: float,
    ink,
    bold=None,
):
    """Wrap one paragraph onto the underlines, shrinking from the chosen size."""
    widths = [max(1, mm_to_px(line["w"], dpi_x)) for line in lines_spec]
    max_px = _pt_to_px(pt, dpi_y)
    if len(lines_spec) > 1:
        steps = [abs(lines_spec[i + 1]["y"] - lines_spec[i]["y"]) for i in range(len(lines_spec) - 1)]
        gap = mm_to_px(sum(steps) / len(steps), dpi_y)
    else:
        gap = 10**6
    cleaned = normalize_flow(text)
    flags = bold_flags(len(cleaned), bold)

    if not any(flags):
        def measure_for(size: int):
            font = _font(size)
            return lambda sample, font=font: _text_width(draw, sample, font)

        def linespace_for(size: int) -> int:
            ascent, descent = _line_box(_font(size))
            return ascent + descent

        px, placed = fit_flow(cleaned, widths, max_px, measure_for, linespace_for, gap, floor=4)
        font = _font(px)
        for line, content in zip(lines_spec, placed):
            if not content:
                continue
            use = _shrink_font(draw, content, font, max(1, mm_to_px(line["w"], dpi_x)))
            draw.text(
                (mm_to_px(line["x"], dpi_x), mm_to_px(line["y"], dpi_y)),
                content,
                font=use,
                fill=ink,
                anchor="ls",
            )
        return

    def measure_for(size: int):
        regular = _font(size, False)
        heavy = _font(size, True)

        def measure(sample: str, is_bold: bool, regular=regular, heavy=heavy) -> float:
            return _text_width(draw, sample, heavy if is_bold else regular)

        return measure

    def linespace_for(size: int) -> int:
        regular_ascent, regular_descent = _line_box(_font(size, False))
        bold_ascent, bold_descent = _line_box(_font(size, True))
        return max(regular_ascent, bold_ascent) + max(regular_descent, bold_descent)

    px, placed, line_flags = fit_marked_flow(
        cleaned, flags, widths, max_px, measure_for, linespace_for, gap, floor=4,
    )
    regular = _font(px, False)
    heavy = _font(px, True)
    for line, content, bits in zip(lines_spec, placed, line_flags):
        if not content:
            continue
        limit = max(1, mm_to_px(line["w"], dpi_x))
        use_regular, use_heavy = regular, heavy
        size = px
        while size > 4 and _slice_width(
            content,
            bits,
            lambda sample, font=use_regular: _text_width(draw, sample, font),
            lambda sample, font=use_heavy: _text_width(draw, sample, font),
        ) > limit:
            size -= 1
            use_regular = _font(size, False)
            use_heavy = _font(size, True)
        _draw_runs(
            draw,
            mm_to_px(line["x"], dpi_x),
            mm_to_px(line["y"], dpi_y),
            content,
            bits,
            use_regular,
            use_heavy,
            ink,
        )


def draw_checkmark(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    dpi: float,
    fill=(0, 0, 0, 255),
    ink_scale: float = 1.0,
):
    s = dpi / 25.4
    # Centre the ink on the circle. A little larger and heavier than the first mark.
    raw = [(-0.95, 0.05), (-0.25, 0.75), (0.95, -0.95)]
    scale = 1.22 * ink_scale
    mid_x = (min(p[0] for p in raw) + max(p[0] for p in raw)) / 2.0
    mid_y = (min(p[1] for p in raw) + max(p[1] for p in raw)) / 2.0
    pts = [
        (cx + (x - mid_x) * scale * s, cy + ((y - mid_y) * scale + 0.12) * s)
        for x, y in raw
    ]
    width = max(2, int(round(0.52 * s * ink_scale)))
    draw.line(pts, fill=fill, width=width, joint="curve")


def draw_plus(draw: ImageDraw.ImageDraw, cx: float, cy: float, dpi: float, fill=(180, 0, 0, 255)):
    s = dpi / 25.4
    arm = 1.6 * s
    width = max(1, int(round(0.25 * s)))
    draw.line([(cx - arm, cy), (cx + arm, cy)], fill=fill, width=width)
    draw.line([(cx, cy - arm), (cx, cy + arm)], fill=fill, width=width)


def _trim_content(image: Image.Image, pad_px: int) -> Image.Image:
    """Drop the empty page around a sticker that sits in one corner."""
    alpha = image.getchannel("A")
    mask = alpha.point(lambda value: 255 if value > 12 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    left = max(0, left - pad_px)
    top = max(0, top - pad_px)
    right = min(image.width, right + pad_px)
    bottom = min(image.height, bottom + pad_px)
    return image.crop((left, top, right, bottom))


def _plate_stamp(image: Image.Image, pad: int, border: int) -> Image.Image:
    """White card and a thin dark edge so the sticker does not mix with the form."""
    source = image.convert("RGBA")
    flat = Image.new("RGBA", source.size, (255, 255, 255, 255))
    flat.alpha_composite(source)
    margin = pad + border
    plate = Image.new("RGBA", (flat.width + margin * 2, flat.height + margin * 2), (255, 255, 255, 255))
    plate.alpha_composite(flat, (margin, margin))
    draw = ImageDraw.Draw(plate)
    # Stroke is centered on the line, so inset it or half the edge is clipped away.
    inset = border // 2
    draw.rectangle(
        (inset, inset, plate.width - 1 - inset, plate.height - 1 - inset),
        outline=(25, 25, 25, 255),
        width=border,
    )
    return plate


def load_stamp(
    path: str,
    box_w_mm: float,
    box_h_mm: float,
    dpi_x: float,
    dpi_y: float,
    mode: str,
) -> Image.Image | None:
    """Rasterise the first page of a label PDF into the stamp rectangle."""
    if not path or not Path(path).is_file():
        return None
    pdf = pdfium.PdfDocument(path)
    try:
        page = pdf[0]
        scale = dpi_x / 72.0
        rendered = page.render(scale=scale, fill_color=(255, 255, 255, 0)).to_pil().convert("RGBA")
    finally:
        pdf.close()

    box_w = max(1, mm_to_px(box_w_mm, dpi_x))
    box_h = max(1, mm_to_px(box_h_mm, dpi_y))
    if dpi_y != dpi_x and rendered.height:
        rendered = rendered.resize(
            (rendered.width, max(1, int(round(rendered.height * dpi_y / dpi_x)))),
            Image.Resampling.LANCZOS,
        )

    # Label PDFs from the label printer are a full page with the sticker in the corner.
    rendered = _trim_content(rendered, mm_to_px(0.4, dpi_x))
    border = max(2, mm_to_px(0.35, dpi_x))
    pad = max(border, mm_to_px(1.6, dpi_x))
    margin = pad + border
    limit_w = max(1, box_w - margin * 2)
    limit_h = max(1, box_h - margin * 2)
    if mode == "fit":
        factor = min(limit_w / rendered.width, limit_h / rendered.height)
    else:
        factor = min(1.0, limit_w / rendered.width, limit_h / rendered.height)
    if abs(factor - 1.0) > 0.01:
        size = (max(1, int(round(rendered.width * factor))), max(1, int(round(rendered.height * factor))))
        rendered = rendered.resize(size, Image.Resampling.LANCZOS)
    return _plate_stamp(rendered, pad, border)


def render_overlay(
    values: dict[str, str],
    checks: dict[str, bool],
    dpi_x: float,
    dpi_y: float,
    *,
    stamp: Image.Image | None = None,
    stamp_box: dict | None = None,
    stamp_align: str = "center",
    registration: bool = False,
    transparent: bool = True,
    ink_scale: float = 1.0,
    bold_marks: dict | None = None,
) -> Image.Image:
    """RGBA (or RGB) image of the A4 page with only the answers on it."""
    width = max(1, mm_to_px(PAGE_W_MM, dpi_x))
    height = max(1, mm_to_px(PAGE_H_MM, dpi_y))
    if transparent:
        image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    else:
        image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    ink = (0, 0, 0, 255) if transparent else (0, 0, 0)

    if stamp is not None and stamp_box:
        _paste_stamp(image, stamp, stamp_box, dpi_x, dpi_y, stamp_align)
    _paste_signature(image, dpi_x, dpi_y)

    for field_id, spec in TEXT_FIELDS.items():
        raw = (values.get(field_id) or "").strip()
        if not raw:
            continue
        if field_id == "infekt_detail" and not checks.get("infekt_ja"):
            continue
        if "box" in spec:
            _draw_centered(draw, raw, spec, dpi_x, dpi_y, ink, ink_scale)
            continue
        if spec.get("block"):
            _draw_block(
                draw,
                raw,
                spec["lines"],
                (spec.get("size") or spec["lines"][0]["size"]) * ink_scale,
                dpi_x,
                dpi_y,
                ink,
                (bold_marks or {}).get(field_id),
            )
            continue
        lines_spec = spec["lines"]
        pt = (spec.get("size") or lines_spec[0]["size"]) * ink_scale
        widths = [max(1, mm_to_px(line["w"], dpi_x)) for line in lines_spec]
        if len(lines_spec) == 1:
            placed = [raw]
        else:
            placed = _wrap(draw, raw, widths, pt, dpi_y)
        for line, text in zip(lines_spec, placed):
            if not text:
                continue
            font = _fit_font(draw, text, mm_to_px(line["w"], dpi_x), line["size"] * ink_scale, dpi_y)
            x = mm_to_px(line["x"], dpi_x)
            y = mm_to_px(line["y"], dpi_y)
            draw.text((x, y), text, font=font, fill=ink, anchor="ls")

    for check_id, (cx_mm, cy_mm) in CHECKS.items():
        if checks.get(check_id):
            draw_checkmark(
                draw,
                mm_to_px(cx_mm, dpi_x),
                mm_to_px(cy_mm, dpi_y),
                (dpi_x + dpi_y) / 2.0,
                ink,
                ink_scale,
            )

    if registration:
        mark = (180, 0, 0, 255) if transparent else (180, 0, 0)
        dpi = (dpi_x + dpi_y) / 2.0
        for spec in TEXT_FIELDS.values():
            if "box" in spec:
                box = spec["box"]
                draw_plus(
                    draw,
                    mm_to_px(box["x"] + box["w"] / 2, dpi_x),
                    mm_to_px(box["y"] + box["h"] / 2, dpi_y),
                    dpi,
                    mark,
                )
            else:
                first = spec["lines"][0]
                draw_plus(draw, mm_to_px(first["x"], dpi_x), mm_to_px(first["y"], dpi_y), dpi, mark)
        for cx_mm, cy_mm in CHECKS.values():
            draw_plus(draw, mm_to_px(cx_mm, dpi_x), mm_to_px(cy_mm, dpi_y), dpi, mark)
        if stamp_box:
            x0 = mm_to_px(stamp_box["x"], dpi_x)
            y0 = mm_to_px(stamp_box["y"], dpi_y)
            x1 = mm_to_px(stamp_box["x"] + stamp_box["w"], dpi_x)
            y1 = mm_to_px(stamp_box["y"] + stamp_box["h"], dpi_y)
            draw.rectangle((x0, y0, x1, y1), outline=mark, width=max(1, mm_to_px(0.3, dpi)))

    return image


def _ink_width(text: str, font) -> float:
    if hasattr(font, "getlength"):
        return float(font.getlength(text))
    box = font.getbbox(text)
    return float(box[2] - box[0])


def _two_lines(text: str, font, max_w: float) -> list[str]:
    """One line when it fits, otherwise two. An explicit newline forces the break."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    parts = [part.strip() for part in cleaned.split("\n")]
    if len(parts) > 2:
        parts = [parts[0], " ".join(part for part in parts[1:] if part)]
    parts = [part for part in parts if part]
    if not parts:
        return []
    if len(parts) >= 2:
        return parts[:2]

    words = parts[0].split()
    if len(words) < 2 or _ink_width(parts[0], font) <= max_w:
        return [parts[0]]
    taken: list[str] = []
    for word in words:
        trial = " ".join(taken + [word])
        if taken and _ink_width(trial, font) > max_w:
            break
        taken.append(word)
    if not taken:
        taken = [words[0]]
    rest = " ".join(words[len(taken):]).strip()
    if not rest:
        return [" ".join(taken)]
    return [" ".join(taken), rest]


def _line_box(font) -> tuple[int, int]:
    if hasattr(font, "getmetrics"):
        ascent, descent = font.getmetrics()
        return int(ascent), int(descent)
    bbox = font.getbbox("Hg")
    return max(1, int(bbox[3] - bbox[1])), 0


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    spec: dict,
    dpi_x: float,
    dpi_y: float,
    ink,
    ink_scale: float = 1.0,
):
    box = spec["box"]
    max_w = max(1, mm_to_px(box["w"], dpi_x))
    max_h = max(1, mm_to_px(box["h"], dpi_y))
    bold = bool(spec.get("bold"))
    px = _pt_to_px(spec.get("size", 18) * ink_scale, dpi_y)
    font = _font(px, bold)
    placed: list[str] = []
    while True:
        font = _font(px, bold)
        placed = _two_lines(text, font, max_w)
        ascent, descent = _line_box(font)
        line_h = ascent + descent
        gap = max(1, int(round(line_h * 0.12))) if len(placed) > 1 else 0
        block_h = line_h * len(placed) + gap * (len(placed) - 1)
        too_wide = any(_ink_width(line, font) > max_w for line in placed)
        if placed and not too_wide and block_h <= max_h:
            break
        if px <= 8:
            break
        px -= 1
    if not placed:
        return
    ascent, descent = _line_box(font)
    line_h = ascent + descent
    gap = max(1, int(round(line_h * 0.12))) if len(placed) > 1 else 0
    block_h = line_h * len(placed) + gap * (len(placed) - 1)
    cx = mm_to_px(box["x"] + box["w"] / 2.0, dpi_x)
    top = mm_to_px(box["y"] + box["h"] / 2.0, dpi_y) - block_h / 2.0
    for index, line in enumerate(placed):
        y = top + index * (line_h + gap) + line_h / 2.0
        draw.text((cx, y), line, font=font, fill=ink, anchor="mm")


_signature: Image.Image | None = None
_signature_loaded = False


def _load_signature() -> Image.Image | None:
    """Blue ink only. The white paper around the stroke stays transparent."""
    global _signature, _signature_loaded
    if _signature_loaded:
        return _signature
    _signature_loaded = True
    path = _bundle_dir() / "Unterschrift.png"
    if not path.is_file():
        _signature = None
        return None
    image = Image.open(path).convert("RGBA")
    pixels = []
    for red, green, blue, _alpha in image.getdata():
        if red >= 242 and green >= 242 and blue >= 242:
            pixels.append((red, green, blue, 0))
        else:
            pixels.append((red, green, blue, 255))
    image.putdata(pixels)
    alpha = image.getchannel("A")
    bbox = alpha.point(lambda value: 255 if value > 12 else 0).getbbox()
    if bbox:
        image = image.crop(bbox)
    _signature = image
    return _signature


def _paste_signature(image: Image.Image, dpi_x: float, dpi_y: float):
    signature = _load_signature()
    if signature is None or signature.width < 1 or signature.height < 1:
        return
    max_w = max(1, mm_to_px(SIGNATURE["w"], dpi_x))
    max_h = max(1, mm_to_px(SIGNATURE["h"], dpi_y))
    factor = min(max_w / signature.width, max_h / signature.height)
    size = (
        max(1, int(round(signature.width * factor))),
        max(1, int(round(signature.height * factor))),
    )
    scaled = signature.resize(size, Image.Resampling.LANCZOS)
    left = mm_to_px(SIGNATURE["x"], dpi_x) + (max_w - scaled.width) // 2
    top = mm_to_px(SIGNATURE["y"], dpi_y) + (max_h - scaled.height)
    _paste_rgba(image, scaled, left, top)


def _paste_rgba(image: Image.Image, sprite: Image.Image, left: int, top: int):
    sprite = sprite.convert("RGBA")
    if left < 0 or top < 0:
        sprite = sprite.crop((-min(0, left), -min(0, top), sprite.width, sprite.height))
        left = max(0, left)
        top = max(0, top)
    if left >= image.width or top >= image.height:
        return
    sprite = sprite.crop((0, 0, min(sprite.width, image.width - left), min(sprite.height, image.height - top)))
    if sprite.width <= 0 or sprite.height <= 0:
        return
    if image.mode == "RGBA":
        image.alpha_composite(sprite, (left, top))
    else:
        image.paste(sprite, (left, top), sprite)


def _paste_stamp(
    image: Image.Image,
    stamp: Image.Image,
    box: dict,
    dpi_x: float,
    dpi_y: float,
    align: str,
):
    stamp = stamp.convert("RGBA")
    box_w = mm_to_px(box["w"], dpi_x)
    box_h = mm_to_px(box["h"], dpi_y)
    origin_x = mm_to_px(box["x"], dpi_x)
    origin_y = mm_to_px(box["y"], dpi_y)
    if align == "center":
        left = origin_x + (box_w - stamp.width) // 2
        top = origin_y + (box_h - stamp.height) // 2
    elif align == "bottomright":
        left = origin_x + box_w - stamp.width
        top = origin_y + box_h - stamp.height
    else:
        left = origin_x
        top = origin_y

    if left < 0 or top < 0:
        stamp = stamp.crop((-min(0, left), -min(0, top), stamp.width, stamp.height))
        left = max(0, left)
        top = max(0, top)
    if left >= image.width or top >= image.height:
        return
    stamp = stamp.crop((0, 0, min(stamp.width, image.width - left), min(stamp.height, image.height - top)))
    if stamp.width <= 0 or stamp.height <= 0:
        return
    if image.mode == "RGBA":
        image.alpha_composite(stamp, (left, top))
    else:
        image.paste(stamp, (left, top), stamp)


def _bundle_dir() -> Path:
    """Folder that holds bundled files. Next to the sources, or inside a frozen exe."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def render_form_page(dpi_x: float, dpi_y: float) -> Image.Image:
    pdf_path = _bundle_dir() / "Konsil_Formular_empty.pdf"
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[0]
        rendered = page.render(scale=dpi_x / 72.0).to_pil().convert("RGBA")
    finally:
        pdf.close()
    target = (max(1, mm_to_px(PAGE_W_MM, dpi_x)), max(1, mm_to_px(PAGE_H_MM, dpi_y)))
    if rendered.size != target:
        rendered = rendered.resize(target, Image.Resampling.LANCZOS)
    return rendered


def compose_preview(
    values: dict[str, str],
    checks: dict[str, bool],
    dpi: float,
    stamp: Image.Image | None,
    stamp_box: dict | None,
    registration: bool,
    form_page: Image.Image | None = None,
    stamp_align: str = "center",
    ink_scale: float = 1.0,
    bold_marks: dict | None = None,
) -> Image.Image:
    """Screen image: scanned form in the background, answers on top. Not sent to the printer."""
    if form_page is None:
        form_page = render_form_page(dpi, dpi)
    overlay = render_overlay(
        values,
        checks,
        dpi,
        dpi,
        stamp=stamp,
        stamp_box=stamp_box,
        stamp_align=stamp_align,
        registration=registration,
        transparent=True,
        ink_scale=ink_scale,
        bold_marks=bold_marks,
    )
    if overlay.size != form_page.size:
        overlay = overlay.resize(form_page.size, Image.Resampling.LANCZOS)
    return Image.alpha_composite(form_page, overlay)
