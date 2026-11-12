# Plan

## Open
(none)

## Done
- [x] **Top-left text auto-shrink.** The Fachrichtung box (top-left) no longer wraps automatically. Only entered newlines break lines (max 3 lines); the font shrinks until the widest line and the line stack fit the box. Same rule on screen (`_layout_box`) and in print (`overlay._draw_centered`).
- [x] **Save diagnosis with the patient Etikett.** `remember_etikett(..., diagnose=...)` stores the diagnosis in `etiketten/index.json`. It is saved when a Konsil is saved or printed. Choosing the Etikett again (saved list or file dialog) restores it via `etikett_diagnose` / `_restore_diagnose`.

## Notes
- Fields and positions live in `form_layout.py`; rendering in `overlay.py`; UI in `app.py` (`FormApp`).
