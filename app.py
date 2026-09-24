"""Fill the Konsil form on screen and print only the answers onto the paper form.

The scanned form stays on the monitor so the typing can be checked against the
real layout. The printer receives the text, the marks and the chosen label PDF.
Put the blank paper forms in the tray.
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

import gdi_print
import overlay
from form_layout import PAGE_H_MM, PAGE_W_MM, STAMP_DEFAULT
from overlay import mm_to_px

ROOT = Path(__file__).resolve().parent
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
DATE_KEYS = ("brief_datum", "verw_datum")

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
        self.geometry("1280x860")
        self.minsize(1040, 700)
        self._ready = False
        self._after_id = None
        self._photo = None
        self._preview_scale = 1.0
        self._form_page = None
        self._stamp_key = None
        self._stamp_image = None
        self._printing = False

        self.entries: dict[str, tk.StringVar] = {}
        self.texts: dict[str, tk.Text] = {}
        self.bools: dict[str, tk.BooleanVar] = {}
        self.important: dict[str, tk.BooleanVar] = {}
        self.remember: dict[str, tk.BooleanVar] = {}
        self._field_labels: dict[str, ttk.Label] = {}
        self._field_order: list[str] = []
        self._field_inputs: dict[str, tk.Entry | tk.Text] = {}
        self._group_frames: dict[str, tk.Frame] = {}
        self._blocks: list[tuple[str, ttk.Frame]] = []
        self._layout_ready = False
        self._suspend_layout = False

        self.printer = tk.StringVar()
        self.copies = tk.StringVar(value="1")
        self.shift_x = tk.StringVar(value="-9.0")
        self.shift_y = tk.StringVar(value="-1.0")
        self.stamp_path = tk.StringVar()
        self.stamp_x = tk.StringVar(value=f"{STAMP_DEFAULT['x']:.1f}")
        self.stamp_y = tk.StringVar(value=f"{STAMP_DEFAULT['y']:.1f}")
        self.stamp_w = tk.StringVar(value=f"{STAMP_DEFAULT['w']:.1f}")
        self.stamp_h = tk.StringVar(value=f"{STAMP_DEFAULT['h']:.1f}")
        self.stamp_mode = tk.StringVar(value="native")
        self.show_marks = tk.BooleanVar(value=False)
        self.paper_info = tk.StringVar(value="Kein Drucker gewählt")
        self.cursor_info = tk.StringVar(value="Umschalt+Klick in die Vorschau setzt die Etikett-Ecke")

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
                "Gedruckt werden nur die Einträge und das Etikett — das Formular selbst bleibt auf dem Papier. "
                "Drucken öffnet den normalen Druckdialog. Unter Einstellungen: Auftrag → Privater Druck, "
                "Grundlagen → Quelle Universal. Formulare erst am Gerät in die Universalzufuhr legen "
                "und den Auftrag dort mit dem Code starten."
            ),
            wraplength=1200,
        )
        hint.pack(fill="x", padx=12, pady=(8, 4))

        paned = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        form_wrap = ttk.Frame(paned)
        preview_wrap = ttk.Frame(paned)
        paned.add(form_wrap, weight=3)
        paned.add(preview_wrap, weight=4)

        self._build_form(form_wrap)
        self._build_preview(preview_wrap)
        self._build_printer_bar()

        for var in (
            self.shift_x, self.shift_y, self.stamp_x, self.stamp_y,
            self.stamp_w, self.stamp_h, self.stamp_path, self.stamp_mode,
        ):
            var.trace_add("write", lambda *_: self.schedule())
        self.show_marks.trace_add("write", lambda *_: self.schedule())

    def _build_form(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        ttk.Button(bar, text="Felder…", command=self.open_field_settings).pack(side="left", padx=(8, 4), pady=4)
        ttk.Label(bar, text="Wichtig und Merken", style="Hint.TLabel").pack(side="left")

        canvas = tk.Canvas(parent, highlightthickness=0, bg="#f4f7f8")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        def wheel(event):
            if isinstance(event.widget, tk.Text):
                return
            canvas.yview_scroll(int(-event.delta / 120), "units")

        def bind_wheel(_event):
            canvas.bind_all("<MouseWheel>", wheel)

        def unbind_wheel(_event):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", bind_wheel)
        canvas.bind("<Leave>", unbind_wheel)
        self._form_body = body
        self._field_host = body
        self._important_head = ttk.Label(body, text="Zuerst ausfüllen", style="Important.TLabel")
        self._optional_head = ttk.Label(body, text="Weitere Angaben", style="Optional.TLabel")

        self._section_kopf(body)
        self._section_patient(body)
        self._section_klinik(body)
        self._section_risiken(body)
        self._section_transport(body)
        self._section_diagnose(body)
        self._section_abrechnung(body)

        actions = ttk.Frame(body)
        self._actions = actions
        ttk.Button(actions, text="Beispiel füllen", command=self.fill_example).pack(side="left")
        ttk.Button(actions, text="Nicht gemerkte leeren", command=self.clear_fields).pack(side="left", padx=6)
        ttk.Button(actions, text="Speichern…", command=self.save_dialog).pack(side="left", padx=6)
        ttk.Button(actions, text="Öffnen…", command=self.open_dialog).pack(side="left")
        self._layout_ready = True
        self._place_blocks()

    def _section(self, parent, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.pack(fill="x", padx=8, pady=4)
        return frame

    def _ensure_flags(self, key: str):
        if key in self.important:
            return
        self.important[key] = tk.BooleanVar(value=key in IMPORTANT_DEFAULT)
        self.remember[key] = tk.BooleanVar(value=key in REMEMBER_DEFAULT)
        self.important[key].trace_add("write", lambda *_k, field=key: self._apply_importance(field))
        self._field_order.append(key)

    def _flag_line(self, parent, key: str, caption: str):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(2, 2))
        title = ttk.Label(row, text=caption, style="Optional.TLabel")
        title.pack(side="left")
        self._field_labels[key] = title
        self._ensure_flags(key)
        self._apply_importance(key)

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
            text="Wichtig steht oben und ist hervorgehoben. Merken bleibt beim nächsten Start erhalten.",
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
            label = self._field_labels.get(key)
            name = label.cget("text") if label is not None else key
            ttk.Label(body, text=name).grid(row=index, column=0, sticky="w", padx=8, pady=2)
            ttk.Checkbutton(body, variable=self.important[key]).grid(row=index, column=1, padx=8, pady=2)
            ttk.Checkbutton(body, variable=self.remember[key]).grid(row=index, column=2, padx=8, pady=2)

    def _apply_importance(self, key: str, reorder: bool = True):
        label = self._field_labels.get(key)
        widget = self._field_inputs.get(key)
        important = bool(self.important.get(key) and self.important[key].get())
        if label is not None:
            label.configure(style="Important.TLabel" if important else "Optional.TLabel")
        if widget is not None:
            widget.configure(
                bg="#fff3cc" if important else "#ffffff",
                highlightbackground="#c47b12" if important else "#c5ced4",
                highlightcolor="#1a6f8a",
            )
        group = self._group_frames.get(key)
        if group is not None:
            group.configure(bg="#fff3cc" if important else "#f4f7f8")
        if reorder and self._layout_ready and not self._suspend_layout:
            self._place_blocks()

    def _apply_all_importance(self):
        for key in self.important:
            self._apply_importance(key, reorder=False)
        if self._layout_ready and not self._suspend_layout:
            self._place_blocks()

    def _block(self, key: str) -> ttk.Frame:
        frame = ttk.Frame(self._field_host)
        self._blocks.append((key, frame))
        return frame

    def _place_blocks(self):
        """Important fields first, the rest underneath, in form order within each group."""
        if not self._blocks:
            return
        important = []
        optional = []
        for key, frame in self._blocks:
            frame.pack_forget()
            var = self.important.get(key)
            if var is not None and var.get():
                important.append(frame)
            else:
                optional.append(frame)
        self._important_head.pack_forget()
        self._optional_head.pack_forget()
        self._actions.pack_forget()
        if important:
            self._important_head.pack(fill="x", padx=8, pady=(8, 2))
            for frame in important:
                frame.pack(fill="x", padx=8, pady=2)
        if optional:
            self._optional_head.pack(fill="x", padx=8, pady=(14, 2))
            for frame in optional:
                frame.pack(fill="x", padx=8, pady=2)
        self._actions.pack(fill="x", padx=8, pady=(8, 12))

    def _highlight_host(self, parent, key: str) -> tk.Frame:
        host = tk.Frame(parent, bg="#f4f7f8", padx=6, pady=4)
        host.pack(fill="x", pady=(0, 4))
        self._group_frames[key] = host
        self._apply_importance(key)
        return host

    def _kept(self, key: str) -> bool:
        var = self.remember.get(key)
        return bool(var and var.get())

    def _entry(self, parent, key: str, label: str, width: int = 24) -> tk.Entry:
        title = ttk.Label(parent, text=label, style="Optional.TLabel")
        title.pack(anchor="w")
        self._field_labels[key] = title
        self._ensure_flags(key)
        var = tk.StringVar()
        var.trace_add("write", lambda *_: self.schedule())
        entry = tk.Entry(
            parent, textvariable=var, width=width, font=("Segoe UI", 10),
            relief="solid", bd=1, highlightthickness=1,
        )
        entry.pack(anchor="w", fill="x", pady=(0, 6))
        self.entries[key] = var
        self._field_inputs[key] = entry
        self._apply_importance(key)
        return entry

    def _mm_entry(self, parent, variable: tk.StringVar, label: str) -> ttk.Entry:
        ttk.Label(parent, text=label).pack(side="left", padx=(8, 2))
        entry = ttk.Entry(parent, textvariable=variable, width=6)
        entry.pack(side="left")
        return entry

    def _text(self, parent, key: str, label: str, height: int = 2, max_lines: int | None = None) -> tk.Text:
        title = ttk.Label(parent, text=label, style="Optional.TLabel")
        title.pack(anchor="w")
        self._field_labels[key] = title
        self._ensure_flags(key)
        widget = tk.Text(
            parent, height=height, width=48, wrap="word", font=("Segoe UI", 10),
            relief="solid", borderwidth=1, highlightthickness=1,
        )
        widget.pack(anchor="w", fill="x", pady=(0, 6))
        self._field_inputs[key] = widget
        self._apply_importance(key)
        widget.bind("<KeyRelease>", lambda _e: widget.after(10, self.schedule))
        widget.bind("<<Paste>>", lambda _e: widget.after(10, self.schedule))
        if max_lines:
            def block_third_line(event, box=widget, limit=max_lines):
                if event.keysym != "Return":
                    return None
                current = int(box.index("end-1c").split(".")[0])
                if current >= limit:
                    return "break"
                return None

            def trim_extra(_event=None, box=widget, limit=max_lines):
                end_line = int(box.index("end-1c").split(".")[0])
                if end_line <= limit:
                    return
                extra = box.get(f"{limit + 1}.0", "end-1c").strip()
                box.delete(f"{limit}.end", "end")
                if extra:
                    box.insert(f"{limit}.end", " " + extra)

            widget.bind("<Return>", block_third_line)
            widget.bind("<KeyRelease>", lambda _e: trim_extra(), add="+")
            widget.bind("<<Paste>>", lambda _e: widget.after(10, trim_extra), add="+")
        self.texts[key] = widget
        return widget

    def _checks(self, parent, keys: list[tuple[str, str]], columns: int = 2):
        grid = ttk.Frame(parent)
        grid.pack(fill="x", anchor="w")
        for index, (key, label) in enumerate(keys):
            var = tk.BooleanVar(value=False)
            self.bools[key] = var
            ttk.Checkbutton(grid, text=label, variable=var, command=self.schedule).grid(
                row=index // columns, column=index % columns, sticky="w", padx=(0, 12), pady=1
            )

    def _radios(self, parent, variable: tk.StringVar, options: list[tuple[str, str]], columns: int = 2):
        grid = ttk.Frame(parent)
        grid.pack(fill="x", anchor="w")
        for index, (value, label) in enumerate(options):
            ttk.Radiobutton(
                grid, text=label, variable=variable, value=value, command=self.schedule
            ).grid(row=index // columns, column=index % columns, sticky="w", padx=(0, 12), pady=1)
        return grid

    def _section_kopf(self, parent):
        self._text(
            self._block("fachrichtung"), "fachrichtung",
            "Fachrichtung, links oben (eine oder zwei Zeilen)",
            height=2, max_lines=2,
        )
        for key, label, width in (
            ("ihr_zeichen", "Ihr Zeichen", 18),
            ("unser_zeichen", "Unser Zeichen", 14),
            ("sachbearbeiter", "Sachbearbeiter", 16),
            ("brief_datum", "Datum oben", 12),
        ):
            self._entry(self._block(key), key, label, width)

    def _section_patient(self, parent):
        frame = self._block("stamp_path")
        file_row = ttk.Frame(frame)
        file_row.pack(fill="x")
        title = ttk.Label(file_row, text="Etikett-PDF", style="Optional.TLabel")
        title.pack(side="left")
        self._field_labels["stamp_path"] = title
        self._ensure_flags("stamp_path")
        self._apply_importance("stamp_path")
        ttk.Entry(file_row, textvariable=self.stamp_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(file_row, text="Wählen…", command=self.browse_stamp).pack(side="left")
        ttk.Button(file_row, text="Entfernen", command=lambda: self.stamp_path.set("")).pack(side="left", padx=(6, 0))

        mode = ttk.Frame(frame)
        mode.pack(fill="x", pady=4)
        ttk.Label(mode, text="Etikett").pack(side="left")
        ttk.Radiobutton(mode, text="Originalgröße", variable=self.stamp_mode, value="native").pack(side="left", padx=(8, 0))
        ttk.Radiobutton(mode, text="in den Rahmen einpassen", variable=self.stamp_mode, value="fit").pack(side="left", padx=(8, 0))
        ttk.Label(
            frame, style="Hint.TLabel", wraplength=420,
            text="Rechts über der roten Linie, nicht auf dem roten Text. Weißer Grund und dünner Rand, damit der Vordruck darunter nicht durchscheint.",
        ).pack(anchor="w", pady=(2, 4))

        box = ttk.Frame(frame)
        box.pack(fill="x", pady=(0, 4))
        ttk.Label(box, text="Rahmen mm").pack(side="left")
        self._mm_entry(box, self.stamp_x, "X")
        self._mm_entry(box, self.stamp_y, "Y")
        self._mm_entry(box, self.stamp_w, "Breite")
        self._mm_entry(box, self.stamp_h, "Höhe")

        self._entry(self._block("geb_am"), "geb_am", "geb. am")
        self._entry(self._block("station"), "station", "Station")
        self._entry(self._block("beh_arzt"), "beh_arzt", "Beh. Arzt im Bezirkskrankenhaus", 48)
        self._entry(self._block("tel"), "tel", "Tel.-Nr. (bei Rückfrage)", 24)

    def _section_klinik(self, parent):
        self._text(self._block("fragestellung"), "fragestellung", "Fragestellung")
        self._text(self._block("vorbefunde"), "vorbefunde", "Wichtige Vorbefunde")

    def _section_risiken(self, parent):
        frame = self._block("infekt")
        self._flag_line(frame, "infekt", "Infektiosität")
        host = self._highlight_host(frame, "infekt")
        ttk.Label(host, text="HIV, Hepatitis, Tbc, Lues").pack(anchor="w")
        self._radios(
            host,
            self.infekt,
            [("", "keine Angabe"), ("ja", "ja"), ("nein", "nein"), ("nicht", "nicht untersucht")],
            columns=4,
        )
        self._entry(frame, "infekt_detail", "Bei ja, kurzer Zusatz (z. B. HIV)", 20)
        frame = self._block("risiken")
        self._flag_line(frame, "risiken", "Kreuze")
        self._checks(
            self._highlight_host(frame, "risiken"),
            [
                ("nuechtern", "nüchtern"),
                ("sediert", "sediert"),
                ("unzug", "unzug. Pat."),
                ("epilepsie", "Epilepsie"),
                ("nicht_gehfaehig", "nicht gehfähig"),
                ("kommunikat", "kommunikat. gest."),
                ("suizidal", "suizidal"),
                ("fluchtgefahr", "Fluchtgefahr"),
            ],
            columns=2,
        )
        self._entry(self._block("risiko_etc"), "risiko_etc", "etc.", 48)

    def _section_transport(self, parent):
        frame = self._block("transport")
        self._flag_line(frame, "transport", "Angaben")
        host = self._highlight_host(frame, "transport")
        self._radios(
            host,
            self.transport,
            [
                ("", "keine Angabe"),
                ("gehend", "gehend"),
                ("fahrdienst", "interner Fahrdienst"),
                ("ktw", "Krankentransportwagen"),
            ],
            columns=2,
        )
        ttk.Label(host, text="Beim Krankentransportwagen").pack(anchor="w", pady=(4, 0))
        self._radios(
            host,
            self.ktw_mode,
            [("", "—"), ("liegend", "nur liegend"), ("sitzend", "sitzend transportfähig")],
            columns=3,
        )
        ttk.Label(host, text="Begleitung").pack(anchor="w", pady=(4, 0))
        self._radios(
            host,
            self.begleit,
            [
                ("", "keine Angabe"),
                ("anz", "Begleitperson notwendig"),
                ("nein", "nein"),
                ("station", "Begleitung von Station"),
            ],
            columns=2,
        )
        self._entry(self._block("begleit_anz"), "begleit_anz", "Anzahl Begleitpersonen", 8)

    def _section_diagnose(self, parent):
        self._entry(self._block("diagnose"), "diagnose", "Psychiatrische Diagnose", 48)
        self._entry(self._block("oa_name"), "oa_name", "Name Oberarzt")
        self._entry(self._block("aa_name"), "aa_name", "Name Ass. Arzt")

    def _section_abrechnung(self, parent):
        frame = self._block("abrechnung")
        self._flag_line(frame, "abrechnung", "Kreuze")
        self._checks(
            self._highlight_host(frame, "abrechnung"),
            [
                ("versichert", "Versichertenkarte"),
                ("befreit", "Keine Rezeptzuzahlungspflicht — befreit"),
                ("goae", "Bezirkskrankenhaus, GOÄ-Einfachsatz"),
                ("bema", "Bezirkskrankenhaus, BEMA-Satz"),
                ("hilfsmittel", "Kostenträger für Hilfsmittel stationär"),
                ("privat", "Privatpatient"),
            ],
            columns=1,
        )
        self._entry(self._block("kasse"), "kasse", "Kasse", 40)
        self._entry(self._block("hilfsmittel_text"), "hilfsmittel_text", "Hilfsmittel, Kostenträger", 40)
        self._entry(self._block("privat_text"), "privat_text", "Privatpatient, Zusatz", 40)
        self._entry(self._block("abrechnung_notiz"), "abrechnung_notiz", "Freie Zeile unter der Abrechnung", 48)
        for key, label, width in (
            ("termin", "Termin am", 14),
            ("uhrzeit", "Uhrzeit", 8),
            ("begl_person", "Begl.-Person", 18),
            ("verw_datum", "Lohr a. Main, den", 12),
        ):
            self._entry(self._block(key), key, label, width)
        self._entry(self._block("verw_name"), "verw_name", "Name Verwaltung", 32)

    def _build_preview(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="Vorschau", style="Status.TLabel").pack(side="left")
        ttk.Checkbutton(bar, text="Passkreuze", variable=self.show_marks).pack(side="left", padx=10)
        ttk.Label(bar, textvariable=self.cursor_info).pack(side="right")

        self.preview = tk.Canvas(parent, bg="#d5dee2", highlightthickness=0)
        yscroll = ttk.Scrollbar(parent, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=yscroll.set)
        self.preview.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.preview.bind("<Configure>", lambda _e: self.schedule())
        self.preview.bind("<Motion>", self._on_preview_motion)
        self.preview.bind("<Button-1>", self._on_preview_click)

        def wheel(event):
            self.preview.yview_scroll(int(-event.delta / 120), "units")

        self.preview.bind("<Enter>", lambda _e: self.preview.bind_all("<MouseWheel>", wheel))
        self.preview.bind("<Leave>", lambda _e: self.preview.unbind_all("<MouseWheel>"))

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

    def _values_and_checks(self) -> tuple[dict[str, str], dict[str, bool]]:
        values = {key: var.get() for key, var in self.entries.items()}
        for key, widget in self.texts.items():
            values[key] = widget.get("1.0", "end-1c")
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
            image = overlay.compose_preview(
                values,
                checks,
                PREVIEW_DPI,
                self._current_stamp(PREVIEW_DPI),
                box,
                self.show_marks.get(),
                form_page=self._form_page,
                stamp_align=align,
            )
        except Exception as exc:
            self.cursor_info.set(f"Vorschau: {exc}")
            return

        width = self.preview.winfo_width() - 4
        if width < 80:
            width = 640
        scale = width / image.width
        display = image.resize((width, max(1, int(round(image.height * scale)))), Image.Resampling.BILINEAR)
        self._preview_scale = scale
        self._photo = ImageTk.PhotoImage(display.convert("RGB"))
        self.preview.delete("all")
        self.preview.create_image(0, 0, image=self._photo, anchor="nw")
        self.preview.configure(scrollregion=(0, 0, display.width, display.height))

    def _event_mm(self, event) -> tuple[float, float] | None:
        if self._preview_scale <= 0:
            return None
        x = self.preview.canvasx(event.x) / self._preview_scale
        y = self.preview.canvasy(event.y) / self._preview_scale
        return x / PREVIEW_DPI * 25.4, y / PREVIEW_DPI * 25.4

    def _on_preview_motion(self, event):
        point = self._event_mm(event)
        if point is None:
            return
        self.cursor_info.set(f"{point[0]:.1f} mm  ·  {point[1]:.1f} mm    Umschalt+Klick setzt die Etikett-Ecke")

    def _on_preview_click(self, event):
        if not (event.state & 0x0001):
            return
        point = self._event_mm(event)
        if point is None:
            return
        self.stamp_x.set(f"{point[0]:.1f}")
        self.stamp_y.set(f"{point[1]:.1f}")

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
            if key in self.entries:
                self.entries[key].set(value)
            elif key in self.texts:
                widget = self.texts[key]
                widget.delete("1.0", "end")
                widget.insert("1.0", value)
        for key, value in long.items():
            widget = self.texts[key]
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
        self.infekt.set("ja")
        self.transport.set("ktw")
        self.ktw_mode.set("sitzend")
        self.begleit.set("anz")
        for key in ("nuechtern", "suizidal", "versichert", "goae"):
            if key in self.bools:
                self.bools[key].set(True)
        self.schedule()

    def clear_fields(self):
        for key, var in self.entries.items():
            if self._kept(key):
                continue
            var.set(_today() if key in DATE_KEYS else "")
        for key, widget in self.texts.items():
            if self._kept(key):
                continue
            widget.delete("1.0", "end")
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
        }

    def _apply_payload(self, data: dict, only_remembered: bool = False):
        self._ready = False
        self._suspend_layout = True
        try:
            for key in ("printer", "copies", "shift_x", "shift_y", "stamp_mode"):
                if key in data and data[key] is not None:
                    getattr(self, key).set(str(data[key]))
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
            for key, var in self.entries.items():
                if key in values and keep(key):
                    var.set(str(values[key]))
            for key, widget in self.texts.items():
                if key in values and keep(key):
                    widget.delete("1.0", "end")
                    widget.insert("1.0", str(values[key]))
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
            if only_remembered:
                for key in DATE_KEYS:
                    if not self._kept(key) and key in self.entries:
                        self.entries[key].set(_today())
            self._apply_all_importance()
        finally:
            self._ready = True
            self._suspend_layout = False
        self._place_blocks()
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
        self.entries["brief_datum"].set(_today())
        self.entries["verw_datum"].set(_today())
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
