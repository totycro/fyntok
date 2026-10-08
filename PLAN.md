# fyntok: Iterative categorization of historical transactions

## Summary

Build a local application using **Python, NiceGUI, Plotly, and SQLite**, with AI supplied through the user's **ChatGPT Pro plan**.

The main workflow is to import several years of transactions, then repeatedly improve categories and assignments across the entire historical dataset. Corrections guide both historical reprocessing and future imports. Users may revisit this process only every few years, spending time refining the historical assignments during each session.

## Runtime and dependencies

- Use Python 3.14 and manage the project with `uv`.
- At implementation time, select the current stable versions of all libraries that are compatible with Python 3.14. Pin each direct dependency to an exact version (`==`) and commit `uv.lock` to fix transitive dependencies as well. Update versions deliberately and validate the changes.

## Import and analysis

- Import the existing headerless CSV, handling semicolon separators, CP1252 encoding, German dates, and decimal commas. The current file is at `data/data.csv`.
- Preserve original transaction data, store amounts as integer cents. 
- Show monthly category totals and transaction counts, category trends, merchant summaries, and a filterable transaction table. For now do not worry about refunds or reimbursements.
- Use the first date column as booking date for monthly aggregation; retain both date columns.
- Show outgoing spending as positive amounts, with income, internal transfers, and uncategorized transactions separately.
- Calculate financial totals deterministically in SQL.

## Iterative refinement workflow

1. **Propose categories:** The LLM suggests names and definitions from representative descriptions spanning the dataset. Users edit and save them.
2. **Initial categorization:** Apply the saved categories to all imported transactions and display suggestions alongside the charts.
3. **Refine assignments:** Users correct one transaction at a time and adjust category definitions. Human-confirmed assignments become examples for future LLM categorization. Do not implement bulk manual categorization.
4. **Reprocess history:** Offer a prominent "Recalculate categories" action for all transactions or the current filtered selection.
5. **Apply automatically:** Recalculation automatically updates transactions in its scope that have not been manually assigned. If the user spots more mistakes, they correct individual transactions and recalculate again.

Reprocessing uses the latest categories and human-confirmed examples. It can revisit previously categorized transactions, including earlier AI suggestions.

### Example interaction: correcting supermarket purchases

1. The user notices that a supermarket transaction was incorrectly categorized as "Shopping".
2. The user changes that one transaction to "Groceries". The application protects its manual assignment and stores it as a human-confirmed example.
3. The user clicks "Recalculate categories" for all transactions.
4. The LLM requests include the corrected supermarket transaction as an example when categorizing related transactions. The LLM uses that example and the current category definitions to assign similar supermarket purchases to "Groceries".
5. Recalculation automatically updates transactions that have not been manually assigned; the corrected example remains protected. The user reviews the results and, if further mistakes remain, corrects another individual transaction and recalculates again.

The user does not need to select all supermarket transactions or create a merchant rule. The correction guides future LLM categorization through examples.

## Corrections and category changes

- Categorization priority: individual correction → LLM suggestion informed by human-confirmed examples.
- Individual corrections remain protected during reprocessing.
- Correcting a category changes only that individual transaction. Do not offer bulk manual categorization, "remember for this merchant", or merchant rules. Use recalculation to propagate the guidance from individual corrections.
- Store all human-confirmed individual assignments as examples. Every future LLM categorization request receives relevant examples from the latest human decisions, for both new imports and historical reprocessing. These examples guide suggestions without creating deterministic merchant-wide assignments. The application supplies this memory; the model is not retrained.
- When a human changes a previous decision, replace its example with the latest confirmed assignment. AI-only assignments do not become confirmed examples automatically.
- Renaming a category retains its assignments. Merging moves assignments and confirmed examples to the destination.
- Changing a definition offers reprocessing of affected historical transactions.
- Deleting a category requires choosing a destination or moving its transactions to Uncategorized.
- Category additions suggested by the LLM require user approval.
- Label model assignments as AI-generated so users can identify and correct them individually. Do not treat model-written confidence as calibrated probability.

## Persistence and AI integration

- Use SQLite through SQLAlchemy to store transactions, stable category IDs and definitions, human-confirmed assignments used as examples, import history, and categorization-run history.
- Record assignment source and retain previous assignments so changes are explainable and reversible.
- Validated LLM assignments automatically become active assignments for eligible transactions during recalculation. Dashboard totals reflect active assignments.
- Detect intervening user edits during a run and exclude those transactions from automatic application.
- if categories are changed, users are responsible for recalculating the other definitions if necessary.
- Implement supported Sign in with ChatGPT OAuth, persistent local registration, token refresh, and sign-out. Discover account-supported Responses models and provide a model selector.
- Keep credentials in a protected local credential store outside the transaction database and repository.
- Follow subscription request requirements, including streaming, `store: false`, and explicit request context.
- Send minimized descriptions, current category definitions, and relevant human-confirmed examples with every categorization request. Remove account identifiers and unrelated personal details.
- Run asynchronously with progress, cancellation, and resumable completed work.
- Validate structured AI responses and reject unknown category IDs or malformed assignments without losing imported data.
- Keep AI integration behind a small replaceable Python interface. Use the regular Responses API for both category proposals and classification in this version.
- Do not automatically switch to separately billed API access. Manual import, editing, and analysis remain available when AI access is unavailable.

## Categorization experiment

The [categorization experiment](experiments/categorization/README.md) supports comparisons with and without examples. Published fixtures contain synthetic data only. Private live requests, responses, and reports are excluded from public history. Synthetic fixtures are demonstrations, not evidence of model accuracy.

## Implementation milestones and status

Implement these milestones in order, using each completed milestone as a working checkpoint. Update each status as work progresses: **Not started**, **In progress**, **Blocked** (with the reason), or **Complete**. Record completed work, remaining work, and validation results here so implementation can continue across context windows. Mark a milestone complete only after its completion criteria are verified.

The retained categorization experiment and application milestones 1–3 are complete. Milestone 4 reliability implementation is finished; human-reviewed live accuracy evaluation remains open. Reuse its validated parsing and integration code where appropriate; production integration and application behavior still require implementation and validation.

### Milestone 1: Data and persistence

**Status: Complete**

- Set up the Python 3.14 application with `uv`, exact direct dependency versions, and a committed lockfile.
- Implement the SQLAlchemy/SQLite schema for transactions, stable categories, active and previous assignments, human-confirmed examples, imports, and categorization runs.
- Import the supplied CSV while preserving original data, both dates, and integer-cent amounts; record import history and define repeat-import behavior.
- Implement deterministic SQL aggregation for monthly totals and counts, trends, and merchant summaries, with spending, income, internal transfers, and uncategorized transactions represented separately.

**Completion criteria:** The supplied dataset imports into persistent storage; exact totals, date parsing, monthly aggregation, and reload behavior pass automated checks. The schema supports assignment history and durable run progress required by later milestones.

Completed October 8, 2026:

- Added the Python 3.14 `uv` project, exact dependency pins, `.python-version`, and root `uv.lock`. Selected SQLAlchemy 2.1.4, NiceGUI 3.18.0, pytest 9.1.1, and Plotly 6.9.0 (the latest stable version compatible with NiceGUI's Plotly extra).
- Implemented schema version 1 with stable categories, transactions, import bytes and row provenance, active/previous assignments, latest confirmed examples, and categorization runs with snapshots and durable per-transaction results, status, attempts, and revision checks for later application logic. Enforced foreign keys, one active assignment, and matching transaction/category references for examples and history.
- Implemented atomic CP1252 CSV import with both dates, all original fields, integer cents, and import history. Repeat/overlapping exports reuse exact rows by field hash and occurrence number; duplicate multiplicity within exports is preserved. The ambiguity of identical transactions across exports is documented in README.md.
- Implemented deterministic SQL monthly category totals/counts, category trends, and merchant summaries with separate financial buckets and currency grouping. Added command-line import and analysis commands and local setup documentation.
- Verified persistent import, repeat-import deduplication, and monthly totals against an independent CSV calculation. Private financial totals and date ranges are omitted.
- Validation: `uv sync --locked` succeeded on Python 3.14.4; `uv run --locked pytest -q` passed **13 tests** including every supplied row, both dates, every monthly total against an independent CSV calculation, exact totals, reopen/repeat behavior, malformed import rollback, duplicate/overlap behavior, currencies and financial buckets, active assignment uniqueness, history/examples, durable run progress, and unknown schema refusal. Private-data checks skip explicitly if the supplied CSV is absent.

Remaining work at this checkpoint was milestones 2–4. Milestone 2 is now complete below; AI integration and actual run application/cancellation/resume behavior remain for milestones 3–4.


### Milestone 2: Manual application

**Status: Complete**

- Build the localhost NiceGUI application with Plotly charts, merchant summaries, and a filterable transaction table.
- Implement category creation, definitions, rename, merge, and deletion with destination selection or Uncategorized.
- Allow individual transaction corrections, protect manual assignments, and maintain the latest human-confirmed example for each corrected transaction.
- Show assignment source and history, and implement undo for assignment changes.

**Completion criteria:** Import, analysis, category management, and individual corrections work without AI access. Verify category lifecycle behavior, protected corrections, replacement of outdated examples, and undo. Do not provide bulk manual categorization or merchant rules.

Completed October 8, 2026:

- Added `fyntok serve` bound to `127.0.0.1`, with browser CSV upload/import history, Plotly monthly amounts/counts and category trends, merchant summaries, and a paginated transaction table. Date, category, merchant, description, source, and currency filters are shared by the table and SQL analysis. Spending displays as positive amounts; income, transfers, Uncategorized, and currencies remain identifiable separately.
- Added atomic category creation, definitions/type edits, stable-ID renames, merge, and deletion with an explicit destination or Uncategorized. Removed categories are archived to preserve historical foreign keys; active assignments and confirmed examples move to the destination, keeping manual protection. Uncategorized is permanent and archived names remain reserved.
- Added individual corrections with explicit protection, one latest confirmed example per transaction, assignment source/history, and repeated undo through prior assignments. Undo appends history, restores the prior category/protection/example, and clears the example when returning to AI/unassigned state. Restored AI assignments retain their AI-generated display label. No bulk manual assignment or merchant rules are provided.
- Added schema version 2 and transactional migration from version 1 for category archives, explicit assignment protection, and undo restoration references. Writes serialize and check editor revisions to reject stale edits. Updated README.md with startup, manual workflows, migration, and current limitations.
- Validation: `uv run --locked pytest -q` passed **21 tests** (the original 13 plus seven manual-service/migration checks and one end-to-end NiceGUI user-simulation check). Tests cover correction/example replacement, protection, undo to manual/AI/unassigned states, repeated undo and reopen persistence, category lifecycle/history, invalid operations and stale revisions, shared SQL filters, version-1 migration/reopen, and UI upload, create/rename/merge/delete, row correction, undo, and filtering. The simulator also checks for application error logs.
- Verified CLI server startup and database migration with existing transactions preserved. Private dataset statistics are omitted.

Remaining work: milestones 3–4. Definition changes currently explain that historical AI recalculation comes in milestone 3. Category lifecycle operations have no undo button; individual assignment undo refuses to restore an archived category, requiring an explicit correction to an existing category instead. These boundaries are documented and tested.

### Milestone 3: AI workflow

**Status: Complete**

- Adapt the retained experiment behind a small replaceable Python interface, with supported ChatGPT sign-in, persistent registration, protected credentials, token refresh, sign-out, model discovery, and model selection.
- Implement category proposals from representative descriptions, requiring user approval before adding categories.
- Implement initial categorization and recalculation for all transactions or the current filtered selection, using current definitions and relevant latest human-confirmed examples.
- Minimize request data, follow subscription request requirements, validate structured responses, and automatically apply valid assignments to eligible transactions while preserving manual corrections and assignment history.
- Run AI work asynchronously with visible progress and persist completed work for the reliability milestone.

**Completion criteria:** The application supports sign-in, approved category proposals, and whole-dataset or filtered recalculation. Verify the supermarket correction interaction, example use for new imports and repeated reprocessing, replacement of changed examples, and exclusion of manual assignments and intervening user edits from automatic application. Manual workflows remain available when AI access is unavailable.

Completed October 8, 2026:

- Adapted the retained experiment into a small `AIClient` protocol and public streaming Responses adapter. Rechecked the official sign-in, session, inference, and preview documentation. Requests include explicit context, `store: false`, strict structured output, and only account-selected models; there is no API billing fallback.
- Added ChatGPT OAuth controls with ephemeral localhost callbacks, state/nonce/PKCE and signed ID-token verification, saved account registrations and stable host ID, protected atomic credential writes outside the repository/database, process-serialized rotating refresh, and sign-out with attempted remote revocation. Saved registrations support account selection, returning sign-in, and new account registration.
- Added representative-description category proposals with editable drafts and explicit individual approval. Added all-history and filtered recalculation using current definitions and latest confirmed examples, bounded relevance ranking, minimized descriptions, and automatic application with assignment history and manual protection.
- Added background execution, visible and durable batch progress, validated results and assignment IDs, serialized run creation/claim, and revision checks before application. Category/example changes conservatively skip stale results. Completed batches survive later failures and database reopening.
- Added readable acceptance stories with a deliberately mistaken scripted advisor: one supermarket correction guides history, changed decisions replace old examples, new imports and repeated runs use guidance, filters limit scope, intervening decisions/definitions win, AI changes support undo, proposals wait for approval, updated definitions reach requests, and AI failures leave manual review available.
- Validation: `uv run --locked pytest -q` passed **55 tests**, including **10 acceptance cases** and two NiceGUI user-simulation workflows. Additional checks cover malformed/unknown/duplicate output, relevance ranking, request minimization and streaming flags, incomplete streams, durable batches, actual signed-token validation, rejected nonce/signature/expiry/identity, token rotation, protected file permissions, registration reuse, and sign-out when remote revocation is unavailable. A real CLI server smoke check returned HTTP 200 and rendered the AI controls against a temporary database. No live authentication/inference or visual browser inspection was performed in this milestone; mocked boundary tests and the retained live experiment provide the current evidence.

Remaining work is milestone 4: cancellation/resume and interrupted-process recovery UI, broader session/limit failure tests, and manually reviewed model/retrieval accuracy. Credentials use Unix file locks (Linux/macOS). Free-form description minimization is heuristic; definitions are transmitted as written. An interrupted process can leave a pending/running run that blocks a new one until recovery is implemented. These limitations are documented in README.md.

### Milestone 4: Run reliability and validation

**Status: In progress** — reliability implementation and regression validation complete; human-reviewed live accuracy evaluation pending.

- Complete cancellation and resume behavior using persisted run progress, preserving completed work without duplicate application.
- Verify transaction and category edits during runs, including protection against applying stale results and correct undo behavior.
- Exercise interrupted streams, malformed output, unknown category IDs, expired sessions, token refresh, sign-out, and subscription limits without losing imported data or silently switching to API billing.
- Evaluate categorization and example retrieval against a manually reviewed sample covering common merchants, unfamiliar descriptions, ambiguous transfers, and corrections that differ from the model's baseline assignment.
- Document local setup, operation, recovery, and remaining limitations.

**Completion criteria:** Reliability and regression checks pass, recovery behavior is documented, and the reviewed-sample evaluation records results and remaining limitations. All validation requirements below have been addressed, with any unmet acceptance criteria explicitly recorded rather than marked complete.

Implemented October 8, 2026:

- Added durable cancellation for pending/running runs and browser controls to select, cancel, resume, or recover any saved run. An in-flight response is discarded after cancellation; committed batches remain applied. Resume preserves original scope/model/category/example/revision snapshots and retries only unfinished items, leaving applied/skipped items and assignment IDs intact. Undo after a completed batch is never replayed by resume.
- Added a nonblocking Unix worker lock per database, held across recovery and execution. Live workers cannot be recovered from another thread/process; process death releases the lock. Recovering an abandoned running item increments its attempt count and retries it without duplicating completed assignments. Cancellation permits abandoning an interrupted run before starting fresh.
- Added safe HTTP and streamed error handling for sessions, permission/eligibility, subscription limits, unsupported requests, and temporary routing failures. Requests stop with completed work retained and no automatic retries or billing fallback. Known error codes and statuses produce actionable recovery messages; raw server messages are omitted to avoid persisting private inputs. Terminal refresh-token failures clear unusable tokens while retaining registration; temporary failures preserve credentials.
- Added regression checks for in-flight/between-batch/pending cancellation, actual process death, worker exclusion across threads/processes, failed-run resume, concurrent transaction/category edits (rename/merge/delete), manual protection, undo during work, and undo before resume. NiceGUI simulation verifies cancellation and recovery after page reload and stable status after sign-out.
- Added `evaluations/sample.json`, a held-out synthetic candidate sample covering common merchants, unfamiliar descriptions, own-account/ambiguous transfers, and a correction preference. Added `python -m fyntok.evaluation` for paired baseline/example-guided inference with per-group scores, retrieval IDs, observed baseline disagreements, and correction outcomes. The harness requires a recorded human reviewer and rejects held-out ID leakage. Its scripted test verifies scoring and retrieval of older relevant examples behind 50 distractors; scripted scores are not live model accuracy.
- Updated README.md with setup, cancellation timing, recovery operation, unchanged snapshots on resume, local SQLite/Unix lock requirements, session/usage failure recovery, and remaining limitations. Recorded the accuracy-evaluation protocol and outstanding evidence in [evaluations/REPORT.md](evaluations/REPORT.md).
- Validation: `uv run --locked pytest -q` passed **79 tests**, including the existing data/acceptance checks, a forced child-process exit mid-run, HTTP/streamed session and subscription failures, refresh-token failure cases, and three NiceGUI workflows. `git diff --check` passed. Tests use temporary databases, scripted advisors, and mock transports; no new live sign-in/inference or visual browser inspection was performed.

**Unmet acceptance criterion:** A person must review and confirm the candidate sample/category labels, then the paired evaluation must run through an account-supported live model and record actual accuracy, retrieval, and correction results. The candidate fixture deliberately has no reviewer; user review was requested during implementation. No human labels or live results are fabricated. The retained experiment does not cover the required unfamiliar/ambiguous cases or a correction differing from the model baseline. Milestone 4 therefore remains In progress, rather than Complete, until that evidence is recorded. Credentials/worker locks remain Unix-only and description minimization/example relevance remain heuristic.

## Validation

- Test exact import totals, date parsing, monthly aggregation, and persistence.
- Verify that individual human decisions supply examples to subsequent categorization of new imports and repeated whole-dataset reprocessing, and that changed decisions replace outdated examples.
- Verify the supermarket interaction: correcting one transaction supplies the example to LLM requests for related transactions during whole-dataset recalculation, while preserving the manual assignment.
- Verify automatic application, protected corrections, undo, and edits made during a run.
- Verify category rename, merge, and deletion behavior.
- Test interrupted AI runs, malformed output, unknown categories, expired sessions, token refresh, sign-out, and subscription limits.
- Evaluate categorization against a manually reviewed sample covering common merchants, unfamiliar descriptions, and ambiguous transfers.

## Defaults and assumptions

- One local user; bind the application to localhost.
- One category per transaction.
- User-approved category changes and automatic historical reassignment during recalculation, with individual corrections protected.
- The user's ChatGPT-plan access was verified through completed inference with Sol and Luna in the retained experiment. Validate granted plan-usage permission during sign-in and handle later access or usage-limit failures.
- OpenAI Decisions, Jev, fine-tuning, and separate API billing are excluded from the first version.
- SQL and application logic handle financial calculations; the LLM supplies categorization suggestions.

## Integration references

The subscription integration was reviewed on October 8, 2026. Verify current requirements during implementation.

- [Sign in with ChatGPT integration guide](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt)
- [ChatGPT plan usage overview](https://developers.openai.com/siwc/token-sharing-open-source)
- [Supported models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
