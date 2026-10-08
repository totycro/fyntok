# fyntok

A local application for refining categories across historical transactions. Milestones 1 and 2 provide persistent storage, CSV import, SQL analysis, and a localhost NiceGUI interface for manual review. AI integration follows in milestone 3.

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/). Install the pinned environment and import the private CSV:

```bash
uv sync --locked
uv run --locked fyntok serve
# Open http://127.0.0.1:8080, or use the CLI:
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

The SQLAlchemy schema retains stable category IDs and definitions, previous and active assignments (at most one active assignment per transaction), one latest confirmed example per transaction, and durable categorization runs. Runs retain scope, category/example snapshots, and per-transaction revision, status, attempts, validated result, error, and applied assignment ID so subsequent milestones can implement safe resume and protection against intervening edits. SQLite foreign keys are enabled; writes use WAL and full synchronization. Schema version 2 is recorded. Opening a milestone 1 database automatically migrates version 1 in a transaction, preserving all data and marking existing manual assignments as protected; unknown versions are refused.

Tests use temporary databases. The private dataset check runs when `data/data.csv` is present and otherwise reports a skip. It verifies all [private row count omitted] rows, every monthly total against an independent CSV calculation, both dates, exact net/debit/credit totals, repeat imports, and persistence after reopening. Synthetic checks cover duplicate rows, overlapping imports, invalid input rollback, signed amounts, currencies, active assignment aggregation, assignment history, confirmed examples, foreign keys, and persisted run progress.


In the browser, upload an export under **Import CSV**, or keep using the CLI importer. The analysis and transaction table share booking-date, category, merchant, description, assignment-source, and currency filters. Click **Apply filters** after editing filters. Plotly charts show monthly amounts/counts and category trends; merchant summaries include exact outgoing, incoming, and signed net values. Spending is positive; transfers and Uncategorized display signed net amounts, and currencies remain separate series. Table amounts remain signed. Charts use floating-point units only for display; totals are computed in SQL as integer cents.

Use **Categories** to create a category, edit its name/definition/financial type, merge it into another category, or delete it with a destination (default Uncategorized). Renames retain stable IDs. Category removal moves current assignments and confirmed examples atomically, preserves manual protection, and archives the old category for history. Uncategorized is permanent. Archived names remain reserved to avoid confusing historical references. Definition changes are saved immediately; historical AI recalculation will be available in milestone 3.

In **Transactions**, click one row to see both dates, its assignment source, protection status, and assignment history. **Confirm individual category** changes only that transaction and replaces its latest confirmed example. There is no bulk correction or merchant assignment rule. **Undo latest assignment change** walks back the transaction's assignment changes, appends an audit record, and restores the previous manual protection/example (or removes the example when restoring an AI/unassigned state). An undo that would restore a merged/deleted category is refused; choose an existing category explicitly instead. Category lifecycle operations themselves have no undo button. Stale editors are rejected using transaction/category revisions; reopen the editor after another tab changes a record.

The app binds to `127.0.0.1` and needs no AI credentials for these workflows. Use `fyntok serve --port 8081` for a different port. Changes persist immediately in the selected database. The UI test uses NiceGUI's built-in user simulator to exercise upload, category editing/deletion, row correction, undo, and filtering; it does not replace a visual browser check.
