# fyntok

A local application for refining categories across historical transactions. Milestone 1 provides persistent storage, CSV import, and SQL analysis. The NiceGUI interface and AI workflow follow in later milestones.

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/). Install the pinned environment and import the private CSV:

```bash
uv sync --locked
uv run --locked fyntok import data/data.csv
uv run --locked fyntok analyze --by monthly
uv run --locked fyntok analyze --by trends
uv run --locked fyntok analyze --by merchant
uv run --locked pytest -q
```

The database defaults to `data/fyntok.sqlite3`; select another with `fyntok --database /path/to/file.sqlite3 import /path/to/export.csv`. Data, databases, and virtual environments are ignored by Git. `uv.lock` fixes transitive dependencies. Direct dependencies are pinned; Plotly 6.9.0 is the latest stable version compatible with NiceGUI's Plotly extra (`<7`).

Imports expect six headerless, semicolon-separated CP1252 fields: account, description, booking date, value date, signed German decimal amount, and currency. Dates use `DD.MM.YYYY`; amounts require exactly two decimal places. The original file bytes and decoded fields are retained, including both dates and the account field. Validation or write failure rolls back the entire import. Financial values are stored and summed as integer cents.

Each successful import records its filename, hash, original bytes, timestamp, counts, and a mapping from every source row to its stored transaction. Repeat imports add an import-history entry but reuse transactions. Matching uses all six original fields plus the occurrence number within the export: two identical rows in a file remain two transactions, and overlapping exports retain the greatest observed multiplicity. With no bank-provided transaction ID, identical fields cannot distinguish an already imported transaction from a new identical transaction. Exports with changed fields count as new rows; review those inputs before importing.

Analysis groups by booking month, currency, category, and financial bucket; merchant summaries use the same measures. Trends are the monthly category series. Each result includes transaction count, signed net cents, positive outgoing cents, and positive incoming cents. Unassigned transactions are Uncategorized regardless of sign. Assigned internal-transfer categories are separate; other categorized credits and income categories are income. Remaining categorized transactions are spending. No currency conversion or refund/reimbursement matching occurs. Merchant display groups use the text after the final `|` (or the full description when absent), with whitespace normalized; these groups do not assign categories.

The SQLAlchemy schema retains stable category IDs and definitions, previous and active assignments (at most one active assignment per transaction), one latest confirmed example per transaction, and durable categorization runs. Runs retain scope, category/example snapshots, and per-transaction revision, status, attempts, validated result, error, and applied assignment ID so subsequent milestones can implement safe resume and protection against intervening edits. SQLite foreign keys are enabled; writes use WAL and full synchronization. Schema version 1 is recorded, and unknown versions are refused until an explicit migration is implemented.

Tests use temporary databases. The private dataset check runs when `data/data.csv` is present and otherwise reports a skip. It verifies all [private row count omitted] rows, every monthly total against an independent CSV calculation, both dates, exact net/debit/credit totals, repeat imports, and persistence after reopening. Synthetic checks cover duplicate rows, overlapping imports, invalid input rollback, signed amounts, currencies, active assignment aggregation, assignment history, confirmed examples, foreign keys, and persisted run progress.
