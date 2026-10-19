"""Fill the Konsil form on screen and print only the answers onto the paper form.

Answers are typed straight onto the scanned form. The printer receives the text,
the marks and the chosen label PDF. Put the blank paper forms in the tray.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

import gdi_print
import overlay
from form_layout import CHECKS, STAMP_DEFAULT, TEXT_FIELDS
from overlay import mm_to_px
from stations import filter_stations, match_station

def _safe_stem(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " _-." else " " for ch in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:80] or "Konsil"


def _etikett_file(json_path: Path) -> Path:
    """Label PDF stored beside the Konsil, so the save does not depend on Downloads."""
    return json_path.with_name(json_path.stem + ".etikett.pdf")


def _stored_etikett(data: dict, folder: Path) -> str:
    """Prefer the copy saved with the Konsil. Fall back to the original path."""
    name = str(data.get("stamp_file") or "").strip()
    if name and Path(name).name == name:
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    return str(data.get("stamp_path") or "").strip()


def _parse_stamp_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_NAME_SKIP = {
    "landgericht", "amtsgericht", "forensik", "strasse", "straße", "str",
    "weg", "platz", "allee", "ring", "gasse", "am", "an", "der", "die", "das",
}


def _etikett_lines(path: Path) -> list[str]:
    text = ""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            textpage = pdf[0].get_textpage()
            try:
                text = textpage.get_text_bounded() or ""
            finally:
                textpage.close()
        finally:
            pdf.close()
    except Exception:
        return []
    lines: list[str] = []
    for raw in str(text).replace("\r", "\n").split("\n"):
        line = " ".join(raw.split())
        if line:
            lines.append(line)
    return lines


def _person_line(line: str) -> bool:
    """A patient name, not the address or the case number printed above it."""
    if any(ch.isdigit() for ch in line) or any(ch in line for ch in ".;:/" ):
        return False
    words = [word for word in line.replace(",", " ").split() if word]
    if not 2 <= len(words) <= 4:
        return False
    for word in words:
        plain = word.casefold().strip("-'")
        if not plain or plain in _NAME_SKIP:
            return False
        if not all(ch.isalpha() or ch in "-'" for ch in word):
            return False
    return True


def _etikett_caption(path: Path) -> str:
    """Patient name and birth date from the label. Empty when neither is found."""
    lines = _etikett_lines(path)
    name = ""
    born = ""
    for line in lines:
        if not born:
            found = re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", line)
            if found:
                born = found.group(0)
        if not name and _person_line(line):
            name = line
    if name and born:
        return f"{name}  geb. {born}"
    return name


def _etikett_preview(path: Path, height: int = 170) -> Image.Image | None:
    """Small picture of the sticker itself, without the empty page around it."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            rendered = pdf[0].render(scale=1.3, fill_color=(255, 255, 255, 0)).to_pil().convert("RGBA")
        finally:
            pdf.close()
        trimmed = overlay._trim_content(rendered, 8)
        plate = Image.new("RGB", trimmed.size, "white")
        plate.paste(trimmed, mask=trimmed.getchannel("A"))
        if plate.height > height:
            width = max(1, int(round(plate.width * height / plate.height)))
            plate = plate.resize((width, height), Image.Resampling.LANCZOS)
        if plate.width > 360:
            next_h = max(1, int(round(plate.height * 360 / plate.width)))
            plate = plate.resize((360, next_h), Image.Resampling.LANCZOS)
        return plate
    except Exception:
        return None


def _refresh_etikett_names(rows: list[dict]) -> None:
    """Replace an address caption with the patient name when the PDF has one."""
    for row in rows:
        file_name = str(row.get("file") or "")
        if not file_name or Path(file_name).name != file_name:
            continue
        path = ETIKETT_DIR / file_name
        if not path.is_file():
            continue
        caption = _etikett_caption(path)
        if caption:
            row["label"] = caption


def _etikett_title(path: Path, source_name: str, hint: str = "") -> str:
    """Prefer the text printed on the label, then the Konsil name, then the file name."""
    caption = _etikett_caption(path)
    if caption:
        return caption
    hint = " ".join(hint.split())
    if hint:
        return hint[:140]
    stem = Path(source_name).stem
    if stem.endswith(".etikett"):
        stem = stem[: -len(".etikett")]
    return stem or "Etikett"


def _read_etikett_index() -> list[dict]:
    if not ETIKETT_INDEX.is_file():
        return []
    try:
        data = json.loads(ETIKETT_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict) and row.get("id") and row.get("file")]


def _write_etikett_index(rows: list[dict]) -> None:
    ETIKETT_DIR.mkdir(exist_ok=True)
    ETIKETT_INDEX.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _purge_etiketten(rows: list[dict] | None = None) -> list[dict]:
    """Drop labels last used more than three months ago, and files with no index entry."""
    if rows is None:
        rows = _read_etikett_index()
    cutoff = datetime.now() - timedelta(days=ETIKETT_DAYS)
    kept: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("file") or "")
        if not name or Path(name).name != name:
            continue
        used = _parse_stamp_time(str(row.get("used_at") or ""))
        path = ETIKETT_DIR / name
        if used is None or used < cutoff or not path.is_file():
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        if name in seen:
            continue
        seen.add(name)
        kept.append(row)
    if ETIKETT_DIR.is_dir():
        for path in ETIKETT_DIR.glob("*.pdf"):
            if path.name not in seen:
                try:
                    path.unlink()
                except OSError:
                    pass
    kept.sort(key=lambda row: str(row.get("used_at") or ""), reverse=True)
    return kept


def remember_etikett(
    source: Path,
    *,
    touch: bool = True,
    used_at: datetime | None = None,
    label_hint: str = "",
) -> Path | None:
    """Keep one copy of a used label for three months. The same file updates its date."""
    if not source.is_file():
        return None
    when = used_at or datetime.now().replace(second=0, microsecond=0)
    cutoff = datetime.now() - timedelta(days=ETIKETT_DAYS)
    try:
        digest = _sha256_file(source)
        ETIKETT_DIR.mkdir(exist_ok=True)
        rows = _purge_etiketten()
    except OSError:
        return None
    file_name = f"{digest[:32]}.pdf"
    dest = ETIKETT_DIR / file_name
    existing = next((row for row in rows if row.get("id") == digest), None)
    if existing is None and when < cutoff:
        return None
    try:
        same = dest.is_file() and source.resolve() == dest.resolve()
    except OSError:
        same = False
    if not same and (not dest.is_file() or existing is None):
        try:
            shutil.copyfile(source, dest)
        except OSError:
            return None
    if existing is None:
        rows.insert(0, {
            "id": digest,
            "file": file_name,
            "label": _etikett_title(dest, source.name, label_hint),
            "source": source.name,
            "used_at": when.isoformat(timespec="minutes"),
        })
    elif touch:
        previous = _parse_stamp_time(str(existing.get("used_at") or ""))
        if previous is None or when > previous:
            existing["used_at"] = when.isoformat(timespec="minutes")
        caption = _etikett_caption(dest)
        if caption:
            existing["label"] = caption
        else:
            titled = _etikett_title(dest, source.name, label_hint)
            current = str(existing.get("label") or "")
            if titled and (not current or current == Path(str(existing.get("source") or "")).stem):
                existing["label"] = titled
    rows.sort(key=lambda row: str(row.get("used_at") or ""), reverse=True)
    try:
        _write_etikett_index(rows)
    except OSError:
        return dest if dest.is_file() else None
    return dest


def import_konsil_etiketten() -> None:
    """Bring labels already stored beside Konsils into the three-month list."""
    if not KONSIL_DIR.is_dir():
        return
    for path in KONSIL_DIR.glob("*.etikett.pdf"):
        try:
            used = datetime.fromtimestamp(path.stat().st_mtime).replace(second=0, microsecond=0)
        except OSError:
            continue
        remember_etikett(path, touch=False, used_at=used)


def _install_dir() -> Path:
    """Folder the user runs. Settings stay beside the exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _install_dir()
CONFIG_PATH = ROOT / "config.json"
KONSIL_DIR = ROOT / "konsile"
ETIKETT_DIR = ROOT / "etiketten"
ETIKETT_INDEX = ETIKETT_DIR / "index.json"
ETIKETT_DAYS = 90
PREVIEW_DPI = 120

# Filled on almost every form. Highlighted until the user turns that off.
IMPORTANT_DEFAULT = {
    "fachrichtung", "geb_am", "station", "beh_arzt", "tel",
    "fragestellung", "diagnose",
}
# Same from one form to the next. Restored on the next start.
REMEMBER_DEFAULT = {
    "fachrichtung", "station", "sachbearbeiter", "beh_arzt", "tel",
    "oa_name", "verw_name",
}
RISK_KEYS = (
    "nuechtern", "sediert", "unzug", "epilepsie",
    "nicht_gehfaehig", "kommunikat", "suizidal", "fluchtgefahr",
)
BILL_KEYS = ("versichert", "befreit", "goae", "bema", "hilfsmittel", "privat")
INFEKT_MARKS = {"infekt_ja": "ja", "infekt_nein": "nein", "infekt_nicht": "nicht"}
TRANSPORT_MARKS = {"gehend": "gehend", "fahrdienst": "fahrdienst", "ktw": "ktw"}
KTW_MARKS = {"liegend": "liegend", "sitzend": "sitzend"}
BEGLEIT_MARKS = {"begleit_anz_mark": "anz", "begleit_nein": "nein", "begleit_station": "station"}
CHECK_HIT_MM = 3.0

# Order in the Felder dialog, and the order of the boxes on the page.
FIELD_ORDER = (
    "fachrichtung",
    "ihr_zeichen", "unser_zeichen", "sachbearbeiter", "brief_datum",
    "stamp_path",
    "geb_am", "station", "beh_arzt", "tel",
    "fragestellung",
    "infekt", "infekt_detail",
    "risiken", "risiko_etc",
    "transport", "begleit_anz",
    "diagnose", "oa_name",
    "abrechnung", "kasse", "hilfsmittel_text", "privat_text", "abrechnung_notiz",
    "termin", "uhrzeit", "begl_person", "verw_datum", "verw_name",
)
CAPTIONS = {
    "fachrichtung": "Fachrichtung, links oben",
    "ihr_zeichen": "Ihr Zeichen",
    "unser_zeichen": "Unser Zeichen",
    "sachbearbeiter": "Sachbearbeiter",
    "brief_datum": "Datum oben",
    "stamp_path": "Etikett-PDF",
    "geb_am": "geb. am",
    "station": "Station",
    "beh_arzt": "Beh. Arzt im Bezirkskrankenhaus",
    "tel": "Tel.-Nr. (bei Rückfrage)",
    "fragestellung": "Fragestellung und wichtige Vorbefunde",
    "infekt": "Infektiosität",
    "infekt_detail": "Bei ja, kurzer Zusatz (z. B. HIV)",
    "risiken": "Kreuze, Risiken",
    "risiko_etc": "etc.",
    "transport": "Angaben zum Transport",
    "begleit_anz": "Anzahl Begleitpersonen",
    "diagnose": "Psychiatrische Diagnose",
    "oa_name": "Name Oberarzt",
    "abrechnung": "Kreuze, Abrechnung",
    "kasse": "Kasse",
    "hilfsmittel_text": "Hilfsmittel, Kostenträger",
    "privat_text": "Privatpatient, Zusatz",
    "abrechnung_notiz": "Freie Zeile unter der Abrechnung",
    "termin": "Termin am",
    "uhrzeit": "Uhrzeit",
    "begl_person": "Begl.-Person",
    "verw_datum": "Lohr a. Main, den",
    "verw_name": "Name Verwaltung",
}
# Paste this into ChatGPT. 350 characters stays at full size on the four underlines.
FRAGESTELLUNG_TIP = (
    "Erstelle die Fragestellung auf Deutsch aus den folgenden Rohinformationen. "
    "Die ersten Wörter tragen die Hauptaussage und die Wichtigkeit. "
    "Höchstens 350 Zeichen, etwa 45 Wörter. Kein einzelnes Wort länger als 70 Zeichen."
)
MARK_GROUPS = {
    "infekt": ("infekt_ja", "infekt_nein", "infekt_nicht"),
    "risiken": RISK_KEYS,
    "transport": (
        "gehend", "fahrdienst", "ktw", "liegend", "sitzend",
        "begleit_anz_mark", "begleit_nein", "begleit_station",
    ),
    "abrechnung": BILL_KEYS,
}

def _today() -> str:
    return date.today().strftime("%d.%m.%Y")


def _parse_mm(text: str, default: float) -> float:
    try:
        return float(str(text).strip().replace(",", "."))
    except ValueError:
        return default


def _font(size: int):
    path = overlay.FONT_PATH
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


class StationEntry(tk.Entry):
    """Type-to-search station list. Picking a ward fills its Durchwahl."""

    def __init__(self, master, on_pick):
        super().__init__(
            master,
            font=("Arial", 10),
            relief="flat",
            bd=0,
            highlightthickness=1,
            bg="#fffef8",
            fg="#142028",
            highlightbackground="#c5d0d6",
            highlightcolor="#1a6f8a",
            insertbackground="#142028",
            selectbackground="#1a6f8a",
            selectforeground="white",
        )
        self.on_pick = on_pick
        self._rows: list[tuple[str, str]] = []
        self._applied = ""
        self._popup: tk.Toplevel | None = None
        self._list: tk.Listbox | None = None
        self.bind("<KeyRelease>", self._on_key, add="+")
        self.bind("<Down>", self._on_down, add="+")
        self.bind("<Up>", self._on_up, add="+")
        self.bind("<Return>", self._on_return, add="+")
        self.bind("<Escape>", self._on_escape, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")
        self.bind("<Button-1>", self._on_click, add="+")

    def _ensure_popup(self):
        if self._popup is not None:
            return
        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        try:
            popup.attributes("-topmost", True)
        except tk.TclError:
            pass
        listing = tk.Listbox(
            popup,
            font=("Arial", 10),
            activestyle="dotbox",
            bg="#fffef8",
            fg="#142028",
            selectbackground="#1a6f8a",
            selectforeground="white",
            highlightthickness=1,
            highlightbackground="#1a6f8a",
            relief="flat",
            exportselection=False,
        )
        listing.pack(fill="both", expand=True)
        listing.bind("<ButtonRelease-1>", self._on_list_click)
        listing.bind("<Return>", self._on_return)
        self._popup = popup
        self._list = listing

    def accept_loaded(self):
        """Remember the station already on the form so a saved phone is kept."""
        self._applied = self.get().strip().casefold()
        self._hide()

    def _popup_open(self) -> bool:
        if self._popup is None:
            return False
        try:
            return bool(self._popup.winfo_viewable())
        except tk.TclError:
            return False

    def _hide(self):
        if self._popup is not None:
            self._popup.withdraw()

    def _on_click(self, _event):
        self.after_idle(self._show)

    def _on_key(self, event):
        if event.keysym in {
            "Up", "Down", "Return", "Escape", "Tab", "ISO_Left_Tab",
            "Shift_L", "Shift_R", "Left", "Right", "Home", "End",
        }:
            return
        self.after_idle(self._show)

    def _on_down(self, _event):
        if not self._popup_open():
            self._show()
            return "break"
        self._move(1)
        return "break"

    def _on_up(self, _event):
        if not self._popup_open():
            return "break"
        self._move(-1)
        return "break"

    def _on_escape(self, _event):
        self._hide()
        return "break"

    def _on_return(self, _event):
        if self._popup_open() and self._rows and self._list is not None:
            picked = self._list.curselection()
            self._choose(picked[0] if picked else 0)
            return "break"
        found = match_station(self.get())
        if found:
            self._apply(found[0], found[1], focus_phone=True)
        return "break"

    def _on_focus_out(self, _event):
        self.after(160, self._after_focus)

    def _after_focus(self):
        focus = self.focus_get()
        if self._list is not None and focus is self._list:
            return
        self._hide()
        found = match_station(self.get())
        if found and found[0].casefold() != self._applied:
            self._apply(found[0], found[1], focus_phone=False)

    def _on_list_click(self, _event):
        if self._list is None:
            return
        picked = self._list.curselection()
        if picked:
            self._choose(picked[0])

    def _move(self, step: int):
        if self._list is None or not self._rows:
            return
        picked = self._list.curselection()
        index = picked[0] if picked else -1
        index = max(0, min(len(self._rows) - 1, index + step))
        self._list.selection_clear(0, "end")
        self._list.selection_set(index)
        self._list.activate(index)
        self._list.see(index)

    def _show(self):
        if not self.winfo_viewable():
            return
        self._ensure_popup()
        assert self._list is not None and self._popup is not None
        self._rows = filter_stations(self.get())
        self._list.delete(0, "end")
        if not self._rows:
            self._hide()
            return
        for label, phone in self._rows:
            self._list.insert("end", f"{label}    {phone}")
        self._list.configure(height=min(8, len(self._rows)))
        self._list.selection_clear(0, "end")
        self._list.selection_set(0)
        self._list.activate(0)
        self.update_idletasks()
        width = max(self.winfo_width(), 168)
        height = self._list.winfo_reqheight() + 2
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        screen_h = self.winfo_screenheight()
        if y + height > screen_h - 8:
            y = max(0, self.winfo_rooty() - height)
        self._popup.geometry(f"{width}x{height}+{x}+{y}")
        self._popup.deiconify()
        self._popup.lift()

    def _choose(self, index: int):
        if index < 0 or index >= len(self._rows):
            return
        label, phone = self._rows[index]
        self._apply(label, phone, focus_phone=True)
        self._hide()

    def _apply(self, label: str, phone: str, focus_phone: bool):
        self._applied = label.casefold()
        if self.get() != label:
            self.delete(0, "end")
            self.insert(0, label)
        self.on_pick(label, phone, focus_phone)


def _display_bold_chars(widget: tk.Text, display: str) -> list[bool]:
    flags = [False] * len(display)
    ranges = widget.tag_ranges("bold")
    for start, end in zip(ranges[0::2], ranges[1::2]):
        left = len(widget.get("1.0", str(start)))
        right = len(widget.get("1.0", str(end)))
        for index in range(max(0, left), min(len(flags), right)):
            flags[index] = True
    return flags


def _normalize_display_bold(display: str, bold_at: list[bool]) -> tuple[str, list[bool]]:
    """Collapse whitespace the same way as the printed paragraph, keeping bold marks."""
    chars: list[str] = []
    flags: list[bool] = []
    index = 0
    size = len(display)
    while index < size and display[index].isspace():
        index += 1
    while index < size:
        if display[index].isspace():
            run = False
            while index < size and display[index].isspace():
                if index < len(bold_at) and bold_at[index]:
                    run = True
                index += 1
            if index >= size:
                break
            chars.append(" ")
            flags.append(run)
            continue
        chars.append(display[index])
        flags.append(bool(index < len(bold_at) and bold_at[index]))
        index += 1
    return "".join(chars), flags


def _display_norm_map(display: str) -> tuple[list[int | None], int]:
    mapping: list[int | None] = [None] * len(display)
    norm = 0
    index = 0
    size = len(display)
    while index < size and display[index].isspace():
        index += 1
    while index < size:
        if display[index].isspace():
            space_at = index
            while index < size and display[index].isspace():
                index += 1
            if index >= size:
                break
            mapping[space_at] = norm
            norm += 1
            continue
        mapping[index] = norm
        norm += 1
        index += 1
    return mapping, norm


def _norm_pos(mapping: list[int | None], total: int, offset: int) -> int:
    if offset <= 0:
        return 0
    if offset >= len(mapping):
        return total
    found = mapping[offset]
    if found is not None:
        return found
    for index in range(offset, len(mapping)):
        if mapping[index] is not None:
            return int(mapping[index])
    return total


def _span_is_bold(widget: tk.Text, start: str, end: str) -> bool:
    if widget.compare(start, ">=", end):
        return False
    pos = start
    while widget.compare(pos, "<", end):
        if "bold" not in widget.tag_names(pos):
            return False
        nxt = widget.index(f"{pos} + 1 char")
        if widget.compare(nxt, "==", pos):
            break
        pos = nxt
    return True


class FormApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Konsil — Druck auf Vordruck")
        self.geometry("1180x920")
        self.minsize(880, 680)
        self._ready = False
        self._after_id = None
        self._photo = None
        self._preview_scale = 1.0
        self._page_ox = 0.0
        self._page_oy = 0.0
        self._zoom = 100
        self._ink = 100
        self.zoom_pct = tk.StringVar(value="100 %")
        self.ink_pct = tk.StringVar(value="100 %")
        self._form_page = None
        self._stamp_key = None
        self._stamp_image = None
        self._printing = False
        self._image_id = None
        self._hover_id = None
        self._baseline_cache: dict[tuple, int] = {}
        self._wrap_draw = None
        self._display_image: Image.Image | None = None
        self._flow_chips: dict[str, list[tk.Label]] = {}
        self._flow_chip_items: dict[str, list[int]] = {}
        self._flow_photos: dict[str, list[ImageTk.PhotoImage | None]] = {}
        self._flow_busy = False

        self.bools: dict[str, tk.BooleanVar] = {}
        self.important: dict[str, tk.BooleanVar] = {}
        self.remember: dict[str, tk.BooleanVar] = {}
        self._field_order: list[str] = []
        self._field_inputs: dict[str, tk.Entry | tk.Text] = {}
        self._lines: dict[str, list[tk.Entry]] = {}
        self._windows: dict[str, int] = {}
        self._tab_chain: list[tk.Widget] = []

        self.printer = tk.StringVar()
        self.copies = tk.StringVar(value="1")
        self.shift_x = tk.StringVar(value="-10.0")
        self.shift_y = tk.StringVar(value="-2.0")
        self.stamp_path = tk.StringVar()
        self.stamp_x = tk.StringVar(value=f"{STAMP_DEFAULT['x']:.1f}")
        self.stamp_y = tk.StringVar(value=f"{STAMP_DEFAULT['y']:.1f}")
        self.stamp_w = tk.StringVar(value=f"{STAMP_DEFAULT['w']:.1f}")
        self.stamp_h = tk.StringVar(value=f"{STAMP_DEFAULT['h']:.1f}")
        self.stamp_mode = tk.StringVar(value="native")
        self.show_marks = tk.BooleanVar(value=False)
        self.paper_info = tk.StringVar(value="Kein Drucker gewählt")
        self.cursor_info = tk.StringVar(
            value="Ins Formular schreiben · Kreis anklicken · Umschalt+Klick setzt die Etikett-Ecke"
        )

        self.infekt = tk.StringVar(value="")
        self.transport = tk.StringVar(value="")
        self.ktw_mode = tk.StringVar(value="")
        self.begleit = tk.StringVar(value="")
        self._konsil_file: Path | None = None
        self._konsil_name = ""
        self._snapshot = ""
        self._question_bold: list[list[int]] = []

        self._build()
        self._load_config()
        self.after(200, self._keep_etiketten)
        self._snapshot = self._form_snapshot()
        self._refresh_title()
        self._refresh_printers()
        self._ready = True
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self.refresh_preview)
        self.after(120, self._update_paper_info)

    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=4)
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"), foreground="#1a6f8a")
        style.configure("Hint.TLabel", foreground="#3d4f57")
        style.configure("Status.TLabel", foreground="#1a6f8a")
        style.configure("Important.TLabel", font=("Segoe UI", 10, "bold"), foreground="#8a4b08")
        style.configure("Optional.TLabel", font=("Segoe UI", 10), foreground="#5c6b73")

        hint = ttk.Label(
            self,
            style="Hint.TLabel",
            text=(
                "Direkt ins Formular schreiben. Ein Kreis setzt oder löscht das Kreuz. "
                "Doppelklick auf das Etikett wählt die PDF, Umschalt+Klick setzt seine Ecke. "
                "Auswahl fett macht markierten Text in der Fragestellung fett, Strg+B ebenso. "
                "Gedruckt werden nur die Einträge und das Etikett — das Formular selbst bleibt auf dem Papier. "
                "Drucken öffnet den normalen Druckdialog. Unter Einstellungen: Auftrag → Privater Druck, "
                "Grundlagen → Quelle Universal. Formulare erst am Gerät in die Universalzufuhr legen "
                "und den Auftrag dort mit dem Code starten."
            ),
            wraplength=1100,
        )
        hint.pack(fill="x", padx=12, pady=(8, 4))
        self.bind(
            "<Configure>",
            lambda e, label=hint: label.configure(wraplength=max(280, e.width - 24)) if e.widget is self else None,
            add="+",
        )

        self._build_toolbar()
        self._build_preview()
        self._build_fields()
        self._build_printer_bar()

        for var in (
            self.shift_x, self.shift_y, self.stamp_x, self.stamp_y,
            self.stamp_w, self.stamp_h, self.stamp_path, self.stamp_mode,
        ):
            var.trace_add("write", lambda *_: self.schedule())
        self.show_marks.trace_add("write", lambda *_: self.schedule())

    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 4))

        row = ttk.Frame(bar)
        row.pack(fill="x")
        ttk.Button(row, text="Felder…", command=self.open_field_settings).pack(side="left", padx=(0, 4))
        ttk.Button(row, text="Beispiel füllen", command=self.fill_example).pack(side="left", padx=4)
        ttk.Button(row, text="Nicht gemerkte leeren", command=self.clear_fields).pack(side="left", padx=4)
        ttk.Button(row, text="Auswahl fett", command=self._toggle_question_bold).pack(side="left", padx=4)
        ttk.Button(row, text="Neu", command=self.new_konsil).pack(side="left", padx=4)
        ttk.Button(row, text="Speichern", command=self.save_konsil).pack(side="left", padx=4)
        ttk.Button(row, text="Speichern unter…", command=lambda: self.save_konsil(save_as=True)).pack(side="left", padx=4)
        ttk.Button(row, text="Öffnen…", command=self.open_dialog).pack(side="left", padx=4)
        ttk.Checkbutton(row, text="Passkreuze", variable=self.show_marks).pack(side="left", padx=(12, 4))

        tip = ttk.Frame(bar)
        tip.pack(fill="x", pady=(4, 0))
        ttk.Label(tip, text="ChatGPT").pack(side="left")
        tip_entry = ttk.Entry(tip)
        tip_entry.insert(0, FRAGESTELLUNG_TIP)
        tip_entry.configure(state="readonly")
        tip_entry.pack(side="left", fill="x", expand=True, padx=6)
        tip_entry.bind("<FocusIn>", lambda e: e.widget.selection_range(0, "end"))
        ttk.Button(tip, text="Kopieren", command=self._copy_frage_tip).pack(side="left")
        view = ttk.Frame(row)
        view.pack(side="right")
        self._stepper(view, "Zoom", self.zoom_pct, self._bump_zoom)
        self._stepper(view, "Schrift", self.ink_pct, self._bump_ink)

        stamp = ttk.Frame(bar)
        stamp.pack(fill="x", pady=(4, 0))
        ttk.Label(stamp, text="Etikett").pack(side="left")
        ttk.Entry(stamp, textvariable=self.stamp_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(stamp, text="Wählen…", command=self.browse_stamp).pack(side="left")
        ttk.Button(stamp, text="Gespeicherte…", command=self.open_saved_etikett).pack(side="left", padx=(6, 0))
        ttk.Button(stamp, text="Entfernen", command=lambda: self.stamp_path.set("")).pack(side="left", padx=(6, 8))
        ttk.Radiobutton(stamp, text="Originalgröße", variable=self.stamp_mode, value="native").pack(side="left")
        ttk.Radiobutton(stamp, text="einpassen", variable=self.stamp_mode, value="fit").pack(side="left", padx=(6, 8))
        self._mm_entry(stamp, self.stamp_x, "X")
        self._mm_entry(stamp, self.stamp_y, "Y")
        self._mm_entry(stamp, self.stamp_w, "B")
        self._mm_entry(stamp, self.stamp_h, "H")
        ttk.Label(bar, textvariable=self.cursor_info, style="Hint.TLabel").pack(fill="x", pady=(3, 0))

    def _copy_frage_tip(self):
        self.clipboard_clear()
        self.clipboard_append(FRAGESTELLUNG_TIP)
        self.update_idletasks()
        self.cursor_info.set("Fragestellung-Tipp ist in der Zwischenablage.")

    def _stepper(self, parent, caption: str, variable: tk.StringVar, command):
        ttk.Label(parent, text=caption).pack(side="left", padx=(12, 4))
        ttk.Button(parent, text="−", width=3, command=lambda: command(-10)).pack(side="left")
        ttk.Label(parent, textvariable=variable, width=6, anchor="center").pack(side="left", padx=2)
        ttk.Button(parent, text="+", width=3, command=lambda: command(10)).pack(side="left")

    def _bump_zoom(self, step: int):
        self._zoom = max(50, min(250, self._zoom + step))
        self.zoom_pct.set(f"{self._zoom} %")
        self.schedule()

    def _bump_ink(self, step: int):
        self._ink = max(100, min(200, self._ink + step))
        self.ink_pct.set(f"{self._ink} %")
        self.schedule()

    def _ink_factor(self) -> float:
        return self._ink / 100.0

    def _build_preview(self):
        holder = ttk.Frame(self)
        holder.pack(fill="both", expand=True, padx=8, pady=4)
        self.preview = tk.Canvas(holder, bg="#d5dee2", highlightthickness=0)
        yscroll = ttk.Scrollbar(holder, orient="vertical", command=self.preview.yview)
        xscroll = ttk.Scrollbar(holder, orient="horizontal", command=self.preview.xview)
        self.preview.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        xscroll.pack(side="bottom", fill="x")
        yscroll.pack(side="right", fill="y")
        self.preview.pack(side="left", fill="both", expand=True)
        self.preview.bind("<Configure>", lambda _e: self.schedule())
        self.preview.bind("<Motion>", self._on_preview_motion)
        self.preview.bind("<Button-1>", self._on_preview_click)
        self.preview.bind("<Double-Button-1>", self._on_preview_double)
        self.preview.bind("<MouseWheel>", self._wheel)
        self._hover_id = self.preview.create_oval(
            -20, -20, -20, -20, outline="#1a6f8a", width=2, state="hidden", tags="hover",
        )

    def _build_fields(self):
        for key in (*RISK_KEYS, *BILL_KEYS):
            self.bools[key] = tk.BooleanVar(value=False)
        for key in FIELD_ORDER:
            self._ensure_flags(key)
        for key in FIELD_ORDER:
            if key not in TEXT_FIELDS:
                continue
            spec = TEXT_FIELDS[key]
            if spec.get("block"):
                widget = self._make_flow(key)
                self._field_inputs[key] = widget
                self._remember_field(widget, paint=False)
            elif "lines" in spec and len(spec["lines"]) > 1:
                entries = [self._make_entry() for _ in spec["lines"]]
                self._lines[key] = entries
                self._field_inputs[key] = entries[0]
                for entry in entries:
                    self._remember_field(entry)
            elif "box" in spec:
                widget = self._make_block()
                self._field_inputs[key] = widget
                self._remember_field(widget, newline=True)
                self._limit_lines(widget, 2)
            elif key == "station":
                widget = StationEntry(self.preview, self._station_chosen)
                self._field_inputs[key] = widget
                self._embed(widget)
                self._remember_field(widget)
            else:
                widget = self._make_entry()
                self._field_inputs[key] = widget
                self._remember_field(widget)
        self._apply_all_importance()

    def _make_entry(self) -> tk.Entry:
        widget = tk.Entry(
            self.preview,
            font=("Arial", 10),
            relief="flat",
            bd=0,
            highlightthickness=1,
            bg="#fffef8",
            fg="#142028",
            highlightbackground="#c5d0d6",
            highlightcolor="#1a6f8a",
            insertbackground="#142028",
            selectbackground="#1a6f8a",
            selectforeground="white",
        )
        self._embed(widget)
        return widget

    def _make_block(self) -> tk.Text:
        widget = tk.Text(
            self.preview,
            font=("Arial", 14, "bold"),
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=1,
            bg="#fffef8",
            fg="#142028",
            highlightbackground="#c5d0d6",
            highlightcolor="#1a6f8a",
            insertbackground="#142028",
            selectbackground="#1a6f8a",
            selectforeground="white",
            undo=True,
            padx=4,
            pady=2,
        )
        widget.tag_configure("center", justify="center")
        widget.bind("<KeyRelease>", lambda _e, box=widget: box.tag_add("center", "1.0", "end"), add="+")
        self._embed(widget)
        return widget

    def _make_flow(self, key: str) -> tk.Text:
        """One wrapping box for a block that sits on several underlines."""
        widget = tk.Text(
            self.preview,
            font=("Arial", 10),
            wrap="none",
            relief="flat",
            bd=0,
            highlightthickness=1,
            bg="#fffef8",
            fg="#142028",
            highlightbackground="#c5d0d6",
            highlightcolor="#1a6f8a",
            insertbackground="#142028",
            selectbackground="#1a6f8a",
            selectforeground="white",
            undo=True,
            padx=1,
            pady=0,
            spacing1=0,
            spacing3=0,
            exportselection=False,
        )
        widget._flow_key = key
        widget._bold_hold = None
        widget.tag_configure("bold", font=("Arial", 10, "bold"))
        widget.tag_configure("flow", lmargin1=0, lmargin2=0)
        widget.tag_add("flow", "1.0", "end")
        self._embed(widget)
        widget.bind("<KeyRelease>", lambda _e, box=widget: self._on_flow_key(box), add="+")
        widget.bind("<<Paste>>", lambda _e, box=widget: box.after(1, lambda: self._on_flow_key(box)), add="+")
        widget.bind("<Control-b>", self._toggle_question_bold)
        widget.bind("<Control-B>", self._toggle_question_bold)
        self._flow_chips[key] = []
        self._flow_chip_items[key] = []
        self._flow_photos[key] = []
        return widget

    def _embed(self, widget: tk.Widget):
        item = self.preview.create_window(-2000, -2000, window=widget, anchor="nw", tags="field")
        self._windows[str(widget)] = item
        widget.bind("<FocusIn>", lambda _e, mark=item, box=widget: self._focus_field(mark, box), add="+")

    def _remember_field(self, widget: tk.Widget, newline: bool = False, paint: bool = True):
        self._tab_chain.append(widget)
        widget.bind("<MouseWheel>", self._wheel, add="+")
        widget.bind("<Tab>", self._tab_next, add="+")
        widget.bind("<Shift-Tab>", self._tab_prev, add="+")
        widget.bind("<ISO_Left_Tab>", self._tab_prev, add="+")
        if not newline:
            widget.bind("<Return>", self._tab_next, add="+")
            if paint:
                widget.bind("<KeyRelease>", lambda _e, box=widget: self._paint_overflow(box), add="+")

    def _focus_field(self, item: int, _widget: tk.Widget):
        self.preview.tag_raise(item)
        self._raise_flow_labels()
        self._reveal_item(item)

    def _raise_flow_labels(self):
        if self._flow_chip_items:
            self.preview.tag_raise("flow-label")

    def _reveal_item(self, item: int):
        _x, y = self.preview.coords(item)
        height = float(self.preview.itemcget(item, "height") or 0)
        view_h = self.preview.winfo_height()
        if view_h < 40:
            return
        top = self.preview.canvasy(0)
        if top + 12 <= y and y + height <= top + view_h - 12:
            return
        region = self.preview.bbox("page")
        total = (region[3] - region[1]) if region else 0
        if total <= view_h:
            return
        target = max(0.0, y - view_h * 0.28)
        self.preview.yview_moveto(target / total)
        self.preview.update_idletasks()

    def _limit_lines(self, widget: tk.Text, limit: int):
        def block_extra(event, box=widget):
            if event.keysym != "Return":
                return None
            current = int(box.index("end-1c").split(".")[0])
            if current >= limit:
                return "break"
            return None

        def trim_extra(_event=None, box=widget):
            end_line = int(box.index("end-1c").split(".")[0])
            if end_line <= limit:
                return
            extra = box.get(f"{limit + 1}.0", "end-1c").strip()
            box.delete(f"{limit}.end", "end")
            if extra:
                box.insert(f"{limit}.end", " " + extra)
            box.tag_add("center", "1.0", "end")

        widget.bind("<Return>", block_extra, add="+")
        widget.bind("<KeyRelease>", lambda _e: trim_extra(), add="+")
        widget.bind("<<Paste>>", lambda _e: widget.after(10, trim_extra), add="+")

    def _wheel(self, event):
        station = self._field_inputs.get("station")
        if isinstance(station, StationEntry):
            station._hide()
        if event.state & 0x0004:
            self._bump_zoom(10 if event.delta > 0 else -10)
            return "break"
        self.preview.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _tab_next(self, event):
        self._move_tab(event.widget, 1)
        return "break"

    def _tab_prev(self, event):
        self._move_tab(event.widget, -1)
        return "break"

    def _move_tab(self, widget, step: int):
        if not self._tab_chain:
            return
        try:
            index = self._tab_chain.index(widget)
        except ValueError:
            index = 0 if step > 0 else -1
        nxt = self._tab_chain[(index + step) % len(self._tab_chain)]
        item = self._windows.get(str(nxt))
        if item is not None:
            self._reveal_item(item)
        nxt.focus_set()

    def _paint_overflow(self, widget: tk.Entry):
        if not isinstance(widget, tk.Entry):
            return
        limit = getattr(widget, "_max_text_px", 0)
        if limit <= 0:
            return
        font = tkfont.Font(font=widget.cget("font"))
        too_wide = font.measure(widget.get()) > limit - 6
        widget.configure(fg="#8a1f1f" if too_wide else "#142028")

    def _style_input(self, widget: tk.Widget, important: bool):
        widget.configure(
            bg="#fff3cc" if important else "#fffef8",
            highlightbackground="#c47b12" if important else "#c5d0d6",
            highlightcolor="#1a6f8a",
        )

    def _widgets_for(self, key: str) -> list[tk.Widget]:
        lines = self._lines.get(key)
        if lines:
            return list(lines)
        widget = self._field_inputs.get(key)
        return [widget] if widget is not None else []

    def _px(self, mm: float) -> float:
        return mm / 25.4 * PREVIEW_DPI * self._preview_scale

    def _screen_pt(self, size_pt: float) -> int:
        ppi = self.winfo_fpixels("1i") or 96.0
        screen_px = size_pt * self._ink_factor() * (PREVIEW_DPI / 72.0) * self._preview_scale
        return max(6, int(round(screen_px * 72.0 / ppi)))

    def _baseline_offset(self, font_spec) -> int:
        key = tuple(font_spec)
        cached = self._baseline_cache.get(key)
        if cached is not None:
            return cached
        probe = tk.Entry(
            self, font=font_spec, relief="flat", bd=0, highlightthickness=1,
        )
        probe.insert(0, "Hg")
        probe.place(x=-800, y=-800)
        self.update_idletasks()
        box = probe.bbox(0)
        ascent = tkfont.Font(font=font_spec).metrics("ascent")
        offset = (box[1] + ascent) if box else (ascent + 1)
        probe.destroy()
        self._baseline_cache[key] = offset
        return offset

    def _layout_fields(self):
        if self._preview_scale <= 0 or not self._windows:
            return
        editing = not self.show_marks.get()
        for key, spec in TEXT_FIELDS.items():
            if spec.get("block"):
                self._layout_block(key, spec, editing)
            elif "box" in spec:
                self._layout_box(key, spec, editing)
            elif key in self._lines:
                for widget, line in zip(self._lines[key], spec["lines"]):
                    self._layout_line(widget, line, editing, bold=False)
            else:
                widget = self._field_inputs.get(key)
                if widget is not None:
                    self._layout_line(widget, spec["lines"][0], editing, bold=False)
        self._uncollide_fields()
        self._draw_importance_marks()
        self.preview.tag_raise("hover")
        self.preview.tag_raise("field")
        self._raise_flow_labels()

    def _layout_line(self, widget: tk.Entry, line: dict, editing: bool, bold: bool):
        size = float(line["size"])
        max_h = self._px(5.4 * self._ink_factor())
        pt = self._screen_pt(size)
        weight = "bold" if bold else "normal"
        font_spec = ("Arial", pt, "bold") if bold else ("Arial", pt)
        while pt > 6:
            font_spec = ("Arial", pt, "bold") if bold else ("Arial", pt)
            metrics = tkfont.Font(family="Arial", size=pt, weight=weight)
            if metrics.metrics("ascent") + metrics.metrics("descent") + 2 <= max_h:
                break
            pt -= 1
            font_spec = ("Arial", pt, "bold") if bold else ("Arial", pt)
        if getattr(widget, "_laid_pt", None) != pt or getattr(widget, "_laid_bold", None) != bold:
            widget.configure(font=font_spec)
            widget._laid_pt = pt
            widget._laid_bold = bold
        offset = self._baseline_offset(font_spec)
        descent = tkfont.Font(font=font_spec).metrics("descent")
        left = self._page_ox + self._px(line["x"])
        top = self._page_oy + self._px(line["y"]) - offset
        width = max(14, self._px(line["w"]))
        height = max(12, offset + descent + 2)
        widget._max_text_px = width
        self._move_window(widget, left, top, width, height, editing)
        self._paint_overflow(widget)

    def _on_flow_key(self, widget: tk.Text):
        if self._flow_busy or getattr(widget, "_flow_sync", False):
            return
        key = getattr(widget, "_flow_key", None)
        spec = TEXT_FIELDS.get(key or "")
        if not isinstance(spec, dict):
            return
        self._flow_busy = True
        try:
            if self._preview_scale > 0:
                self._layout_block(str(key), spec, not self.show_marks.get())
        finally:
            self._flow_busy = False

    def _max_line_pt(self, size: float) -> int:
        """Same ceiling as the other fields: the chosen Schrift, shrunk to the row."""
        max_h = self._px(5.4 * self._ink_factor())
        pt = self._screen_pt(size)
        while pt > 6:
            metrics = tkfont.Font(family="Arial", size=pt)
            if metrics.metrics("ascent") + metrics.metrics("descent") + 2 <= max_h:
                break
            pt -= 1
        return pt

    def _text_baseline(self, font_spec) -> int:
        key = ("text", tuple(font_spec))
        cached = self._baseline_cache.get(key)
        if cached is not None:
            return cached
        probe = tk.Text(
            self, font=font_spec, relief="flat", bd=0, highlightthickness=1,
            padx=1, pady=0, wrap="none",
        )
        probe.insert("1.0", "Hg")
        probe.place(x=-900, y=-900, width=180, height=70)
        self.update_idletasks()
        bbox = probe.bbox("1.0")
        ascent = tkfont.Font(font=font_spec).metrics("ascent")
        offset = (bbox[1] + ascent) if bbox else (ascent + 2)
        probe.destroy()
        self._baseline_cache[key] = offset
        return offset

    def _flow_display_lines(self, widget: tk.Text) -> int | None:
        try:
            widget.update_idletasks()
            if not widget.get("1.0", "end-1c").strip():
                return 1
            counted = widget.count("1.0", "end-1c", "displaylines")
        except tk.TclError:
            return None
        # Tk counts the breaks between display lines, so one wrapped line
        # reports 0 (None) and two wrapped lines report 1.
        if not counted:
            return 1
        return int(counted[0]) + 1

    def _apply_flow_font(self, widget: tk.Text, pt: int, gap_px: float):
        font_spec = ("Arial", max(1, int(pt)))
        widget.configure(font=font_spec, spacing2=0, spacing3=0)
        linespace = tkfont.Font(font=font_spec).metrics("linespace")
        # Each wrapped line is its own paragraph, so the gap sits below the line.
        widget.configure(spacing3=max(0, int(round(gap_px - linespace))))
        widget._laid_pt = pt
        widget.tag_configure("bold", font=("Arial", max(1, int(pt)), "bold"))
        widget.tag_add("flow", "1.0", "end")
        widget.tag_raise("bold")

    def _layout_block(self, key: str, spec: dict, editing: bool):
        widget = self._field_inputs.get(key)
        if not isinstance(widget, tk.Text):
            return
        lines = spec["lines"]
        left_mm = min(line["x"] for line in lines)
        right_mm = max(line["x"] + line["w"] for line in lines)
        max_pt = self._max_line_pt(float(spec.get("size") or lines[0]["size"]))
        pad = (
            int(widget.cget("highlightthickness") or 0)
            + int(widget.cget("padx") or 0)
            + int(widget.cget("borderwidth") or 0)
        )
        content_w = max(8, int(round(self._px(right_mm - left_mm))))
        indents = []
        wrap_widths = []
        for line in lines:
            indent = max(0, int(round(self._px(line["x"] - left_mm))))
            indent = min(indent, content_w - 8)
            indents.append(indent)
            wrap_widths.append(max(4, content_w - indent))
        outer_w = content_w + 2 * pad
        if len(lines) > 1:
            steps = [lines[i + 1]["y"] - lines[i]["y"] for i in range(len(lines) - 1)]
            gap_px = self._px(sum(steps) / len(steps))
        else:
            gap_px = 0.0
        span_px = gap_px * max(0, len(lines) - 1)
        self._apply_flow_font(widget, max_pt, gap_px)
        font_spec = ("Arial", max_pt)
        offset = self._text_baseline(font_spec)
        descent = tkfont.Font(font=font_spec).metrics("descent")
        left = self._page_ox + self._px(left_mm) - pad
        top = self._page_oy + self._px(lines[0]["y"]) - offset
        height = max(12, offset + span_px + descent + 2)
        self._move_window(widget, left, top, outer_w, height, editing)

        text, flags = self._flow_plain_and_flags(widget, consume_hold=True)
        if key == "fragestellung":
            self._question_bold = overlay.bold_ranges(flags)
        selection = self._flow_selection_norm(widget)

        def measure_for(pt: int):
            regular = tkfont.Font(family="Arial", size=max(1, pt))
            heavy = tkfont.Font(family="Arial", size=max(1, pt), weight="bold")

            def measure(sample: str, is_bold: bool, regular=regular, heavy=heavy) -> int:
                return (heavy if is_bold else regular).measure(sample)

            return measure

        def linespace_for(pt: int) -> int:
            regular = tkfont.Font(family="Arial", size=max(1, pt))
            heavy = tkfont.Font(family="Arial", size=max(1, pt), weight="bold")
            return max(regular.metrics("linespace"), heavy.metrics("linespace"))

        if any(flags):
            pt, placed, line_flags = overlay.fit_marked_flow(
                text, flags, wrap_widths, max_pt, measure_for, linespace_for, gap_px, floor=4,
            )
        else:
            pt, placed = overlay.fit_flow(
                text, wrap_widths, max_pt,
                lambda size: (lambda sample, size=size: tkfont.Font(family="Arial", size=max(1, size)).measure(sample)),
                lambda size: tkfont.Font(family="Arial", size=max(1, size)).metrics("linespace"),
                gap_px,
                floor=4,
            )
            line_flags = [[False] * len(line) for line in placed]
        self._apply_flow_font(widget, pt, gap_px)
        self._show_flow_lines(widget, placed, indents, line_flags, selection)

        font_spec = ("Arial", max(1, pt))
        offset = self._text_baseline(font_spec)
        descent = tkfont.Font(font=font_spec).metrics("descent")
        ascent = tkfont.Font(font=font_spec).metrics("ascent")
        top = self._page_oy + self._px(lines[0]["y"]) - offset
        height = max(12, offset + span_px + descent + 2)
        self._move_window(widget, left, top, outer_w, height, editing)
        self._place_flow_chips(key, widget, lines, left_mm, top, pad, descent, ascent, editing)

    def _show_flow_lines(
        self,
        widget: tk.Text,
        placed: list[str],
        indents: list[int],
        line_flags: list[list[bool]] | None = None,
        selection: tuple[int, int] | None = None,
    ):
        """Show the wrapped lines, each starting at its own underline."""
        display = "\n".join(placed)
        current = widget.get("1.0", "end-1c")
        widget._flow_sync = True
        try:
            if current != display:
                cursor = len(widget.get("1.0", "insert").replace("\r", "").replace("\n", " "))
                widget.delete("1.0", "end")
                if display:
                    widget.insert("1.0", display)
                self._move_flow_cursor(widget, display, cursor)
            count = int(widget.index("end-1c").split(".")[0]) if widget.get("1.0", "end-1c") else 0
            for index, margin in enumerate(indents):
                name = f"margin{index}"
                widget.tag_configure(name, lmargin1=margin, lmargin2=margin)
                widget.tag_raise(name)
                if index < count:
                    widget.tag_add(name, f"{index + 1}.0", f"{index + 1}.end")
            self._apply_line_bold(widget, line_flags or [])
            if selection is not None:
                self._restore_flow_selection(widget, placed, selection)
        finally:
            widget._flow_sync = False

    def _apply_line_bold(self, widget: tk.Text, line_flags: list[list[bool]]):
        widget.tag_remove("bold", "1.0", "end")
        for line_no, bits in enumerate(line_flags, start=1):
            column = 0
            while column < len(bits):
                if not bits[column]:
                    column += 1
                    continue
                end = column + 1
                while end < len(bits) and bits[end]:
                    end += 1
                widget.tag_add("bold", f"{line_no}.{column}", f"{line_no}.{end}")
                column = end
        widget.tag_raise("bold")

    def _flow_plain_and_flags(self, widget: tk.Text, *, consume_hold: bool) -> tuple[str, list[bool]]:
        display = widget.get("1.0", "end-1c")
        hold = getattr(widget, "_bold_hold", None)
        if hold is not None:
            text = overlay.normalize_flow(display)
            if consume_hold:
                widget._bold_hold = None
            return text, overlay.bold_flags(len(text), hold)
        return _normalize_display_bold(display, _display_bold_chars(widget, display))

    def _flow_selection_norm(self, widget: tk.Text) -> tuple[int, int] | None:
        try:
            start = widget.index("sel.first")
            end = widget.index("sel.last")
        except tk.TclError:
            return None
        display = widget.get("1.0", "end-1c")
        mapping, total = _display_norm_map(display)
        left = _norm_pos(mapping, total, len(widget.get("1.0", start)))
        right = _norm_pos(mapping, total, len(widget.get("1.0", end)))
        if right <= left:
            return None
        return left, right

    def _restore_flow_selection(self, widget: tk.Text, placed: list[str], selection: tuple[int, int]):
        start, end = selection
        if end <= start:
            return
        points: dict[str, tuple[int, int]] = {}
        pos = 0
        for line_no, line in enumerate(placed, start=1):
            for column in range(len(line) + 1):
                if pos == start and "start" not in points:
                    points["start"] = (line_no, column)
                if pos == end and "end" not in points:
                    points["end"] = (line_no, column)
                if column < len(line):
                    pos += 1
            if line_no < len(placed):
                pos += 1
        if pos == start and "start" not in points and placed:
            points["start"] = (len(placed), len(placed[-1]))
        if pos == end and "end" not in points and placed:
            points["end"] = (len(placed), len(placed[-1]))
        if "start" not in points or "end" not in points:
            return
        widget.tag_add(
            "sel",
            f"{points['start'][0]}.{points['start'][1]}",
            f"{points['end'][0]}.{points['end'][1]}",
        )
        widget.tag_raise("sel")

    def _toggle_question_bold(self, _event=None):
        widget = self._field_inputs.get("fragestellung")
        if not isinstance(widget, tk.Text):
            return "break"
        try:
            start = widget.index("sel.first")
            end = widget.index("sel.last")
        except tk.TclError:
            self.cursor_info.set("In der Fragestellung Text markieren, dann Fett oder Strg+B.")
            return "break"
        if _span_is_bold(widget, start, end):
            widget.tag_remove("bold", start, end)
            self.cursor_info.set("Markierung in der Fragestellung ist nicht mehr fett.")
        else:
            widget.tag_add("bold", start, end)
            widget.tag_raise("bold")
            self.cursor_info.set("Markierung in der Fragestellung ist fett.")
        self.schedule()
        return "break"

    def _question_bold_ranges(self) -> list[list[int]]:
        widget = self._field_inputs.get("fragestellung")
        if isinstance(widget, tk.Text):
            _text, flags = self._flow_plain_and_flags(widget, consume_hold=False)
            return overlay.bold_ranges(flags)
        return [list(item) for item in self._question_bold]

    def _hold_question_bold(self, ranges) -> None:
        widget = self._field_inputs.get("fragestellung")
        text = ""
        if isinstance(widget, tk.Text):
            text = overlay.normalize_flow(widget.get("1.0", "end-1c"))
        cleaned = overlay.bold_ranges(overlay.bold_flags(len(text), ranges or []))
        self._question_bold = cleaned
        if isinstance(widget, tk.Text):
            widget._bold_hold = cleaned

    def _move_flow_cursor(self, widget: tk.Text, display: str, index: int):
        index = max(0, min(index, len(display.replace("\n", " "))))
        line_no = 1
        column = 0
        seen = 0
        for char in display:
            if seen >= index:
                break
            seen += 1
            if char == "\n":
                line_no += 1
                column = 0
            else:
                column += 1
        widget.mark_set("insert", f"{line_no}.{column}")

    def _add_flow_chip(self, key: str, widget: tk.Text):
        chip = tk.Label(self.preview, bd=0, highlightthickness=0, padx=0, pady=0, bg="#ffffff")
        chip.bind("<Button-1>", lambda _e, box=widget: box.focus_set())
        item = self.preview.create_window(-2000, -2000, window=chip, anchor="nw", tags="flow-label")
        self._flow_chips[key].append(chip)
        self._flow_chip_items[key].append(item)
        self._flow_photos[key].append(None)

    def _place_flow_chips(
        self,
        key: str,
        widget: tk.Text,
        lines: list[dict],
        left_mm: float,
        widget_top: float,
        pad: int,
        descent: int,
        ascent: int,
        editing: bool,
    ):
        """Put each printed label back on top of the box so the cream does not hide it."""
        chips = self._flow_chips.get(key)
        items = self._flow_chip_items.get(key)
        if chips is None or items is None:
            return
        for item in items:
            self.preview.itemconfigure(item, state="hidden")
        if not editing or self._display_image is None:
            return
        used = 0
        for index, line in enumerate(lines):
            if line["x"] - left_mm < 0.4:
                continue
            while used >= len(chips):
                self._add_flow_chip(key, widget)
            chip = chips[used]
            item = items[used]
            chip_x = self._page_ox + self._px(left_mm)
            chip_w = max(1, int(round(self._px(line["x"] - left_mm))))
            base = self._page_oy + self._px(line["y"])
            chip_top = widget_top + pad if index == 0 else base - ascent
            next_top = None
            if index + 1 < len(lines):
                next_top = self._page_oy + self._px(lines[index + 1]["y"]) - ascent
            chip_bottom = base + descent + 1
            if next_top is not None:
                chip_bottom = min(chip_bottom, next_top - 1)
            chip_h = int(round(chip_bottom - chip_top))
            photo = self._chip_photo(chip_x, chip_top, chip_w, chip_h)
            if photo is None or chip_h < 2:
                used += 1
                continue
            self._flow_photos[key][used] = photo
            chip.configure(image=photo)
            self.preview.coords(item, chip_x, chip_top)
            self.preview.itemconfigure(item, width=chip_w, height=chip_h, state="normal")
            used += 1

    def _chip_photo(self, x: float, y: float, w: int, h: int):
        image = self._display_image
        if image is None or w < 2 or h < 2:
            return None
        ix = int(round(x - self._page_ox))
        iy = int(round(y - self._page_oy))
        ix2 = ix + int(w)
        iy2 = iy + int(h)
        if ix < 0 or iy < 0 or ix2 > image.width or iy2 > image.height:
            return None
        crop = image.crop((ix, iy, ix2, iy2))
        if crop.width < 1 or crop.height < 1:
            return None
        return ImageTk.PhotoImage(crop)

    def _uncollide_fields(self):
        """Keep two boxes on the same line from covering each other."""
        placed = []
        for key in TEXT_FIELDS:
            for widget in self._widgets_for(key):
                if not isinstance(widget, tk.Entry):
                    continue
                item = self._windows.get(str(widget))
                if item is None:
                    continue
                x, y = self.preview.coords(item)
                width = int(float(self.preview.itemcget(item, "width")))
                placed.append([item, widget, x, y, width])
        placed.sort(key=lambda row: (row[3], row[2]))
        for index, row in enumerate(placed):
            for other in placed[index + 1 :]:
                if other[3] - row[3] > 6:
                    break
                if abs(other[3] - row[3]) > 4:
                    continue
                if row[2] + row[4] > other[2] - 3:
                    row[4] = max(16, int(other[2] - row[2] - 3))
                    self.preview.itemconfigure(row[0], width=row[4])
                    row[1]._max_text_px = row[4]
                    self._paint_overflow(row[1])

    def _layout_box(self, key: str, spec: dict, editing: bool):
        widget = self._field_inputs[key]
        box = spec["box"]
        inset = self._px(0.8)
        left = self._page_ox + self._px(box["x"]) + inset
        top = self._page_oy + self._px(box["y"]) + inset
        width = max(20, self._px(box["w"]) - inset * 2)
        height = max(20, self._px(box["h"]) - inset * 2)
        pt = self._screen_pt(float(spec.get("size", 18)))
        # Keep two lines inside the empty block.
        while pt > 8:
            font = tkfont.Font(family="Arial", size=pt, weight="bold")
            if font.metrics("linespace") * 2 + 8 <= height:
                break
            pt -= 1
        if getattr(widget, "_laid_pt", None) != pt:
            widget.configure(font=("Arial", pt, "bold"))
            widget._laid_pt = pt
        self._move_window(widget, left, top, width, height, editing)

    def _move_window(self, widget: tk.Widget, x: float, y: float, w: float, h: float, editing: bool):
        item = self._windows[str(widget)]
        self.preview.coords(item, x, y)
        self.preview.itemconfigure(
            item,
            width=max(8, int(round(w))),
            height=max(8, int(round(h))),
            state="normal" if editing else "hidden",
        )

    def _draw_importance_marks(self):
        self.preview.delete("important-mark")
        if self.show_marks.get() or self._preview_scale <= 0:
            return
        radius = self._px(2.6)
        for key, marks in MARK_GROUPS.items():
            var = self.important.get(key)
            if var is None or not var.get():
                continue
            for mark in marks:
                cx, cy = CHECKS[mark]
                x = self._page_ox + self._px(cx)
                y = self._page_oy + self._px(cy)
                self.preview.create_oval(
                    x - radius, y - radius, x + radius, y + radius,
                    outline="#c47b12", width=2, tags="important-mark",
                )

    def _nearest_mark(self, x_mm: float, y_mm: float) -> str | None:
        best = None
        best_d = CHECK_HIT_MM * CHECK_HIT_MM
        for key, (cx, cy) in CHECKS.items():
            dist = (cx - x_mm) ** 2 + (cy - y_mm) ** 2
            if dist <= best_d:
                best = key
                best_d = dist
        return best

    def _toggle_mark(self, key: str):
        if key in INFEKT_MARKS:
            value = INFEKT_MARKS[key]
            self.infekt.set("" if self.infekt.get() == value else value)
        elif key in TRANSPORT_MARKS:
            value = TRANSPORT_MARKS[key]
            if self.transport.get() == value:
                self.transport.set("")
                self.ktw_mode.set("")
            else:
                self.transport.set(value)
                if value != "ktw":
                    self.ktw_mode.set("")
        elif key in KTW_MARKS:
            value = KTW_MARKS[key]
            if self.transport.get() == "ktw" and self.ktw_mode.get() == value:
                self.ktw_mode.set("")
            else:
                self.transport.set("ktw")
                self.ktw_mode.set(value)
        elif key in BEGLEIT_MARKS:
            value = BEGLEIT_MARKS[key]
            self.begleit.set("" if self.begleit.get() == value else value)
        elif key in self.bools:
            self.bools[key].set(not self.bools[key].get())
        self.schedule()

    def _point_in_stamp(self, x_mm: float, y_mm: float) -> bool:
        box = self._stamp_box()
        return box["x"] <= x_mm <= box["x"] + box["w"] and box["y"] <= y_mm <= box["y"] + box["h"]

    def _station_chosen(self, _label: str, phone: str, focus_phone: bool = False):
        self._set_value("tel", phone)
        if focus_phone:
            tel = self._field_inputs.get("tel")
            if isinstance(tel, tk.Entry):
                tel.focus_set()
                tel.selection_range(0, "end")
                tel.icursor("end")
        self.schedule()

    def _note_station(self):
        station = self._field_inputs.get("station")
        if isinstance(station, StationEntry):
            station.accept_loaded()

    def _ensure_flags(self, key: str):
        if key in self.important:
            return
        self.important[key] = tk.BooleanVar(value=key in IMPORTANT_DEFAULT)
        self.remember[key] = tk.BooleanVar(value=key in REMEMBER_DEFAULT)
        self.important[key].trace_add("write", lambda *_k, field=key: self._apply_importance(field))
        self._field_order.append(key)

    def _kept(self, key: str) -> bool:
        var = self.remember.get(key)
        return bool(var and var.get())

    def _mm_entry(self, parent, variable: tk.StringVar, label: str) -> ttk.Entry:
        ttk.Label(parent, text=label).pack(side="left", padx=(8, 2))
        entry = ttk.Entry(parent, textvariable=variable, width=6)
        entry.pack(side="left")
        return entry

    def _apply_importance(self, key: str):
        important = bool(self.important.get(key) and self.important[key].get())
        for widget in self._widgets_for(key):
            self._style_input(widget, important)
        if getattr(self, "preview", None) is not None:
            self._draw_importance_marks()

    def _apply_all_importance(self):
        for key in self.important:
            important = bool(self.important[key].get())
            for widget in self._widgets_for(key):
                self._style_input(widget, important)
        if getattr(self, "preview", None) is not None:
            self._draw_importance_marks()

    def open_field_settings(self):
        existing = getattr(self, "_settings", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_set()
            return
        win = tk.Toplevel(self)
        self._settings = win
        win.title("Felder")
        win.transient(self)
        win.geometry("560x640")
        win.minsize(420, 360)
        ttk.Label(
            win, style="Hint.TLabel", wraplength=520,
            text="Wichtig ist auf dem Formular gelb markiert. Merken bleibt beim nächsten Start erhalten.",
        ).pack(fill="x", padx=12, pady=(10, 6))
        ttk.Button(win, text="Schließen", command=win.destroy).pack(side="bottom", anchor="e", padx=12, pady=8)

        canvas = tk.Canvas(win, highlightthickness=0, bg="#f4f7f8")
        scroll = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=4)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        def wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        ttk.Label(body, text="Feld", style="Status.TLabel").grid(row=0, column=0, sticky="w", padx=8, pady=(4, 2))
        ttk.Label(body, text="Wichtig", style="Status.TLabel").grid(row=0, column=1, padx=8, pady=(4, 2))
        ttk.Label(body, text="Merken", style="Status.TLabel").grid(row=0, column=2, padx=8, pady=(4, 2))
        body.columnconfigure(0, weight=1)
        for index, key in enumerate(self._field_order, start=1):
            ttk.Label(body, text=CAPTIONS.get(key, key)).grid(row=index, column=0, sticky="w", padx=8, pady=2)
            ttk.Checkbutton(body, variable=self.important[key]).grid(row=index, column=1, padx=8, pady=2)
            ttk.Checkbutton(body, variable=self.remember[key]).grid(row=index, column=2, padx=8, pady=2)

    def _build_printer_bar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text="Drucker").grid(row=0, column=0, sticky="w")
        self.printer_combo = ttk.Combobox(bar, textvariable=self.printer, width=42, state="readonly")
        self.printer_combo.grid(row=0, column=1, sticky="we", padx=6)
        self.printer_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_paper_info())
        ttk.Button(bar, text="Aktualisieren", command=self._refresh_printers).grid(row=0, column=2, padx=4)

        ttk.Label(bar, text="Kopien").grid(row=0, column=3, padx=(10, 2))
        ttk.Spinbox(bar, from_=1, to=20, textvariable=self.copies, width=4).grid(row=0, column=4)

        ttk.Label(bar, text="Korrektur mm").grid(row=0, column=5, padx=(12, 2))
        ttk.Entry(bar, textvariable=self.shift_x, width=6).grid(row=0, column=6)
        ttk.Label(bar, text="X").grid(row=0, column=7, padx=(2, 6))
        ttk.Entry(bar, textvariable=self.shift_y, width=6).grid(row=0, column=8)
        ttk.Label(bar, text="Y  (+ rechts / + unten)").grid(row=0, column=9, padx=(2, 8))

        self.print_button = tk.Button(
            bar, text="Drucken", command=self.print_form,
            bg="#1a6f8a", fg="white", activebackground="#14586e", activeforeground="white",
            font=("Segoe UI", 10, "bold"), padx=14, pady=4, relief="flat",
        )
        self.print_button.grid(row=0, column=10, padx=(8, 4))
        ttk.Button(bar, text="Passkreuze drucken", command=self.print_marks).grid(row=0, column=11)

        ttk.Label(bar, textvariable=self.paper_info, style="Hint.TLabel").grid(
            row=1, column=0, columnspan=12, sticky="w", pady=(4, 0)
        )
        bar.columnconfigure(1, weight=1)

    def schedule(self):
        if not self._ready:
            return
        if self._after_id is not None:
            self.after_cancel(self._after_id)
        self._after_id = self.after(90, self._refresh_soon)

    def _refresh_soon(self):
        self._after_id = None
        self.refresh_preview()
        self._refresh_title()

    def _stamp_box(self) -> dict:
        return {
            "x": _parse_mm(self.stamp_x.get(), STAMP_DEFAULT["x"]),
            "y": _parse_mm(self.stamp_y.get(), STAMP_DEFAULT["y"]),
            "w": max(1.0, _parse_mm(self.stamp_w.get(), STAMP_DEFAULT["w"])),
            "h": max(1.0, _parse_mm(self.stamp_h.get(), STAMP_DEFAULT["h"])),
        }

    def _get_value(self, key: str) -> str:
        spec = TEXT_FIELDS.get(key) or {}
        widget = self._field_inputs.get(key)
        if spec.get("block") and isinstance(widget, tk.Text):
            return " ".join(widget.get("1.0", "end-1c").split())
        lines = self._lines.get(key)
        if lines:
            parts = [entry.get().replace("\n", " ") for entry in lines]
            while parts and not parts[-1].strip():
                parts.pop()
            if not any(part.strip() for part in parts):
                return ""
            return "\n".join(part.strip() for part in parts)
        widget = self._field_inputs.get(key)
        if isinstance(widget, tk.Text):
            return widget.get("1.0", "end-1c")
        if isinstance(widget, tk.Entry):
            return widget.get()
        return ""

    def _set_value(self, key: str, value: str):
        value = "" if value is None else str(value)
        spec = TEXT_FIELDS.get(key) or {}
        widget = self._field_inputs.get(key)
        if spec.get("block") and isinstance(widget, tk.Text):
            flat = " ".join(value.split())
            widget.delete("1.0", "end")
            if flat:
                widget.insert("1.0", flat)
            widget.tag_add("flow", "1.0", "end")
            widget._bold_hold = None
            return
        lines = self._lines.get(key)
        if lines:
            self._fill_lines(key, lines, value)
            return
        widget = self._field_inputs.get(key)
        if isinstance(widget, tk.Text):
            widget.delete("1.0", "end")
            if value:
                widget.insert("1.0", value)
            widget.tag_add("center", "1.0", "end")
            return
        if isinstance(widget, tk.Entry):
            widget.delete(0, "end")
            if value:
                widget.insert(0, value)
            self._paint_overflow(widget)

    def _fill_lines(self, key: str, entries: list[tk.Entry], value: str):
        raw = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            parts: list[str] = []
        elif "\n" in raw:
            parts = raw.split("\n")
        else:
            parts = self._wrap_field(key, raw)
        if len(parts) > len(entries):
            parts = parts[: len(entries) - 1] + [" ".join(parts[len(entries) - 1 :])]
        for index, entry in enumerate(entries):
            entry.delete(0, "end")
            if index < len(parts) and parts[index]:
                entry.insert(0, parts[index])
            self._paint_overflow(entry)

    def _wrap_field(self, key: str, text: str) -> list[str]:
        spec = TEXT_FIELDS[key]
        line_specs = spec["lines"]
        pt = float(spec.get("size") or line_specs[0]["size"]) * self._ink_factor()
        dpi = 300.0
        widths = [max(1, overlay.mm_to_px(line["w"], dpi)) for line in line_specs]
        if self._wrap_draw is None:
            image = Image.new("RGB", (8, 8))
            self._wrap_draw = ImageDraw.Draw(image)
        return list(overlay._wrap(self._wrap_draw, text, widths, pt, dpi))

    def _values_and_checks(self) -> tuple[dict[str, str], dict[str, bool]]:
        values = {key: self._get_value(key) for key in TEXT_FIELDS}
        checks = {key: var.get() for key, var in self.bools.items()}
        infekt = self.infekt.get()
        checks["infekt_ja"] = infekt == "ja"
        checks["infekt_nein"] = infekt == "nein"
        checks["infekt_nicht"] = infekt == "nicht"
        transport = self.transport.get()
        checks["gehend"] = transport == "gehend"
        checks["fahrdienst"] = transport == "fahrdienst"
        checks["ktw"] = transport == "ktw"
        ktw = self.ktw_mode.get()
        checks["liegend"] = transport == "ktw" and ktw == "liegend"
        checks["sitzend"] = transport == "ktw" and ktw == "sitzend"
        begleit = self.begleit.get()
        checks["begleit_anz_mark"] = begleit == "anz"
        checks["begleit_nein"] = begleit == "nein"
        checks["begleit_station"] = begleit == "station"
        return values, checks

    def _current_stamp(self, dpi: float):
        path = self.stamp_path.get().strip()
        box = self._stamp_box()
        mode = self.stamp_mode.get()
        if not path:
            return None
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return None
        key = (path, mtime, round(box["w"], 2), round(box["h"], 2), mode, round(dpi, 2))
        if key != self._stamp_key:
            try:
                self._stamp_image = overlay.load_stamp(path, box["w"], box["h"], dpi, dpi, mode)
            except Exception as exc:
                self._stamp_image = None
                self.cursor_info.set(f"Etikett konnte nicht gelesen werden: {exc}")
            self._stamp_key = key
        return self._stamp_image

    def refresh_preview(self):
        self._after_id = None
        if not self._ready:
            return
        try:
            if self._form_page is None:
                self._form_page = overlay.render_form_page(PREVIEW_DPI, PREVIEW_DPI)
            values, checks = self._values_and_checks()
            box = self._stamp_box()
            align = "bottomright"
            # While the boxes are on the page they are the text. Passkreuze hides
            # them and draws the answers into the picture, same as the print.
            image = overlay.compose_preview(
                values if self.show_marks.get() else {},
                checks,
                PREVIEW_DPI,
                self._current_stamp(PREVIEW_DPI),
                box,
                self.show_marks.get(),
                form_page=self._form_page,
                stamp_align=align,
                ink_scale=self._ink_factor(),
                bold_marks=(
                    {"fragestellung": self._question_bold_ranges()}
                    if self.show_marks.get() else None
                ),
            )
        except Exception as exc:
            self.cursor_info.set(f"Vorschau: {exc}")
            return

        margin = 28
        view_w = self.preview.winfo_width()
        view_h = self.preview.winfo_height()
        if view_w < 80:
            view_w = 900
        if view_h < 80:
            view_h = 700
        fit = min(
            (view_w - margin * 2) / image.width,
            (view_h - margin * 2) / image.height,
        )
        scale = max(0.2, fit * (self._zoom / 100.0))
        display_w = max(1, int(round(image.width * scale)))
        display_h = max(1, int(round(image.height * scale)))
        display = image.resize((display_w, display_h), Image.Resampling.BILINEAR).convert("RGB")
        self._display_image = display
        self._preview_scale = scale
        self._page_ox = max(margin, (view_w - display_w) / 2)
        self._page_oy = max(margin, (view_h - display_h) / 2)
        self._photo = ImageTk.PhotoImage(display)
        if self._image_id is None:
            self._image_id = self.preview.create_image(
                self._page_ox, self._page_oy, image=self._photo, anchor="nw", tags="page",
            )
        else:
            self.preview.itemconfig(self._image_id, image=self._photo)
            self.preview.coords(self._image_id, self._page_ox, self._page_oy)
        self.preview.tag_lower(self._image_id)
        self.preview.configure(scrollregion=(
            0, 0,
            max(view_w, display_w + margin * 2),
            max(view_h, display_h + margin * 2),
        ))
        self._layout_fields()

    def _event_mm(self, event) -> tuple[float, float] | None:
        if self._preview_scale <= 0:
            return None
        x = self.preview.canvasx(event.x) - self._page_ox
        y = self.preview.canvasy(event.y) - self._page_oy
        if x < 0 or y < 0:
            return None
        return x / self._preview_scale / PREVIEW_DPI * 25.4, y / self._preview_scale / PREVIEW_DPI * 25.4

    def _on_preview_motion(self, event):
        point = self._event_mm(event)
        if point is None:
            return
        mark = self._nearest_mark(*point)
        if mark and self._hover_id is not None:
            cx, cy = CHECKS[mark]
            radius = self._px(CHECK_HIT_MM)
            x = self._page_ox + self._px(cx)
            y = self._page_oy + self._px(cy)
            self.preview.coords(self._hover_id, x - radius, y - radius, x + radius, y + radius)
            self.preview.itemconfigure(self._hover_id, state="normal")
            self.preview.tag_raise(self._hover_id)
            self.preview.tag_raise("field")
            self._raise_flow_labels()
            self.preview.configure(cursor="hand2")
        else:
            if self._hover_id is not None:
                self.preview.itemconfigure(self._hover_id, state="hidden")
            self.preview.configure(cursor="")
        self.cursor_info.set(
            f"{point[0]:.1f} mm  ·  {point[1]:.1f} mm    Kreis anklicken · Umschalt+Klick Etikett-Ecke"
        )

    def _on_preview_click(self, event):
        point = self._event_mm(event)
        if point is None:
            return
        if event.state & 0x0001:
            self.stamp_x.set(f"{point[0]:.1f}")
            self.stamp_y.set(f"{point[1]:.1f}")
            return
        mark = self._nearest_mark(*point)
        if mark:
            self._toggle_mark(mark)

    def _on_preview_double(self, event):
        if event.state & 0x0001:
            return
        point = self._event_mm(event)
        if point and self._point_in_stamp(*point):
            self.browse_stamp()

    def _refresh_printers(self):
        try:
            printers = gdi_print.list_printers()
        except Exception as exc:
            self.paper_info.set(f"Druckerliste nicht verfügbar: {exc}")
            return
        self.printer_combo["values"] = printers
        current = self.printer.get()
        if current not in printers:
            default = gdi_print.default_printer()
            self.printer.set(default if default in printers else (printers[0] if printers else ""))
        self._update_paper_info()

    def _update_paper_info(self):
        name = self.printer.get()
        if not name:
            self.paper_info.set("Kein Drucker gewählt")
            return
        try:
            metrics = gdi_print.printer_metrics(name)
        except Exception as exc:
            self.paper_info.set(f"Papiermaß nicht lesbar: {exc}")
            return
        width, height = metrics["paper_mm"]
        left, top, right, bottom = metrics["margin_mm"]
        kind = "A4" if abs(width - 210) < 8 and abs(height - 297) < 8 else "kein A4"
        self.paper_info.set(
            f"{name}: {width:.0f} × {height:.0f} mm ({kind}). "
            f"Nicht bedruckbar links {left:.1f}, oben {top:.1f}, rechts {right:.1f}, unten {bottom:.1f} mm. "
            f"Korrektur verschiebt nur den Ausdruck, nicht die Vorschau."
        )

    def _keep_etiketten(self):
        try:
            import_konsil_etiketten()
            if ETIKETT_DIR.is_dir():
                rows = _purge_etiketten()
                _refresh_etikett_names(rows)
                _write_etikett_index(rows)
        except OSError:
            pass

    def _remember_stamp(self, path_text: str, *, warn: bool, label_hint: str = "") -> str:
        """Copy a chosen label into the three-month archive and return the path to use."""
        text = path_text.strip()
        if not text:
            return ""
        source = Path(text)
        if not source.is_file():
            return text
        kept = remember_etikett(source, label_hint=label_hint)
        if kept is None:
            if warn:
                messagebox.showwarning(
                    "Etikett",
                    "Das Etikett konnte nicht für die nächsten 3 Monate gespeichert werden.\n"
                    "Für dieses Konsil wird weiter die gewählte Datei verwendet.",
                )
            return text
        return str(kept)

    def browse_stamp(self):
        path = filedialog.askopenfilename(
            title="Etikett-PDF",
            filetypes=[("PDF", "*.pdf"), ("Alle Dateien", "*.*")],
            initialdir=str(ROOT),
        )
        if path:
            self.stamp_path.set(self._remember_stamp(path, warn=True))

    def open_saved_etikett(self):
        try:
            rows = _purge_etiketten()
            _refresh_etikett_names(rows)
            _write_etikett_index(rows)
        except OSError as exc:
            messagebox.showerror("Etiketten", str(exc))
            return
        win = tk.Toplevel(self)
        win.title("Gespeicherte Etiketten")
        win.transient(self)
        win.grab_set()
        win.geometry("860x620")
        win.minsize(640, 400)
        ttk.Label(win, text="Etiketten der letzten 3 Monate").pack(anchor="w", padx=12, pady=(12, 0))
        ttk.Label(
            win,
            style="Hint.TLabel",
            text="Name und Vorschau, damit dieselbe Patientenetikette wieder gewählt werden kann. Doppelklick übernimmt sie.",
        ).pack(anchor="w", padx=12, pady=(2, 6))

        search_var = tk.StringVar()
        search = ttk.Entry(win, textvariable=search_var)
        search.pack(fill="x", padx=12, pady=(0, 6))

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12)
        canvas = tk.Canvas(frame, highlightthickness=0, bg="white", bd=0)
        scroll = ttk.Scrollbar(frame, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg="white")
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def fit_inner(event=None):
            canvas.configure(scrollregion=canvas.bbox("all") or (0, 0, 0, 0))
            if event is None or event.widget is canvas:
                width = canvas.winfo_width()
                if width > 1:
                    canvas.itemconfigure(window_id, width=width)

        inner.bind("<Configure>", fit_inner)
        canvas.bind("<Configure>", fit_inner)

        def on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        shown: list[dict] = []
        selected = {"index": 0}

        photos: list[ImageTk.PhotoImage] = []

        def paint_selection():
            for index, entry in enumerate(shown):
                bg = "#d6eef4" if index == selected["index"] else "white"
                entry["row"].configure(bg=bg)
                entry["text"].configure(bg=bg)
                entry["name"].configure(bg=bg)
                entry["when"].configure(bg=bg)
                if entry["picture"] is not None:
                    entry["picture"].configure(bg=bg)

        def select(index: int):
            if not shown:
                return
            selected["index"] = max(0, min(index, len(shown) - 1))
            paint_selection()
            canvas.focus_set()

        def filtered() -> list[dict]:
            needle = " ".join(search_var.get().casefold().split())
            if not needle:
                return list(rows)
            picked = []
            for row in rows:
                hay = " ".join(
                    str(row.get(key) or "") for key in ("label", "source", "used_at")
                ).casefold()
                if needle in hay:
                    picked.append(row)
            return picked

        def build_rows(prefer: int = 0):
            for child in inner.winfo_children():
                child.destroy()
            shown.clear()
            matches = filtered()
            if not matches:
                tk.Label(
                    inner,
                    text="Keine Etiketten in den letzten 3 Monaten." if not rows else "Nichts passt zur Suche.",
                    bg="white",
                    fg="#5c6b73",
                    font=("Segoe UI", 11),
                ).pack(anchor="w", padx=8, pady=8)
                canvas.after_idle(fit_inner)
                return
            for index, row in enumerate(matches):
                used = _parse_stamp_time(str(row.get("used_at") or ""))
                when = used.strftime("%d.%m.%Y %H:%M") if used else ""
                label = str(row.get("label") or row.get("source") or "Etikett")
                line = tk.Frame(inner, bg="white")
                line.pack(fill="x")
                picture = None
                file_name = str(row.get("file") or "")
                preview = _etikett_preview(ETIKETT_DIR / file_name) if file_name else None
                if preview is not None:
                    photo = ImageTk.PhotoImage(preview)
                    photos.append(photo)
                    picture = tk.Label(line, image=photo, bg="white", bd=1, relief="solid")
                    picture.pack(side="left", padx=(8, 8), pady=6)
                text = tk.Frame(line, bg="white")
                text.pack(side="left", fill="both", expand=True, pady=6)
                name = tk.Label(
                    text,
                    text=label,
                    anchor="w",
                    bg="white",
                    fg="#142028",
                    font=("Segoe UI", 12, "bold"),
                    justify="left",
                )
                name.pack(anchor="w", fill="x", padx=(0, 8))
                stamp = tk.Label(
                    text,
                    text=when,
                    anchor="w",
                    bg="white",
                    fg="#5c6b73",
                    font=("Segoe UI", 10),
                )
                stamp.pack(anchor="w", fill="x", padx=(0, 8), pady=(2, 0))
                entry = {
                    "row": line,
                    "text": text,
                    "name": name,
                    "when": stamp,
                    "picture": picture,
                    "data": row,
                }
                shown.append(entry)

                def bind_row(widget, item=index):
                    widget.bind("<Button-1>", lambda _e, i=item: select(i))
                    widget.bind("<Double-Button-1>", lambda _e, i=item: do_use_index(i))
                    widget.bind("<MouseWheel>", on_wheel)

                bind_row(line)
                bind_row(text)
                bind_row(name)
                bind_row(stamp)
                if picture is not None:
                    bind_row(picture)
            select(prefer)
            canvas.after_idle(fit_inner)

        canvas.bind("<MouseWheel>", on_wheel)
        search.bind("<Return>", lambda _e: do_use())

        def do_use_index(index: int):
            if index < 0 or index >= len(shown):
                return
            row = shown[index]["data"]
            name = str(row.get("file") or "")
            path = ETIKETT_DIR / name
            if not path.is_file():
                messagebox.showerror("Etikett", "Diese Etikette ist nicht mehr da.", parent=win)
                return
            kept = remember_etikett(path)
            win.destroy()
            self.stamp_path.set(str(kept or path))

        def do_use(_event=None):
            if shown:
                do_use_index(selected["index"])

        def on_search(*_args):
            build_rows(0)

        search_var.trace_add("write", on_search)

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=12, pady=12)
        ttk.Button(buttons, text="Übernehmen", command=do_use).pack(side="left")
        ttk.Button(buttons, text="Schließen", command=win.destroy).pack(side="right")
        canvas.bind("<Return>", do_use)
        canvas.bind("<Up>", lambda _e: select(selected["index"] - 1))
        canvas.bind("<Down>", lambda _e: select(selected["index"] + 1))
        build_rows(0)
        search.focus_set()
        win.wait_window()

    def fill_example(self):
        sample = {
            "fachrichtung": "Radiologie",
            "ihr_zeichen": "A-1044",
            "unser_zeichen": "K-18",
            "sachbearbeiter": "Berger",
            "brief_datum": _today(),
            "geb_am": "14.03.1978",
            "station": "P2",
            "beh_arzt": "Dr. med. Anna Beispiel",
            "tel": "503-2140",
            "infekt_detail": "HIV",
            "risiko_etc": "Alkohol",
            "diagnose": "Mittelgradige depressive Episode",
            "oa_name": "Dr. Keller",
            "kasse": "AOK Bayern",
            "hilfsmittel_text": "Brille",
            "privat_text": "",
            "abrechnung_notiz": "",
            "termin": _today(),
            "uhrzeit": "10:30",
            "begl_person": "Tochter",
            "verw_datum": _today(),
            "verw_name": "Sommer",
            "begleit_anz": "1",
        }
        long = {
            "fragestellung": (
                "Bitte um konsiliarische Einschätzung der aktuellen depressiven Symptomatik und medikamentösen Optionen. "
                "CT Schädel 2024 ohne pathologischen Befund. Vorbestehende Angststörung."
            ),
        }
        for key, value in sample.items():
            self._set_value(key, value)
        for key, value in long.items():
            self._set_value(key, value)
        self.infekt.set("ja")
        self.transport.set("ktw")
        self.ktw_mode.set("sitzend")
        self.begleit.set("anz")
        for key in ("nuechtern", "suizidal", "versichert", "goae"):
            if key in self.bools:
                self.bools[key].set(True)
        self._note_station()
        self.schedule()

    def clear_fields(self):
        for key in TEXT_FIELDS:
            if self._kept(key):
                continue
            self._set_value(key, "")
        if not self._kept("risiken"):
            for key in RISK_KEYS:
                if key in self.bools:
                    self.bools[key].set(False)
        if not self._kept("abrechnung"):
            for key in BILL_KEYS:
                if key in self.bools:
                    self.bools[key].set(False)
        if not self._kept("infekt"):
            self.infekt.set("")
        if not self._kept("transport"):
            self.transport.set("")
            self.ktw_mode.set("")
            self.begleit.set("")
        if not self._kept("stamp_path"):
            self.stamp_path.set("")
        if not self._kept("fragestellung"):
            self._question_bold = []
        self._note_station()
        self.schedule()

    def _payload(self) -> dict:
        values, checks = self._values_and_checks()
        return {
            "printer": self.printer.get(),
            "copies": self.copies.get(),
            "shift_x": self.shift_x.get(),
            "shift_y": self.shift_y.get(),
            "stamp_path": self.stamp_path.get(),
            "stamp_mode": self.stamp_mode.get(),
            "stamp_box": self._stamp_box(),
            "values": values,
            "checks": checks,
            "infekt": self.infekt.get(),
            "transport": self.transport.get(),
            "ktw_mode": self.ktw_mode.get(),
            "begleit": self.begleit.get(),
            "bools": {key: var.get() for key, var in self.bools.items()},
            "important": {key: var.get() for key, var in self.important.items()},
            "remember": {key: var.get() for key, var in self.remember.items()},
            "zoom": self._zoom,
            "ink": self._ink,
            "fragestellung_bold": self._question_bold_ranges(),
        }

    def _apply_payload(self, data: dict, only_remembered: bool = False):
        self._ready = False
        try:
            for key in ("printer", "copies", "shift_x", "shift_y", "stamp_mode"):
                if key in data and data[key] is not None:
                    getattr(self, key).set(str(data[key]))
            if "zoom" in data:
                self._zoom = max(50, min(250, int(float(data["zoom"]))))
                self.zoom_pct.set(f"{self._zoom} %")
            if "ink" in data:
                self._ink = max(100, min(200, int(float(data["ink"]))))
                self.ink_pct.set(f"{self._ink} %")
            box = data.get("stamp_box") or {}
            for key, var in (("x", self.stamp_x), ("y", self.stamp_y), ("w", self.stamp_w), ("h", self.stamp_h)):
                if key in box:
                    var.set(str(box[key]))
            for name in ("important", "remember"):
                saved = data.get(name)
                if isinstance(saved, dict):
                    target = self.important if name == "important" else self.remember
                    for key, value in saved.items():
                        if key in target:
                            target[key].set(bool(value))

            def keep(key: str) -> bool:
                return self._kept(key) if only_remembered else True

            if "stamp_path" in data and keep("stamp_path"):
                self.stamp_path.set(str(data["stamp_path"] or ""))
            values = dict(data.get("values") or {})
            earlier = str(values.get("vorbefunde") or "").strip()
            if earlier:
                question = str(values.get("fragestellung") or "").strip()
                values["fragestellung"] = (question + " " + earlier).strip() if question else earlier
            for key in TEXT_FIELDS:
                if key in values and keep(key):
                    self._set_value(key, str(values[key]))
            if keep("fragestellung"):
                self._hold_question_bold(data.get("fragestellung_bold"))
            else:
                self._question_bold = []
            bools = data.get("bools") or {}
            for key, var in self.bools.items():
                if key not in bools:
                    continue
                if key in RISK_KEYS and not keep("risiken"):
                    continue
                if key in BILL_KEYS and not keep("abrechnung"):
                    continue
                var.set(bool(bools[key]))
            if keep("infekt") and "infekt" in data:
                self.infekt.set(str(data["infekt"]))
            if keep("transport"):
                for key in ("transport", "ktw_mode", "begleit"):
                    if key in data:
                        getattr(self, key).set(str(data[key]))
            self._apply_all_importance()
            self._note_station()
        finally:
            self._ready = True
        self.schedule()

    def _form_snapshot(self) -> str:
        return json.dumps(self._payload(), ensure_ascii=False, sort_keys=True)

    def _is_dirty(self) -> bool:
        return self._form_snapshot() != self._snapshot

    def _refresh_title(self):
        name = self._konsil_name or "Neues Konsil"
        mark = " *" if self._is_dirty() else ""
        self.title(f"Konsil — {name}{mark}")

    def _suggest_name(self) -> str:
        values, _checks = self._values_and_checks()
        parts = [
            values.get("fachrichtung", "").replace("\n", " ").strip(),
            values.get("geb_am", "").strip(),
            values.get("station", "").strip(),
        ]
        parts = [part for part in parts if part]
        if parts:
            return " ".join(parts)[:60]
        return date.today().strftime("Konsil %d.%m.%Y")

    def _confirm_discard(self) -> bool:
        if not self._is_dirty():
            return True
        answer = messagebox.askyesnocancel("Konsil", "Änderungen an diesem Konsil speichern?")
        if answer is None:
            return False
        if answer:
            return self.save_konsil()
        return True

    def new_konsil(self):
        if not self._confirm_discard():
            return
        self._konsil_file = None
        self._konsil_name = ""
        self.clear_fields()
        self._snapshot = self._form_snapshot()
        self._refresh_title()

    def _store_etikett(self, json_path: Path, label_hint: str = "") -> str | None:
        """Copy the label next to the Konsil. None means the save stops."""
        dest = _etikett_file(json_path)
        source_text = self.stamp_path.get().strip()
        if not source_text:
            if dest.is_file():
                try:
                    dest.unlink()
                except OSError as exc:
                    messagebox.showerror("Speichern", str(exc))
                    return None
            return ""
        source = Path(source_text)
        self._remember_stamp(source_text, warn=True, label_hint=label_hint)
        if not source.is_file():
            messagebox.showerror(
                "Speichern",
                "Das Etikett wurde nicht gefunden und kann nicht mit dem Konsil gespeichert werden:\n"
                + source_text,
            )
            return None
        try:
            same = False
            if dest.is_file():
                try:
                    same = source.resolve() == dest.resolve()
                except OSError:
                    same = False
            if not same:
                shutil.copyfile(source, dest)
        except OSError as exc:
            messagebox.showerror("Speichern", f"Etikett konnte nicht gespeichert werden:\n{exc}")
            return None
        self.stamp_path.set(str(dest))
        return dest.name

    def save_konsil(self, save_as: bool = False) -> bool:
        try:
            KONSIL_DIR.mkdir(exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Speichern", str(exc))
            return False
        if self._konsil_file is not None and not save_as:
            path = self._konsil_file
            name = self._konsil_name or path.stem
        else:
            name = simpledialog.askstring(
                "Konsil speichern",
                "Name für dieses Konsil:",
                initialvalue=self._konsil_name or self._suggest_name(),
                parent=self,
            )
            if name is None:
                return False
            name = " ".join(name.split())
            if not name:
                return False
            path = KONSIL_DIR / f"{_safe_stem(name)}.json"
            if self._konsil_file is None or path.resolve() != self._konsil_file.resolve():
                if path.exists() and not messagebox.askyesno(
                    "Konsil speichern",
                    f"„{name}“ gibt es schon. Ersetzen?",
                ):
                    return False
        stored = self._store_etikett(path, label_hint=name)
        if stored is None:
            return False
        payload = self._payload()
        payload["name"] = name
        payload["stamp_file"] = stored
        payload["saved_at"] = datetime.now().isoformat(timespec="minutes")
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Speichern", str(exc))
            return False
        self._konsil_file = path
        self._konsil_name = name
        self._snapshot = self._form_snapshot()
        self._refresh_title()
        return True

    def open_dialog(self):
        try:
            KONSIL_DIR.mkdir(exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Öffnen", str(exc))
            return
        files = sorted(KONSIL_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        win = tk.Toplevel(self)
        win.title("Konsil öffnen")
        win.transient(self)
        win.grab_set()
        win.geometry("560x460")
        win.minsize(440, 340)
        ttk.Label(win, text="Gespeicherte Konsile").pack(anchor="w", padx=12, pady=(12, 0))
        ttk.Label(
            win,
            style="Hint.TLabel",
            text="Ankreuzen, was in einen Druckauftrag soll. Doppelklick öffnet ein Konsil.",
        ).pack(anchor="w", padx=12, pady=(2, 6))

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12)
        canvas = tk.Canvas(frame, highlightthickness=0, bg="white", bd=0)
        scroll = ttk.Scrollbar(frame, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg="white")
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def fit_inner(event=None):
            canvas.configure(scrollregion=canvas.bbox("all") or (0, 0, 0, 0))
            if event is None or event.widget is canvas:
                width = canvas.winfo_width()
                if width > 1:
                    canvas.itemconfigure(window_id, width=width)

        inner.bind("<Configure>", fit_inner)
        canvas.bind("<Configure>", fit_inner)

        def on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        entries: list[dict] = []
        selected = {"index": 0}

        def paint_selection():
            for index, entry in enumerate(entries):
                bg = "#d6eef4" if index == selected["index"] else "white"
                entry["row"].configure(bg=bg)
                entry["name"].configure(bg=bg)

        def select(index: int):
            if not entries:
                return
            selected["index"] = max(0, min(index, len(entries) - 1))
            paint_selection()
            canvas.focus_set()

        def build_rows(prefer: int = 0):
            previous = {entry["path"]: entry["var"].get() for entry in entries}
            for child in inner.winfo_children():
                child.destroy()
            entries.clear()
            empty_label = None
            if not files:
                tk.Label(
                    inner,
                    text="Noch nichts gespeichert.",
                    bg="white",
                    fg="#5c6b73",
                    font=("Segoe UI", 11),
                ).pack(anchor="w", padx=8, pady=8)
                refresh_caption()
                return
            for index, path in enumerate(files):
                label = path.stem
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("name"):
                        label = str(data["name"])
                except (OSError, json.JSONDecodeError):
                    data = None
                when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
                row = tk.Frame(inner, bg="white")
                row.pack(fill="x")
                var = tk.BooleanVar(value=previous.get(path, False))
                var.trace_add("write", lambda *_: refresh_caption())
                box = ttk.Checkbutton(row, variable=var, command=lambda i=index: select(i))
                box.pack(side="left", padx=(6, 2), pady=2)
                name = tk.Label(
                    row,
                    text=f"{label}    {when}",
                    anchor="w",
                    bg="white",
                    font=("Segoe UI", 11),
                )
                name.pack(side="left", fill="x", expand=True, padx=(2, 8), pady=3)
                entry = {"path": path, "var": var, "row": row, "name": name, "title": label, "data": data}
                entries.append(entry)

                def bind_row(widget, item=index):
                    widget.bind("<Button-1>", lambda _e, i=item: select(i))
                    widget.bind("<Double-Button-1>", lambda _e, i=item: do_open_index(i))
                    widget.bind("<MouseWheel>", on_wheel)

                bind_row(row)
                bind_row(name)
                box.bind("<MouseWheel>", on_wheel)
            select(prefer)
            refresh_caption()
            canvas.after_idle(fit_inner)

        canvas.bind("<MouseWheel>", on_wheel)

        three = tk.BooleanVar(value=True)
        options = ttk.Frame(win)
        options.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Checkbutton(options, text="3 von jedem", variable=three).pack(anchor="w")
        batch_hint = ttk.Label(options, style="Hint.TLabel", wraplength=500)
        batch_hint.pack(anchor="w", pady=(2, 0))

        def refresh_caption():
            picked = sum(1 for entry in entries if entry["var"].get())
            each = 3 if three.get() else 1
            if picked:
                print_btn.configure(text=f"Auswahl drucken ({picked * each} Seiten)")
            else:
                print_btn.configure(text="Auswahl drucken")
            if three.get():
                batch_hint.configure(
                    text="Jedes angehakte Konsil dreimal hintereinander, alles in einem Auftrag. Im Druckdialog die Anzahl auf 1 lassen."
                )
            else:
                batch_hint.configure(
                    text="Jedes angehakte Konsil einmal, alles in einem Auftrag. Im Druckdialog die Anzahl auf 1 lassen."
                )

        three.trace_add("write", lambda *_: refresh_caption())

        def do_open_index(index: int):
            if index < 0 or index >= len(entries):
                return
            path = entries[index]["path"]
            win.destroy()
            self._open_konsil(path)

        def do_open(_event=None):
            if not entries:
                return
            do_open_index(selected["index"])

        def do_delete():
            if not entries:
                return
            index = selected["index"]
            entry = entries[index]
            if not messagebox.askyesno("Löschen", f"„{entry['title']}“ löschen?", parent=win):
                return
            etiketten = {_etikett_file(entry["path"])}
            saved = entry.get("data")
            if isinstance(saved, dict):
                stamp_name = str(saved.get("stamp_file") or "").strip()
                if stamp_name and Path(stamp_name).name == stamp_name:
                    etiketten.add(entry["path"].parent / stamp_name)
            try:
                entry["path"].unlink()
            except OSError as exc:
                messagebox.showerror("Löschen", str(exc), parent=win)
                return
            for etikett in etiketten:
                if not etikett.is_file():
                    continue
                try:
                    etikett.unlink()
                except OSError as exc:
                    messagebox.showerror("Löschen", str(exc), parent=win)
            if self._konsil_file is not None and entry["path"].resolve() == self._konsil_file.resolve():
                self._konsil_file = None
                self._snapshot = ""
                self._refresh_title()
            del files[index]
            build_rows(index)

        def do_print():
            if self._printing:
                return
            picked = [entry for entry in entries if entry["var"].get()]
            if not picked:
                messagebox.showwarning("Druck", "Bitte mindestens ein Konsil ankreuzen.", parent=win)
                return
            payloads: list[dict] = []
            for entry in picked:
                data = entry["data"]
                if not isinstance(data, dict):
                    try:
                        data = json.loads(entry["path"].read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        messagebox.showerror("Druck", str(exc), parent=win)
                        return
                if not isinstance(data, dict) or "values" not in data:
                    messagebox.showerror("Druck", f"„{entry['title']}“ ist kein Konsil.", parent=win)
                    return
                data = dict(data)
                stamp = _stored_etikett(data, entry["path"].parent)
                data["stamp_path"] = stamp
                if stamp and not Path(stamp).is_file():
                    messagebox.showerror(
                        "Druck",
                        f"Bei „{entry['title']}“ fehlt das Etikett:\n{stamp}",
                        parent=win,
                    )
                    return
                payloads.append(data)
            each = 3 if three.get() else 1
            printer_name = self.printer.get().strip()
            win.grab_release()
            try:
                choice = gdi_print.ask_print(win.winfo_id(), printer_name, 1)
            except OSError as exc:
                if win.winfo_exists():
                    win.grab_set()
                    messagebox.showerror("Druck", str(exc), parent=win)
                return
            if choice is None:
                if win.winfo_exists():
                    win.grab_set()
                return
            known = list(self.printer_combo["values"])
            if choice.printer_name not in known:
                known.append(choice.printer_name)
                self.printer_combo["values"] = known
            self.printer.set(choice.printer_name)
            self._update_paper_info()
            shift_x = _parse_mm(self.shift_x.get(), 0.0)
            shift_y = _parse_mm(self.shift_y.get(), 0.0)
            self._printing = True
            self.print_button.configure(state="disabled")
            if win.winfo_exists():
                try:
                    print_btn.configure(state="disabled")
                except tk.TclError:
                    pass
            threading.Thread(
                target=self._print_many_worker,
                args=(choice, payloads, each, shift_x, shift_y, win, print_btn),
                daemon=True,
            ).start()
            if win.winfo_exists():
                win.grab_set()

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=12, pady=12)
        ttk.Button(buttons, text="Öffnen", command=do_open).pack(side="left")
        ttk.Button(buttons, text="Löschen", command=do_delete).pack(side="left", padx=6)
        print_btn = tk.Button(
            buttons,
            text="Auswahl drucken",
            command=do_print,
            bg="#1a6f8a",
            fg="white",
            activebackground="#14586e",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            padx=12,
            pady=3,
            relief="flat",
        )
        print_btn.pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Schließen", command=win.destroy).pack(side="right")
        canvas.bind("<Return>", do_open)
        canvas.bind("<Up>", lambda _e: select(selected["index"] - 1))
        canvas.bind("<Down>", lambda _e: select(selected["index"] + 1))
        build_rows(0)
        canvas.focus_set()
        win.wait_window()

    def _open_konsil(self, path: Path):
        if not self._confirm_discard():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("Öffnen", str(exc))
            return
        if not isinstance(data, dict):
            messagebox.showerror("Öffnen", "Diese Datei ist kein Konsil.")
            return
        data = dict(data)
        data["stamp_path"] = _stored_etikett(data, path.parent)
        self._apply_payload(data)
        self._konsil_file = path
        self._konsil_name = str(data.get("name") or path.stem)
        self._snapshot = self._form_snapshot()
        self._refresh_title()

    def _config_payload(self) -> dict:
        """Only the fields marked Merken are kept for the next start."""
        data = self._payload()
        values = dict(data["values"])
        for key in list(values):
            if not self._kept(key):
                values[key] = ""
        data["values"] = values
        bools = dict(data["bools"])
        if not self._kept("risiken"):
            for key in RISK_KEYS:
                bools[key] = False
        if not self._kept("abrechnung"):
            for key in BILL_KEYS:
                bools[key] = False
        data["bools"] = bools
        if not self._kept("infekt"):
            data["infekt"] = ""
        if not self._kept("transport"):
            data["transport"] = ""
            data["ktw_mode"] = ""
            data["begleit"] = ""
        if not self._kept("stamp_path"):
            data["stamp_path"] = ""
        if not self._kept("fragestellung"):
            data["fragestellung_bold"] = []
        return data

    def _save_config(self):
        try:
            CONFIG_PATH.write_text(json.dumps(self._config_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._apply_payload(data, only_remembered=True)

    def _on_close(self):
        if not self._confirm_discard():
            return
        self._save_config()
        self.destroy()

    def _render_print_page(
        self,
        payload: dict,
        metrics: dict,
        registration: bool,
        shift_x: float,
        shift_y: float,
    ) -> Image.Image:
        dpi_x = metrics["dpi_x"]
        dpi_y = metrics["dpi_y"]
        box = payload.get("stamp_box") or self._stamp_box()
        stamp = None
        if payload.get("stamp_path") and not registration:
            stamp = overlay.load_stamp(
                payload["stamp_path"],
                box["w"],
                box["h"],
                dpi_x,
                dpi_y,
                payload.get("stamp_mode") or "native",
            )
        image = overlay.render_overlay(
            {} if registration else (payload.get("values") or {}),
            {} if registration else (payload.get("checks") or {}),
            dpi_x,
            dpi_y,
            stamp=stamp,
            stamp_box=box,
            stamp_align="bottomright",
            registration=registration,
            transparent=False,
            ink_scale=max(1.0, min(2.0, float(payload.get("ink", 100)) / 100.0)),
            bold_marks=None if registration else {"fragestellung": payload.get("fragestellung_bold") or []},
        )
        if registration:
            draw = ImageDraw.Draw(image)
            draw.text(
                (mm_to_px(18, dpi_x), mm_to_px(8, dpi_y)),
                f"Passkreuze    Korrektur X {shift_x:+.1f} mm    Y {shift_y:+.1f} mm",
                font=_font(max(12, mm_to_px(3.2, dpi_y))),
                fill=(180, 0, 0),
                anchor="ls",
            )
        page = Image.new("RGB", (metrics["phys_w"], metrics["phys_h"]), "white")
        image = image.crop((0, 0, min(image.width, page.width), min(image.height, page.height)))
        page.paste(image, (0, 0))
        return page

    def print_form(self):
        self._print(registration=False)

    def print_marks(self):
        self._print(registration=True)

    def _print(self, registration: bool):
        if self._printing:
            return
        name = self.printer.get().strip()
        try:
            copies = max(1, min(20, int(self.copies.get())))
        except ValueError:
            copies = 1
        try:
            choice = gdi_print.ask_print(self.winfo_id(), name, copies)
        except OSError as exc:
            messagebox.showerror("Druck", str(exc))
            return
        if choice is None:
            return
        known = list(self.printer_combo["values"])
        if choice.printer_name not in known:
            known.append(choice.printer_name)
            self.printer_combo["values"] = known
        self.printer.set(choice.printer_name)
        self.copies.set(str(choice.reported_copies))
        self._update_paper_info()
        if not registration:
            kept = self._remember_stamp(self.stamp_path.get(), warn=False)
            if kept:
                self.stamp_path.set(kept)
        payload = self._payload()
        self._printing = True
        self.print_button.configure(state="disabled")
        threading.Thread(
            target=self._print_worker,
            args=(choice, payload, registration),
            daemon=True,
        ).start()

    def _print_worker(self, choice: gdi_print.PrintChoice, payload: dict, registration: bool):
        hdc = None
        try:
            hdc = gdi_print.create_printer_dc(choice.printer_name, choice.devmode)
            metrics = gdi_print.metrics_from_hdc(hdc)
            shift_x = _parse_mm(payload["shift_x"], 0.0)
            shift_y = _parse_mm(payload["shift_y"], 0.0)
            page = self._render_print_page(payload, metrics, registration, shift_x, shift_y)
            gdi_print.draw_on_dc(
                hdc,
                page,
                choice.page_loops,
                shift_x,
                shift_y,
                "Konsil Passkreuze" if registration else "Konsil Vordruck",
            )
        except Exception as exc:
            self.after(0, lambda err=exc: messagebox.showerror("Druck", str(err)))
        else:
            self.after(0, self._save_config)
            sent = choice.printer_name
            count = choice.reported_copies
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Druck",
                    f"{count} Seite(n) an {sent} gesendet.",
                ),
            )
        finally:
            gdi_print.close_dc(hdc)
            self.after(0, self._print_finished)

    def _print_many_worker(
        self,
        choice: gdi_print.PrintChoice,
        payloads: list[dict],
        each: int,
        shift_x: float,
        shift_y: float,
        win: tk.Toplevel,
        print_btn: tk.Button,
    ):
        hdc = None
        session = None
        drawn = 0
        sheets = len(payloads) * max(1, each) * max(1, choice.page_loops)

        def tell(kind: str, text: str):
            def show(kind=kind, text=text):
                parent = win if win.winfo_exists() else self
                if kind == "error":
                    messagebox.showerror("Druck", text, parent=parent)
                else:
                    messagebox.showinfo("Druck", text, parent=parent)

            self.after(0, show)

        try:
            hdc = gdi_print.create_printer_dc(choice.printer_name, choice.devmode)
            session = gdi_print.PrintSession(hdc, "Konsile")
            for _loop in range(max(1, choice.page_loops)):
                for payload in payloads:
                    name = str(payload.get("name") or "Konsil")
                    try:
                        page = self._render_print_page(payload, session.metrics, False, shift_x, shift_y)
                    except Exception as exc:
                        raise OSError(f"„{name}“ konnte nicht gedruckt werden: {exc}") from exc
                    for _copy in range(max(1, each)):
                        session.draw(page, shift_x, shift_y)
                        drawn += 1
        except Exception as exc:
            text = str(exc)
            if drawn:
                text += "\nSeiten davor liegen schon im Auftrag."
            tell("error", text)
        else:
            tell("info", f"{sheets} Seiten in einem Auftrag an {choice.printer_name} gesendet.")
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            gdi_print.close_dc(hdc)

            def done():
                self._print_finished()
                if win.winfo_exists():
                    try:
                        win.grab_set()
                        print_btn.configure(state="normal")
                    except tk.TclError:
                        pass

            self.after(0, done)

    def _print_finished(self):
        self._printing = False
        self.print_button.configure(state="normal")


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = FormApp()
    app.mainloop()


if __name__ == "__main__":
    main()
