"""Positions on Konsil_Formular_empty.pdf, in millimetres from the top-left of the page.

The PDF is the scan of the physical sheet (209.93 x 296.93 mm). Text baselines sit
on the printed underlines. Checkbox coordinates are the centres of the open circles.
Nothing here is printed from the form itself — these are only where answers land.
"""

from __future__ import annotations

PAGE_W_MM = 595.0800170898438 / 72.0 * 25.4
PAGE_H_MM = 841.6799926757812 / 72.0 * 25.4

# How far a text baseline sits above its underline.
_ON_LINE = 0.65


def _line(x: float, underline_y: float, w: float, size: float = 10.0) -> dict:
    return {"x": x, "y": underline_y - _ON_LINE, "w": w, "size": size}


# id -> where the string is drawn. "lines" is filled in order, wrapping words.
TEXT_FIELDS: dict[str, dict] = {
    # Large specialty in the empty block under "BEZIRK UNTERFRANKEN", left of the address.
    "fachrichtung": {
        "box": {"x": 28.0, "y": 54.0, "w": 102.0, "h": 32.0},
        "size": 22,
        "bold": True,
    },
    "ihr_zeichen": {"lines": [_line(30.0, 93.05, 46.0, 9)], "size": 9},
    "unser_zeichen": {"lines": [_line(80.0, 93.05, 30.0, 9)], "size": 9},
    "sachbearbeiter": {"lines": [_line(115.0, 93.05, 28.0, 9)], "size": 9},
    "brief_datum": {"lines": [_line(132.0, 93.05, 18.0, 8.5)], "size": 8.5},
    "geb_am": {"lines": [_line(45.4, 121.68, 23.8)]},
    "station": {"lines": [_line(87.0, 121.68, 21.0)]},
    "beh_arzt": {
        "lines": [
            _line(166.0, 121.68, 41.5),
            _line(32.4, 128.00, 93.0),
        ]
    },
    "tel": {"lines": [_line(169.6, 128.00, 37.5)]},
    "fragestellung": {
        "lines": [
            _line(55.0, 140.76, 152.0, 9.5),
            _line(31.2, 147.11, 176.0, 9.5),
        ],
        "size": 9.5,
    },
    "vorbefunde": {
        "lines": [
            _line(66.0, 153.46, 141.0, 9.5),
            _line(31.2, 159.72, 176.0, 9.5),
        ],
        "size": 9.5,
    },
    "infekt_detail": {"lines": [_line(54.0, 170.36, 12.2, 8)]},
    "risiko_etc": {"lines": [_line(97.6, 178.86, 109.0, 9)]},
    "diagnose": {"lines": [_line(71.6, 197.90, 135.0)]},
    "oa_name": {"lines": [_line(32.0, 204.26, 74.0)]},
    "aa_name": {"lines": [_line(159.0, 204.26, 48.0)]},
    "kasse": {"lines": [_line(48.2, 229.66, 155.0)]},
    "hilfsmittel_text": {"lines": [_line(147.2, 255.12, 60.0, 9)]},
    "privat_text": {"lines": [_line(144.4, 261.58, 62.0, 9)]},
    "abrechnung_notiz": {"lines": [_line(36.2, 267.76, 170.0, 9)]},
    "termin": {"lines": [_line(49.2, 274.17, 40.0)]},
    "uhrzeit": {"lines": [_line(103.4, 274.17, 15.2)]},
    "begl_person": {"lines": [_line(144.0, 274.17, 35.0)]},
    "verw_datum": {"lines": [_line(60.0, 280.50, 33.5)]},
    "verw_name": {"lines": [_line(132.0, 280.50, 74.0)]},
    "begleit_anz": {"lines": [_line(97.0, 189.44, 9.5, 9)]},
}

# Circle centres (mm). The printed mark is drawn here, on the physical circle.
CHECKS: dict[str, tuple[float, float]] = {
    "infekt_ja": (46.3, 168.80),
    "infekt_nein": (72.1, 168.80),
    "infekt_nicht": (86.8, 168.80),
    "nuechtern": (46.4, 173.04),
    "sediert": (67.2, 173.04),
    "unzug": (84.9, 173.04),
    "epilepsie": (109.8, 173.04),
    "nicht_gehfaehig": (131.2, 173.04),
    "kommunikat": (160.3, 173.04),
    "suizidal": (46.4, 177.27),
    "fluchtgefahr": (65.3, 177.27),
    "gehend": (32.3, 183.62),
    "fahrdienst": (55.2, 183.62),
    "ktw": (96.8, 183.62),
    "liegend": (142.3, 183.62),
    "sitzend": (166.9, 183.62),
    "begleit_anz_mark": (46.4, 187.85),
    "begleit_nein": (111.2, 187.85),
    "begleit_station": (125.0, 187.85),
    "versichert": (32.3, 221.72),
    "befreit": (32.3, 234.42),
    "goae": (32.3, 240.83),
    "bema": (32.3, 247.18),
    "hilfsmittel": (32.3, 253.55),
    "privat": (32.3, 259.82),
}

# Patient label. The sticker PDF is a full page with the real content at the
# top-left. Place that block on the right, just above the red
# "Patientenetikett" line, clear of the red words.
STAMP_DEFAULT = {"x": 149.0, "y": 85.5, "w": 56.0, "h": 27.0}
