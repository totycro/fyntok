# Repository layout

- `src/fyntok/`: Python application, NiceGUI dashboard, CLI, SQLite storage, and AI categorization.
- `tests/`: Automated tests; `acceptance/` covers user workflows.
- `data/`: Transaction CSV data.
- `evaluations/`: Categorization evaluation sample and report.
- `experiments/categorization/`: Standalone model experiments, fixtures, and results.
- `README.md` and `PLAN.md`: Usage documentation and implementation plan.
- `pyproject.toml` and `uv.lock`: Project configuration, dependencies, and lockfile.

# Required validation

- Install `uv` and `just`. Development tools are pinned in `uv.lock` and run through `uv`.
- Run `just check` before committing or completing Python changes. This runs strict Ruff
  linting, Ruff formatting validation, ty type checking, and the pytest suite.
- Individual commands: `just lint`, `just typecheck`, and `just test` (accepts pytest arguments).
- Run `just format` to apply safe lint fixes and formatting, then review the diff and rerun
  `just check`. It can exit with remaining lint errors that require manual fixes.
- Checks cover application code, tests, and standalone experiment Python files. Keep function
  annotations complete and fix diagnostics rather than weakening checks. Any targeted suppression
  must explain the underlying third-party limitation or intentional behavior.
