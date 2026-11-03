# Categorization API experiment

## Tracked experiments with Promptfoo

The application now has a separate, local Promptfoo workflow for repeatable
categorization experiments. Its Python provider uses the application's transport
and sign-in, not the standalone experiment credentials below. Nothing in this
workflow changes production assignments or the transaction database.

From the repository root, with Python 3.14 and Node >=22.22.0:

```bash
uv sync --locked
just experiment-install
just experiment-smoke
just experiment-view
```

The smoke test uses a synthetic, offline-only fixture and scripted responses.
It verifies Promptfoo's provider, scoring, saved history, and exports without
authentication or model calls. It is not model accuracy evidence. The viewer
binds to `0.0.0.0` for remote access; saved history is under ignored
`data/experiments/promptfoo/`.
Sharing and telemetry are disabled by the supplied commands.

## Clean slate and archive

All previous local datasets, reviews, runs, Promptfoo history, and standalone
results and reports were moved to
[`data/experiment-archive/20261015T114616932291Z/`](../../data/experiment-archive/20261015T114616932291Z/).
The archive preserves original relative paths; `inventory.json` records each
file's original path, size, and SHA-256 checksum. It is excluded from Git by the
repository's `data/` rule. The earlier `data/OLD/` archive remains intact.

Active experiment storage and Promptfoo history start empty. Prepare a new
reviewed dataset before live inference. Source code and reusable test fixtures
remain available; archived runs are never imported automatically.

## Prepare and run a benchmark

### Define experiments with configuration files

Experiments live under `experiments/`, separate from the shared runner and
production code. A JSON config defines `defaults`, a nonempty `variants` list,
and optionally a Python `provider` path relative to the config. Each variant
overrides the defaults. Give variants a `label`; mark the comparison baseline
with `baseline: true` and other variants with `baseline: false`.

For example, compare top-level examples against category-nested examples:

```bash
just experiment-run --config experiments/categorization/experiments/example-placement/experiment.json
```

The [example-placement experiment](experiments/example-placement/README.md)
contains its own config and payload transformation. New experiments need only a
config and, when necessary, a local provider exposing Promptfoo's `call_api`.
Providers can reuse `provider_response(..., transform=...)` for shared transport,
validation, scoring, traces, and token accounting. The transform receives only
the outgoing payload, never expected labels or review notes. No new CLI flag or
`justfile` recipe is needed per experiment.

The generic runner retains seeded scheduling, three live repetitions, reports,
and viewer imports. Run directory names include a configuration hash to keep
variants distinct. The original config and provider source are saved alongside
the frozen dataset and manifest. Python providers are trusted local code; review
them before running. Sharing and telemetry remain disabled. Without `--config`,
the existing single-variant pilot is unchanged. Production requests are unaffected.

Prepare a private benchmark:

```bash
just experiment-export
# Open data/experiments/review.csv in a spreadsheet.
# Review every expected_category against the saved category definitions in
# data/experiments/review.snapshot.json; enter reviewed=yes on every row.
just experiment-prepare 'Your review identifier'
```

Export uses a read-only SQLite connection. It selects up to 50 distinct cleaned
descriptions, first ensuring at least one row from every non-archived category
with an active transaction assignment, then sampling across category labels,
repeats, and conflicts. Categories without assigned transactions are reported.
If `--limit` is too small for category coverage, export fails before writing files.
Each cleaned description contributes exactly one row. Current manual or protected
choices take precedence over AI assignments, including choices retained by undo or
category merges. All distinct manually chosen descriptions are included even when
this exceeds `--limit`. Repeated manual choices for the same description collapse
to one representative. Category coverage uses different descriptions where possible;
categories that cannot be represented without duplicating a description are reported.
Representative ties use transaction ID order. Superseded choices remain in history.
Confirmed examples with an exported description are excluded to prevent duplicate
manual choices from leaking the expected label into inference.
The sample is intentionally diagnostic,
not a prevalence-weighted estimate of all spending. Every expected label needs
review, including any candidate copied from an existing assignment. Check the
exported signed amounts and booking dates too. Use exact
saved category names; retain the original row IDs. Reviewed description edits are
frozen into the new dataset. Notes stay local
and never enter inference. A new export requires a new output path.

### Supply teaching examples in the review

The review CSV includes a `role` column. Leave it as `test` for rows to score, or
change it to `example` to teach the model that description's category:

| role | description | expected_category | reviewed |
|---|---|---|---|
| example | Example Insurance | Housing | yes |
| test | Example Supermarket | Groceries | yes |

Use your actual saved category names. Both roles require `reviewed=yes` and a
reviewed `expected_category`. Missing or blank roles mean `test`, so older review
files still work. Roles accept surrounding spaces and either letter case. Keep
at least one test row. Retain each original row ID. Reviewed descriptions are used as entered.

You can also append rows with `role=example`. These may copy a test merchant
and its ID, or leave the ID blank; preparation assigns a separate, deterministic
example ID. Enter the example description, saved category, and `reviewed=yes`.
Amounts and dates are optional for appended examples because they are not sent.
The original test row remains scored. Example descriptions are used exactly as
written; remove any temporary suffixes you do not want the model to see.

Preparation moves example rows into the dataset's `examples` list and records
`review_example_ids`. They are supplied as description/category guidance in
every batch and repetition, and never appear in the scored comparison. Amounts,
dates, and review notes are not included in teaching examples. Every explicitly
selected example is sent, plus up to 40 retrieved existing confirmed examples;
selected examples cannot be dropped by retrieval. Choosing an example reduces
the number of scored transactions, so compare variants using the same frozen split.
Prepared example IDs must be unique and separate from test IDs. Example and test
descriptions may match: this measures classification with known merchant guidance,
rather than prediction on unseen merchants.

Preparation prints the counts of test rows, selected review examples, and
existing examples. When changing an already prepared review, use a new output
path so earlier frozen datasets and experiment results remain reproducible:

```bash
just experiment-prepare 'Your review identifier' --output data/experiments/dataset-with-examples.json
just experiment-run --dataset data/experiments/dataset-with-examples.json
```

Preparation freezes the test/example split and example selection into a hashed
dataset. Existing datasets without `review_example_ids` retain their retrieval
behavior. Automatically exported confirmed examples exclude exported cleaned
descriptions; explicit review examples become available after those rows leave
the test set. Similar descriptions in examples are retained intentionally to
measure prediction under the application's guidance; the same example set is
used for all configurations. The review export contains no identical descriptions.

After review, sign in through the dashboard (`just dev`) if needed, then:

```bash
just experiment-run
just experiment-view
# Optional: regenerate reports from an existing output directory.
just experiment-report data/experiments/pilot-<timestamp>
```

The pilot checks account availability of `gpt-6.1-sol`, then runs one
configuration with three repetitions:

- **Amount:** cleaned description and signed integer `amount_cents`.

The current configuration records `category_format: name-only-nested-v2` in saved
metadata and includes it in run filenames to distinguish it from earlier runs.
Category objects contain `name`, `definition`, `kind`, and `confirmed_examples`;
they have no `id`. Each category's example list contains `example_id` and
`description`, with an empty list when no examples apply. There is no top-level
example list or redundant category label on individual examples.
Model responses use `category_name`, with exact saved
names enforced by the output schema and local validation. Predictions are mapped
back to stable category IDs for scoring and storage. Dataset files retain IDs.
The prompt, schema, and reference conversion are shared with production; production
retrieves examples per batch, while this benchmark fixes examples for the full sample.
Named requests always nest examples, including configurations carrying the older
`name-only-v1` marker. Historical reports and saved request traces remain readable
and unchanged. The example-placement experiment alone flattens examples in its
local provider when explicitly comparing layouts. Older ID-field experiments
retain their separate legacy serialization.

Requests use category names, short transaction references (`t1`, `t2`, ...), and
individual rows, even when descriptions repeat. Positive amounts mean incoming;
negative amounts mean outgoing. Positive amounts default to a suitable saved income
category when no other purpose is known; evidence of transfers, refunds, or another
specific purpose takes precedence. No currency or transaction date fields are
sent. Examples retain their existing description/category guidance. Visible run
and column labels show only the model name, such as `gpt-6.1-sol`; internal
filenames remain unique for each repetition. Earlier matrix results remain readable.

Each of the three evaluations covers the entire frozen sample, in batches of at
most 25 transactions. With 40 reviewed rows, this is six inference requests.
Requests run sequentially in recorded, seeded configuration order, with fresh
responses rather than Promptfoo's response cache. Provider-side prompt caching
can still occur; raw usage is retained to make it visible.

Review exports include `amount_cents` (signed integer cents) and `booking_date`
(`YYYY-MM-DD`) in both `review.csv` and its source snapshot. Check these fields
alongside the expected category; preparation validates and freezes the values
from the CSV, including any corrections you make there. Neither preparation nor
experiment execution looks up transaction context in SQLite. The runner requires
both fields in its frozen dataset and stops before inference if either is missing
or invalid. Older datasets must be prepared again from a review CSV containing
these fields. Existing run artifacts remain unchanged.

The archived sample remains one row per description; a future sample with
repeated descriptions is needed to measure the value of context for duplicates.

The command prints the planned request count before inference. Each evaluation
can make multiple requests. This consumes the connected account's plan usage.
An unavailable model stops the pilot without substitution. Failed batches produce no
predictions for their rows, lower accuracy and coverage, and remain in reports.
An interrupted pilot keeps completed evaluations; use a new directory for another
pilot. Reports can also be generated from the completed portion.

Each run retains normalized per-row predictions, scores, errors, request bodies,
raw response text, and transport usage. The pilot manifest records the dataset
hash, Git revision, dirty-workspace flag, seed, variants, and request count.
`comparison.md` and `comparison.json` report accuracy over original rows,
accuracy averaged over distinct descriptions, coverage, failed/invalid batches,
tokens, latency, duplicate disagreement, conflicting labels, paired improvements
and regressions against the same-model/repetition amount-only baseline, and stability across
repetitions. Category breakdowns are in individual `result.json` artifacts.
Unknown token usage is shown as unavailable; dollar costs are not invented.

After a completed pilot, the viewer receives one **Categorization comparison**
evaluation with **all variants and repetitions**: one transaction per row and
one variant per column. The comparison supports any number of variants defined
in the Python matrix. Column labels show only the model name. Repeat counts and
configuration details remain in saved metadata; historical configurations with
the same model label receive a short identifier to keep their columns distinct.
Opening a column's prompt shows the original saved request bodies, including
instructions, input, and output schema, for every repetition and batch. Runs
without retained request traces show an empty request list; historical prompts
are never reconstructed from current code.

Each cell shows fractional accuracy, such as **2/3 correct (66.7%)**, the expected
category, and every repeat's prediction and explanation. The column's named
**accuracy** metric averages these scores across transactions and repetitions;
it equals the average run accuracy because every run covers the same sample.
For 40 transactions and three repetitions, 108 correct predictions out of 120
means 90% accuracy. **Pass** means every repetition for that transaction was
correct; the binary pass rate is not the mean accuracy. Use the named accuracy
metric to compare variants.

**Coverage** is the fraction of repetitions with a prediction. Missing predictions
count as incorrect and uncovered. **Consistency** measures whether every repeat
picked the same category, even if wrong; any missing prediction prevents
consistency. With one repetition, consistency is unavailable.
`comparison.md` and `comparison.json` include per-variant repetition counts, mean
accuracy and its range across runs, coverage, consistency, mean run duration,
and mean token usage, alongside the individual run summaries. Incomplete pilots
are labeled using the manifest; averages use available results and show actual
repeat counts. Unknown token usage remains unavailable.

The variables show the description, expected category, and available reviewed
amount/date context. Shared table context does not mean every variant received
all fields; source request artifacts record the exact inputs. The comparison
imports saved results without new inference or grading calls. Token usage and
latency remain at run/variant level and are not distributed across transactions.
Promptfoo may show zero default timing or cost fields on imported cells; those
are not measurements or dollar costs.

To create the aggregate view for an older or interrupted pilot:

```bash
just experiment-view-report data/experiments/pilot-<timestamp>
# Optional: most recent two variants, with ALL repetitions for each.
just experiment-view-report data/experiments/pilot-<timestamp> --latest 2
# Explicit alias for the default selection.
just experiment-view-report data/experiments/pilot-<timestamp> --all-runs
just experiment-view
```

The command prints the direct local evaluation URL and saves it in
`comparison-view.json`. Unlike the earlier run-level selection, `--latest N`
selects N variants by their most recent completed repetition, using evaluation
timestamps and falling back to result-file modification times. It never removes
repetitions from a selected variant. Reimporting uses the original frozen dataset
and predictions, creates a fresh comparison, and leaves earlier evaluations and
source artifacts available. Offline comparisons are labeled **OFFLINE smoke**.
Repetitions are not independent samples; this small diagnostic pilot should guide
a larger held-out evaluation before changing production behavior.

The latest retained live pilot, dated October 15, 2026, scored 41/41 correct
in each of three repetitions, with full coverage and consistency. It supplied
nine existing confirmed examples plus three explicitly reviewed teaching examples.
The preceding pilot with nine examples averaged 91.9% accuracy. These results
used the earlier wire format with redundant category `id` and `name` fields;
they are not live validation of `name-only-v1`. This small diagnostic sample does
not establish full-history accuracy. Benchmark teaching examples are not imported
into production automatically. The new format has offline validation; a future
live run can measure whether removing redundant fields affects accuracy.

To extend the experiment, add explicit factors to the Python matrix and provider
while retaining stable datasets and canonical output IDs. Follow-up suites can
compare example retrieval, batch sizes, extra transaction context, and other
models. The current configuration omits transaction dates; add new variant
labels when introducing new factors.

References: [Python provider](https://www.promptfoo.dev/docs/providers/python/),
[viewer](https://www.promptfoo.dev/docs/usage/web-ui/),
[response caching](https://www.promptfoo.dev/docs/configuration/caching/).

## Earlier standalone API experiment

Small, reproducible Python 3.14 project managed by `uv`, with exact direct dependency
versions and a committed lockfile for transitive dependencies. Uses the public Responses API
with a separately registered ChatGPT-plan OAuth connection. No API-key billing or
fallback to Codex/ChatGPT private backend routes.

## Run

```bash
cd experiments/categorization
uv sync --locked
uv run auth.py serve
```

Open the printed **Continue with ChatGPT** authorization URL and grant plan usage.
The callback listener binds only to `127.0.0.1:1456`. If your browser is on another
machine, forward that port before sign-in, or use a second terminal to run
`uv run auth.py complete` and paste the complete redirected URL into its hidden
prompt. State, nonce, PKCE and the ID-token signature/audience/issuer are validated.

Credentials and the stable host ID are saved with owner-only permissions in
`~/.fyntok-experiment/`, outside this repository. Override with
`FYNTOK_CREDENTIAL_DIR` if needed. Refresh runs sequentially in this single-process
experiment; do not run concurrent experiments sharing these credentials.
Disconnect this experiment in ChatGPT settings when its access is no longer needed.

```bash
uv run experiment.py --output results/my-comparison
uv run -m unittest -v
```

By default, model discovery selects the first displayed account model. Use
`--model SLUG` to choose another discovered model. Defaults to two repetitions
(four inference requests); use `--repeats 1` for one pair. The second repetition
reverses the condition order. No temperature override or server-stored conversation
history is used. A stream is successful only after `response.completed` and local
validation of complete, unique transaction IDs and valid category/example IDs.

Offline preparation, without credentials or network calls:

```bash
uv run experiment.py --prepare-only --output results/prepared
```

To test a different correction, copy a generated `fixture.json`, move one of its
target transactions into `examples` with an `example_id` and the desired
`category_id`, remove that transaction from the targets, and document who chose the
label in `example_provenance`. Then run:

```bash
uv run experiment.py --fixture path/to/fixture.json --output results/custom-correction
```

## Synthetic demonstration fixture

`fixtures/dm-personal-care.json` contains six invented transactions and one scripted personal-care example. All merchants, locations, dates, amounts, and row identifiers are synthetic. It has no source CSV fingerprint and does not represent a human-confirmed decision or a measured accuracy result.

Run the demonstration with:

```bash
uv run experiment.py --fixture fixtures/dm-personal-care.json --output ../../data/experiments/synthetic-care
```

This command uses the configured model and consumes plan usage. Keep generated fixtures, prompts, responses, and reports under ignored `data/`. Private historical live experiment artifacts are excluded from Git history.

## Official integration references

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
