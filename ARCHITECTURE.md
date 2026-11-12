# Architecture

Desktop app (Tkinter) that fills the Konsil form on screen and prints only the answers onto the pre-printed paper form.

| File | Role |
|---|---|
| `app.py` | Main UI (`FormApp`), Etikett handling (`remember_etikett`, index in `etiketten/index.json`, which also holds the diagnosis per Etikett; `etikett_diagnose` reads it back), saving Konsile to `konsile/` |
| `form_layout.py` | `TEXT_FIELDS`, `CHECKS`, stamp defaults: positions of fields on the form |
| `overlay.py` | Draws text/marks onto the form image / print output |
| `gdi_print.py` | Windows GDI printing |
| `printer_size.py` | Printer paper size handling |
| `stations.py` | Station name filter/match |
| `config.json` | Local settings (not committed) |
| `Konsil_Formular_empty.pdf` | Blank form template |

Dependencies: `pywin32`, `pypdfium2`, `pillow`. Packaged with `konsil.spec` (PyInstaller).
