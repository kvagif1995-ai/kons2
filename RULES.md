# Rules

- Do not commit `config.json`, `konsile/`, `etiketten/` (patient data), `build/`, `dist/`.
- Never put patient data in commit messages, logs or test fixtures.
- Printing is calibrated to the paper form: do not change mm offsets or `form_layout.py` positions without checking a test print.
- No automatic line wrapping in form text fields; only user-entered newlines break lines. Shrink the font instead.
- Keep changes small and focused; no refactors beyond the task.
- Windows only (pywin32 / GDI printing). Run with `start.bat` or `python app.py`.
- Update `WORKLOG.md` after each work session; tick items in `PLAN.md`.
