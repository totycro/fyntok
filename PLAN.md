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

## Completed categorization experiment

On October 8, 2026, the [retained uv experiment](experiments/categorization/README.md) parsed the supplied [private row count omitted]-row CSV and compared categorization with and without a held-out example. It tested an illustrative EXAMPLE GROCERY → Groceries example and the user's confirmed EXAMPLE CARE → Personal care preference, with two repetitions per condition. Exact requests, streamed responses, fixtures, validation code, and results are retained for reuse in implementation.

All 16 live requests completed through the user's ChatGPT-plan OAuth connection with valid structured output: eight using `gpt-6.1-sol` and eight using `gpt-6-luna`. Both models assigned EXAMPLE GROCERY and EXAMPLE FOODS to Groceries and EXAMPLE CARE to Personal care. Luna matched Sol on all 52 paired assignments across seven distinct target transactions. Mean observed response time was 6.87 seconds for Luna versus 9.28 seconds for Sol; timings were not a controlled benchmark. See the [categorization report](experiments/categorization/REPORT.md) and [model comparison](experiments/categorization/MODEL_COMPARISON.md).

Luna is a suitable starting model for these straightforward categorization cases; retain the account-supported model selector. Adding examples did not change categories because their labels already matched baseline assignments. This experiment verifies the API integration and example input format, but does not establish accuracy on ambiguous transactions, correction of a wrong assignment, or example retrieval at scale. Evaluate those cases against human-reviewed transactions during implementation.

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
