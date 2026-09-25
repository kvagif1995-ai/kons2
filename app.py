"""Fill the Konsil form on screen and print only the answers onto the paper form.

Answers are typed straight onto the scanned form. The printer receives the text,
the marks and the chosen label PDF. Put the blank paper forms in the tray.
"""

from __future__ import annotations

import json
import sys
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

import gdi_print
import overlay
from form_layout import CHECKS, STAMP_DEFAULT, TEXT_FIELDS
from overlay import mm_to_px

def _install_dir() -> Path:
    """Folder the user runs. Settings stay beside the exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _install_dir()
CONFIG_PATH = ROOT / "config.json"
PREVIEW_DPI = 120

# Filled on almost every form. Highlighted until the user turns that off.
IMPORTANT_DEFAULT = {
    "fachrichtung", "geb_am", "station", "beh_arzt", "tel",
    "fragestellung", "vorbefunde", "diagnose",
}
# Same from one form to the next. Restored on the next start.
REMEMBER_DEFAULT = {
    "fachrichtung", "station", "sachbearbeiter", "beh_arzt", "tel",
    "oa_name", "aa_name", "verw_name",
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
    "fragestellung", "vorbefunde",
    "infekt", "infekt_detail",
    "risiken", "risiko_etc",
    "transport", "begleit_anz",
    "diagnose", "oa_name", "aa_name",
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
    "fragestellung": "Fragestellung",
    "vorbefunde": "Wichtige Vorbefunde",
    "infekt": "Infektiosität",
    "infekt_detail": "Bei ja, kurzer Zusatz (z. B. HIV)",
    "risiken": "Kreuze, Risiken",
    "risiko_etc": "etc.",
    "transport": "Angaben zum Transport",
    "begleit_anz": "Anzahl Begleitpersonen",
    "diagnose": "Psychiatrische Diagnose",
    "oa_name": "Name Oberarzt",
    "aa_name": "Name Ass. Arzt",
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

        self._build()
        self._load_config()
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
        ttk.Button(row, text="Speichern…", command=self.save_dialog).pack(side="left", padx=4)
        ttk.Button(row, text="Öffnen…", command=self.open_dialog).pack(side="left", padx=4)
        ttk.Checkbutton(row, text="Passkreuze", variable=self.show_marks).pack(side="left", padx=(12, 4))
        view = ttk.Frame(row)
        view.pack(side="right")
        self._stepper(view, "Zoom", self.zoom_pct, self._bump_zoom)
        self._stepper(view, "Schrift", self.ink_pct, self._bump_ink)

        stamp = ttk.Frame(bar)
        stamp.pack(fill="x", pady=(4, 0))
        ttk.Label(stamp, text="Etikett").pack(side="left")
        ttk.Entry(stamp, textvariable=self.stamp_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(stamp, text="Wählen…", command=self.browse_stamp).pack(side="left")
        ttk.Button(stamp, text="Entfernen", command=lambda: self.stamp_path.set("")).pack(side="left", padx=(6, 8))
        ttk.Radiobutton(stamp, text="Originalgröße", variable=self.stamp_mode, value="native").pack(side="left")
        ttk.Radiobutton(stamp, text="einpassen", variable=self.stamp_mode, value="fit").pack(side="left", padx=(6, 8))
        self._mm_entry(stamp, self.stamp_x, "X")
        self._mm_entry(stamp, self.stamp_y, "Y")
        self._mm_entry(stamp, self.stamp_w, "B")
        self._mm_entry(stamp, self.stamp_h, "H")
        ttk.Label(bar, textvariable=self.cursor_info, style="Hint.TLabel").pack(fill="x", pady=(3, 0))

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
            if "lines" in spec and len(spec["lines"]) > 1:
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

    def _embed(self, widget: tk.Widget):
        item = self.preview.create_window(-2000, -2000, window=widget, anchor="nw", tags="field")
        self._windows[str(widget)] = item
        widget.bind("<FocusIn>", lambda _e, mark=item, box=widget: self._focus_field(mark, box), add="+")

    def _remember_field(self, widget: tk.Widget, newline: bool = False):
        self._tab_chain.append(widget)
        widget.bind("<MouseWheel>", self._wheel, add="+")
        widget.bind("<Tab>", self._tab_next, add="+")
        widget.bind("<Shift-Tab>", self._tab_prev, add="+")
        widget.bind("<ISO_Left_Tab>", self._tab_prev, add="+")
        if not newline:
            widget.bind("<Return>", self._tab_next, add="+")
            widget.bind("<KeyRelease>", lambda _e, box=widget: self._paint_overflow(box), add="+")

    def _focus_field(self, item: int, _widget: tk.Widget):
        self.preview.tag_raise(item)
        self._reveal_item(item)

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
            if "box" in spec:
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
        self._after_id = self.after(90, self.refresh_preview)

    def _stamp_box(self) -> dict:
        return {
            "x": _parse_mm(self.stamp_x.get(), STAMP_DEFAULT["x"]),
            "y": _parse_mm(self.stamp_y.get(), STAMP_DEFAULT["y"]),
            "w": max(1.0, _parse_mm(self.stamp_w.get(), STAMP_DEFAULT["w"])),
            "h": max(1.0, _parse_mm(self.stamp_h.get(), STAMP_DEFAULT["h"])),
        }

    def _get_value(self, key: str) -> str:
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
        display = image.resize((display_w, display_h), Image.Resampling.BILINEAR)
        self._preview_scale = scale
        self._page_ox = max(margin, (view_w - display_w) / 2)
        self._page_oy = max(margin, (view_h - display_h) / 2)
        self._photo = ImageTk.PhotoImage(display.convert("RGB"))
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

    def browse_stamp(self):
        path = filedialog.askopenfilename(
            title="Etikett-PDF",
            filetypes=[("PDF", "*.pdf"), ("Alle Dateien", "*.*")],
            initialdir=str(ROOT),
        )
        if path:
            self.stamp_path.set(path)

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
            "aa_name": "Dr. Novak",
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
            "fragestellung": "Bitte um konsiliarische Einschätzung der aktuellen depressiven Symptomatik und medikamentösen Optionen.",
            "vorbefunde": "CT Schädel 2024 ohne pathologischen Befund. Vorbestehende Angststörung.",
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
            values = data.get("values") or {}
            for key in TEXT_FIELDS:
                if key in values and keep(key):
                    self._set_value(key, str(values[key]))
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
        finally:
            self._ready = True
        self.schedule()

    def save_dialog(self):
        path = filedialog.asksaveasfilename(
            title="Formular speichern",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialdir=str(ROOT),
        )
        if not path:
            return
        Path(path).write_text(json.dumps(self._payload(), ensure_ascii=False, indent=2), encoding="utf-8")

    def open_dialog(self):
        path = filedialog.askopenfilename(
            title="Formular öffnen",
            filetypes=[("JSON", "*.json")],
            initialdir=str(ROOT),
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("Öffnen", str(exc))
            return
        self._apply_payload(data)

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
        self._save_config()
        self.destroy()

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
            dpi_x = metrics["dpi_x"]
            dpi_y = metrics["dpi_y"]
            box = payload["stamp_box"]
            align = "bottomright"
            stamp = None
            if payload["stamp_path"] and not registration:
                stamp = overlay.load_stamp(
                    payload["stamp_path"], box["w"], box["h"], dpi_x, dpi_y, payload["stamp_mode"]
                )
            image = overlay.render_overlay(
                {} if registration else payload["values"],
                {} if registration else payload["checks"],
                dpi_x,
                dpi_y,
                stamp=stamp,
                stamp_box=box,
                stamp_align=align,
                registration=registration,
                transparent=False,
                ink_scale=max(1.0, min(2.0, float(payload.get("ink", 100)) / 100.0)),
            )
            if registration:
                shift_x = _parse_mm(payload["shift_x"], 0.0)
                shift_y = _parse_mm(payload["shift_y"], 0.0)
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
            gdi_print.draw_on_dc(
                hdc,
                page,
                choice.page_loops,
                _parse_mm(payload["shift_x"], 0.0),
                _parse_mm(payload["shift_y"], 0.0),
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
