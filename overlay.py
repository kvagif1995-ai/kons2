"""Draw answers and the label PDF. The form artwork is never included."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

from form_layout import CHECKS, PAGE_H_MM, PAGE_W_MM, TEXT_FIELDS

FONT_PATH = Path(r"C:\Windows\Fonts\arial.ttf")
FONT_BOLD_PATH = Path(r"C:\Windows\Fonts\arialbd.ttf")
_fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def mm_to_px(mm: float, dpi: float) -> int:
    return int(round(mm / 25.4 * dpi))


def _font(size_px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    size_px = max(6, int(size_px))
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


def draw_checkmark(draw: ImageDraw.ImageDraw, cx: float, cy: float, dpi: float, fill=(0, 0, 0, 255)):
    s = dpi / 25.4
    # Centre the ink on the circle. A little larger and heavier than the first mark.
    raw = [(-0.95, 0.05), (-0.25, 0.75), (0.95, -0.95)]
    scale = 1.22
    mid_x = (min(p[0] for p in raw) + max(p[0] for p in raw)) / 2.0
    mid_y = (min(p[1] for p in raw) + max(p[1] for p in raw)) / 2.0
    pts = [
        (cx + (x - mid_x) * scale * s, cy + ((y - mid_y) * scale + 0.12) * s)
        for x, y in raw
    ]
    width = max(2, int(round(0.52 * s)))
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

    for field_id, spec in TEXT_FIELDS.items():
        raw = (values.get(field_id) or "").strip()
        if not raw:
            continue
        if field_id == "infekt_detail" and not checks.get("infekt_ja"):
            continue
        if "box" in spec:
            _draw_centered(draw, raw, spec, dpi_x, dpi_y, ink)
            continue
        lines_spec = spec["lines"]
        pt = spec.get("size") or lines_spec[0]["size"]
        widths = [max(1, mm_to_px(line["w"], dpi_x)) for line in lines_spec]
        if len(lines_spec) == 1:
            placed = [raw]
        else:
            placed = _wrap(draw, raw, widths, pt, dpi_y)
        for line, text in zip(lines_spec, placed):
            if not text:
                continue
            font = _fit_font(draw, text, mm_to_px(line["w"], dpi_x), line["size"], dpi_y)
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


def _draw_centered(draw: ImageDraw.ImageDraw, text: str, spec: dict, dpi_x: float, dpi_y: float, ink):
    box = spec["box"]
    max_w = max(1, mm_to_px(box["w"], dpi_x))
    max_h = max(1, mm_to_px(box["h"], dpi_y))
    bold = bool(spec.get("bold"))
    px = _pt_to_px(spec.get("size", 18), dpi_y)
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


def render_form_page(dpi_x: float, dpi_y: float) -> Image.Image:
    pdf_path = Path(__file__).resolve().parent / "Konsil_Formular_empty.pdf"
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
    )
    if overlay.size != form_page.size:
        overlay = overlay.resize(form_page.size, Image.Resampling.LANCZOS)
    return Image.alpha_composite(form_page, overlay)
