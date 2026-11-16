---
applyTo: "src/**/*.py,tests/**/*.py,tools/**/*.py,eval/**/*.py"
---

- Use Python 3.12 or later, the ruff rules in `pyproject.toml`, and type hints.
- Keep tests offline and deterministic.
- Run live operations only through the guarded clients.
- `src` never imports `eval`.
