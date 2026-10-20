# fyntok

A local application for refining categories across historical transactions. The application provides persistent storage, CSV import, SQL analysis, a localhost NiceGUI interface, ChatGPT-plan category proposals, and historical recalculation with cancellation and restart recovery.

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

For development, run `just dev` (requires [just](https://just.systems/)) with debug logging enabled and bound to `0.0.0.0:8080`. Python changes under
`src/fyntok` automatically restart the server. Saved SQLite data persists, but browser UI state
may reset. Normal `fyntok serve` runs without reload. To enable reload with a custom database or
port, use `uv run --locked python -m fyntok.cli --database /path/to/file.sqlite3 serve --reload --port 8081`.

The database defaults to `data/fyntok.sqlite3`; select another with `fyntok --database /path/to/file.sqlite3 import /path/to/export.csv`. Data, databases, and virtual environments are ignored by Git. `uv.lock` fixes transitive dependencies. Direct dependencies are pinned; Plotly 6.9.0 is the latest stable version compatible with NiceGUI's Plotly extra (`<7`).

To remove test transactions while keeping all category names and definitions, stop the app and run:

```bash
uv run --locked python scripts/clear-transactions.py --dangerously-delete-all-transactions
```

Use `--database /path/to/file.sqlite3` to select another database. The full danger flag is required;
abbreviations are rejected. The script creates a timestamped SQLite backup beside the database before
clearing transactions, import history, assignments, confirmed examples, and categorization runs.
Restart the app afterward to import fresh data.

Imports expect six headerless, semicolon-separated CP1252 fields: account, description, booking date, value date, signed German decimal amount, and currency. Dates use `DD.MM.YYYY`; amounts require exactly two decimal places. The original file bytes and decoded fields are retained, including both dates and the account field. Validation or write failure rolls back the entire import. Financial values are stored and summed as integer cents.

Each successful import records its filename, hash, original bytes, timestamp, counts, and a mapping from every source row to its stored transaction. Repeat imports add an import-history entry but reuse transactions. Matching uses all six original fields plus the occurrence number within the export: two identical rows in a file remain two transactions, and overlapping exports retain the greatest observed multiplicity. With no bank-provided transaction ID, identical fields cannot distinguish an already imported transaction from a new identical transaction. Exports with changed fields count as new rows; review those inputs before importing.

Analysis groups by booking month, currency, category, and financial bucket; merchant summaries use the same measures. Trends are the monthly category series. Each result includes transaction count, signed net cents, positive outgoing cents, and positive incoming cents. Unassigned transactions are Uncategorized regardless of sign. Transactions whose active category has the Internal transfer type are excluded from all analysis totals and transaction counts, including CLI output. Other categorized credits and income categories are income. Remaining categorized transactions are spending. No currency conversion or refund/reimbursement matching occurs. A shared `parse_description(description, account="")` parser returns `merchant_name` and `cleaned_description`. New imports store cleaned merchant names, removing recognized payment-processor prefixes, branch metadata, bank identifiers, and backslash-delimited locations while preserving brand digits such as EXAMPLE24. Empty names become Unknown. These groups do not assign categories. Existing stored merchant names remain unchanged, including on duplicate imports.

The SQLAlchemy schema retains stable category IDs and definitions, previous and active assignments (at most one active assignment per transaction), one latest confirmed example per transaction, and durable categorization runs. Runs retain scope, category/example snapshots, and per-transaction revision, status, attempts, validated result, error, and applied assignment ID so subsequent milestones can implement safe resume and protection against intervening edits. SQLite foreign keys are enabled; writes use WAL and full synchronization. Schema version 2 is recorded. Opening a milestone 1 database automatically migrates version 1 in a transaction, preserving all data and marking existing manual assignments as protected; unknown versions are refused.

Tests use temporary databases. The private dataset check runs when `data/data.csv` is present and otherwise reports a skip. It verifies all [private row count omitted] rows, every monthly total against an independent CSV calculation, both dates, exact net/debit/credit totals, repeat imports, and persistence after reopening. Synthetic checks cover duplicate rows, overlapping imports, invalid input rollback, signed amounts, currencies, active assignment aggregation, assignment history, confirmed examples, foreign keys, and persisted run progress.


In the browser, upload an export under **Import CSV**, or keep using the CLI importer. The analysis and transaction table share booking-date, category, merchant, description, assignment-source, and currency filters. Click **Apply filters** after editing filters. Plotly charts show monthly amounts/counts and category trends; merchant summaries include exact outgoing, incoming, and signed net values. Internal transfers are excluded from charts, statistical transaction counts, and merchant summaries but remain available in the transaction list. The matching-transaction count refers to that list, including transfers. Filtering to an internal-transfer category produces empty statistics while retaining its transactions in the list. Spending is positive; Uncategorized displays signed net amounts, and currencies remain separate series. Table amounts remain signed. Charts use floating-point units only for display; totals are computed in SQL as integer cents.

Use **Categories** to create a category, edit its name/definition/financial type, merge it into another category, or delete it with a destination (default Uncategorized). Renames retain stable IDs. Category removal moves current assignments and confirmed examples atomically, preserves manual protection, and archives the old category for history. Uncategorized is permanent. Archived names remain reserved to avoid confusing historical references. Definition changes are saved immediately; use **Recalculate categories** to revisit history with the new definitions.

In **Transactions**, click one row to see both dates, its assignment source, protection status, and assignment history. **Confirm individual category** changes only that transaction and replaces its latest confirmed example. There is no bulk correction or merchant assignment rule. **Undo latest assignment change** walks back the transaction's assignment changes, appends an audit record, and restores the previous manual protection/example (or removes the example when restoring an AI/unassigned state). An undo that would restore a merged/deleted category is refused; choose an existing category explicitly instead. Category lifecycle operations themselves have no undo button. Stale editors are rejected using transaction/category revisions; reopen the editor after another tab changes a record.

The app defaults to `127.0.0.1` and needs no AI credentials for these workflows. Use `fyntok serve --host 0.0.0.0` to listen on all interfaces, or `fyntok serve --port 8081` for a different port. Changes persist immediately in the selected database. The UI test uses NiceGUI's built-in user simulator to exercise upload, category editing/deletion, row correction, undo, and filtering; it does not replace a visual browser check.

`fyntok serve` logs the database path, server startup/shutdown, HTTP requests, and import/manual-action outcomes to the terminal. Use `fyntok serve --log-level debug` to trace UI actions, including AI proposals, sign-in, recalculation, cancellation/recovery, category edits, transaction corrections, filters, and navigation. Actions log what is starting, why they are blocked, and their results; run progress is logged when it changes. Recalculation workers also log each AI batch before sending it, response validation time, committed applied/skipped counts, and the final run summary. Batch logs include run IDs and counts without transaction descriptions or AI response bodies. Handled UI errors and background categorization failures log full tracebacks at ERROR level, including the original exception message, with the default logging settings. Use `--log-level warning` to reduce routine output while retaining errors.


To save the actual LLM queries and responses, explicitly enable tracing:

```bash
uv run --locked fyntok serve --log-level debug --trace-llm
```

Tracing works independently of the log level and is disabled by default, including in `just dev`.
Each proposal or classification request gets a unique UTC-timestamped directory relative to the
working directory, ordered chronologically by name:

```text
data/llm-traces/2026-10-13T14-35-02.123456Z_classify_<request-id>/
  request.json
  response.txt
  metadata.json
```

`request.json` contains the complete request body: model, instructions, input, and output schema.
Authentication headers and credentials are excluded. `response.txt` preserves assembled model
output, including malformed JSON, refusal text, partial output from failed/interrupted streams,
or an HTTP error response body. Streaming events are not saved individually. `metadata.json`
records UTC start/end times, duration, model, operation, HTTP/stream status, exception type,
and categorization run ID/batch number where applicable. Its final status reflects the AI
transport and JSON parsing, rather than whether database assignments were subsequently applied.
Normal INFO logs link to trace directories without printing request or response bodies.
Trace-writing errors emit a warning and do not stop categorization. Files remain until manually
deleted and the default `data/` location is ignored by Git.

Under **AI categorization**, click **Continue with ChatGPT**, open the sign-in link, grant ChatGPT plan usage, and return to propose categories or recalculate transactions. New requests use `gpt-6.1-sol`, fixed in the `AI_MODEL` constant in `src/fyntok/ai.py`. If it is unavailable, requests fail visibly without switching models; edit that constant manually to change it. The app starts an ephemeral loopback callback listener on `127.0.0.1`; run the app on the same computer as the browser for sign-in. Select a saved account to reauthorize it, or clear the account selector to register another account. Registrations and the stable host ID persist across restarts and sign-out. Credentials live in `~/.config/fyntok/accounts.json` with owner-only permissions, outside the transaction database and repository. Refresh is serialized across processes with Unix file locks; this integration currently targets Linux/macOS. **Sign out** attempts remote session revocation and clears the selected account's local tokens. If remote revocation cannot be confirmed, the app says so and links to ChatGPT Settings.

**Propose categories** samples up to 120 distinct cleaned descriptions distributed across booking history. Review and edit each suggestion, then click **Approve category** to save it; dismiss unwanted suggestions. Proposals never add categories automatically. **Recalculate categories — all transactions** performs initial categorization or revisits earlier AI assignments. **Recalculate categories — current filters** freezes the matching selection when the run starts. Both automatically apply validated results, preserve assignment history, and update the dashboard when finished. Use individual correction and undo as before.

For example, correct one supermarket purchase from Shopping to Groceries, then recalculate history. The corrected purchase remains protected and becomes guidance for related purchases and later imports. Each run snapshots the current category definitions and latest confirmed decisions; each request receives up to 40 examples ranked by description-word overlap, with recent examples as fallback. This is guidance supplied to the model, not a merchant rule. Changed decisions replace outdated examples. AI-only assignments never become confirmed examples. If transactions, definitions, or confirmed examples change during a request, stale results are skipped; changes to shared definitions or examples conservatively exclude subsequent results from that run. Start a fresh recalculation with the latest guidance.

Requests use the public streaming Responses API, explicit context, and `store: false`, following [OpenAI's ChatGPT-plan integration requirements](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations). No separately billed API fallback is configured. Signed amounts are sent as integer `amount_cents`; positive means incoming and negative means outgoing. When a positive transaction has no other known purpose, categorization defaults to a suitable saved income category. Evidence of transfers, refunds, or another specific purpose takes precedence. Original fields, accounts, dates, and currencies are omitted. AI descriptions use the same parser for both new and existing transactions. It skips fee/reference suffixes, redacts account identifiers, email addresses, and common labeled references, removes branch numbers while preserving brand digits, and retains city context before limiting text to 160 characters. Empty results become Unknown merchant. This is a heuristic, not a general detector of personal information in arbitrary free text; category definitions are sent as written. Review bank-description conventions and definitions before using AI.

Categorization requests share their prompt, schema, and reference conversion with the current experiment. Categories contain only `name`, `definition`, and `kind`; examples and model responses use `category_name`. Transactions use batch-local references (`t1`, `t2`, …). The adapter validates exact names and complete, unique references, then maps them back to stable database IDs using the run's saved snapshot. Repeated descriptions remain separate transactions. Resumed runs keep their saved context and use the current request serialization.

Runs process 25 transactions per request in a background worker. Progress and each completed batch are saved in SQLite, including validated results and applied assignment IDs. Unknown categories, missing/duplicate transaction IDs, malformed output, and streams without completion are rejected before that batch changes assignments. Failed runs retain completed batches and leave manual workflows available. Use **Categorization run** to select saved history, **Cancel selected run** to stop application, and **Resume / recover selected run** to retry unfinished work. Cancellation is saved immediately; an in-flight request may consume plan usage and take up to its transport timeout to return, but its result is discarded. Completed batches remain applied. Resume retries pending/failed/interrupted items and preserves completed/skipped items, assignment IDs, scope, original model, definitions, examples, and transaction revisions. It does not replay an assignment you subsequently undid. After category/example edits, start a fresh recalculation to use the latest guidance; stale resumed results are skipped.

After a process crash or restart, select the unfinished run and click **Resume / recover selected run**. No database manipulation is needed. A nonblocking Unix file lock next to the database (`<database>.ai.lock`) prevents recovery while a worker is alive in any tab/process; the OS releases it when the worker exits. Keep the lock file in place. To abandon an interrupted run and start fresh, cancel it first. Run history includes failed and cancelled runs, so older work remains selectable. Local SQLite files and Unix locks are required; network-mounted databases and Windows are not supported.

Session, access, and subscription-limit failures stop the run with retained batches and no automatic retries or billing switch. Plan-limit messages link through **ChatGPT usage and app access**; check usage and resume when access is restored. Reauthorize revoked/expired sessions with the saved account. Terminal refresh-token errors clear unusable tokens but retain registration; temporary network/routing errors preserve credentials. Known server error codes and HTTP statuses are shown without saving raw server messages in SQLite or normal logs; explicitly enabled LLM trace files retain HTTP error response bodies. Unknown failures are summarized safely. This follows the [official errors and recovery guidance](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery).

The reliability implementation is tested, but human-reviewed live model accuracy remains an open milestone 4 acceptance criterion. See [the evaluation record](evaluations/REPORT.md) for the candidate synthetic sample and reproducible paired evaluation command.

For tracked comparisons of category references, transaction references, and duplicate grouping,
use the [local Promptfoo workflow](experiments/categorization/README.md#tracked-experiments-with-promptfoo).
It exports a private sample for spreadsheet review and tracks a two-model, three-repetition pilot
with accuracy, consistency, tokens, latency, and retained per-row results.

Read the feature stories in `tests/acceptance/test_ai_stories.py`, or run `uv run --locked pytest tests/acceptance -v`. They use a scripted advisor to test the user's workflow deterministically, including an initially wrong supermarket suggestion that a correction changes. Additional tests exercise the Responses transport, credential lifecycle, durable batches, validation, and NiceGUI controls. Tests never sign in or send private transaction data to OpenAI. Live sign-in/inference and visual browser inspection were not repeated for milestone 3; the retained experiment remains the live integration evidence.


Reliability tests additionally force a worker process to exit mid-run, verify exclusion across threads and processes, cancel in-flight/between-batch/pending runs, recover through the UI after reload, and check that resume preserves undo and excludes intervening manual/category edits. HTTP and streamed plan/session failures and terminal versus temporary refresh failures use mock transport. No private data or real credentials are sent by these tests.
