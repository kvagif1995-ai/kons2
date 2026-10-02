"""Ward names and their Durchwahl, taken from the Konsil address list.

The number is the extension after 09352/503- (Station F6 → 60601).
"""

from __future__ import annotations

# Same wards as stations_by_prefix in the Kons project, with the full extension.
STATIONS: list[tuple[str, str]] = [
    ("3", "10301"),
    ("5 Oben", "10511"),
    ("5 Unten", "10501"),
    ("6 Oben", "10611"),
    ("6 Unten", "10601"),
    ("7", "10701"),
    ("9 Unten", "10901"),
    ("11", "11101"),
    ("18 Mitte", "11811"),
    ("18 Oben", "11821"),
    ("18 Unten", "11801"),
    ("19 Mitte", "11911"),
    ("19 Oben", "11921"),
    ("19 Unten", "11901"),
    ("F1", "60101"),
    ("F2", "60201"),
    ("F3", "60301"),
    ("F4", "60401"),
    ("F5", "60501"),
    ("F6", "60601"),
    ("F7", "60701"),
]


def _key(value: str) -> str:
    return "".join((value or "").split()).casefold()


def match_station(name: str) -> tuple[str, str] | None:
    key = _key(name)
    if not key:
        return None
    for label, phone in STATIONS:
        if _key(label) == key:
            return label, phone
    return None


def lookup_durchwahl(name: str) -> str:
    found = match_station(name)
    return found[1] if found else ""


def filter_stations(query: str) -> list[tuple[str, str]]:
    text = (query or "").strip().casefold()
    if not text:
        return list(STATIONS)
    return [
        (label, phone)
        for label, phone in STATIONS
        if text in label.casefold() or text in phone
    ]
