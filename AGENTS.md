# Repository layout

- `src/fyntok/`: Python application, Dash dashboard, CLI, SQLite storage, and AI categorization.
- `tests/`: Automated tests; `tests/acceptance/` covers user workflows.
- `data/`: Transaction CSV data.
- `evaluations/`: Categorization evaluation sample and report.
- `experiments/categorization/`: Standalone model experiments, fixtures, and results.
- `README.md` and `PLAN.md`: Usage documentation and implementation plan.
- `pyproject.toml` and `uv.lock`: Project configuration, dependencies, and lockfile.

# Required validation

- Use Python 3.14; install the pinned environment with `uv sync --locked`.
- Use latest python and library features like pattern matching, t-strings, etc.
- Install `uv` and `just`. Development tools are pinned in `uv.lock` and run through `uv`.
- Run `just dev` for the local dashboard with reload and debug logging.
- Run `just check` before committing or completing Python changes. This runs strict Ruff
  linting, Ruff formatting validation, ty type checking, and the pytest suite.
- Individual commands: `just lint`, `just typecheck`, and `just test` (accepts pytest arguments).
- Run `just format` to apply safe lint fixes and formatting, then review the diff and rerun
  `just check`. It can exit with remaining lint errors that require manual fixes.
- Checks cover application code, tests, and standalone experiment Python files. Keep function
  annotations complete and fix diagnostics rather than weakening checks. Any targeted suppression
  must explain the underlying third-party limitation or intentional behavior.

# Data and behavior

- Use only anonymized bank and transaction-counterparty names in code, comments, tests,
  fixtures, and documentation. Do not include real bank names, account/card product names,
  merchants, payees, or other transaction targets. Use generic labels or synthetic examples;
  keep parsing rules independent of specific institutions and counterparties.
- Keep private CSVs, SQLite databases, and credentials out of Git and test fixtures. Use temporary
  databases and mocked AI/auth transports in tests; the private dataset check skips if absent.
- Store and aggregate money as integer cents; use booking dates for monthly totals.
- Preserve original import fields and assignment history. Recalculation must protect manual
  assignments; only human-confirmed decisions become examples for AI categorization.
