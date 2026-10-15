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
saved category names; leave descriptions and row IDs unchanged. Notes stay local
and never enter inference. A new export requires a new output path.

Preparation freezes a hashed dataset. Confirmed examples exclude the selected
cleaned descriptions. Similar descriptions in examples are retained intentionally to
measure prediction under the application's guidance; the same retrieved example
set is used for all configurations. The review export contains no identical descriptions.

After review, sign in through the dashboard (`just dev`) if needed, then:

```bash
just experiment-run
just experiment-view
# Optional: regenerate reports from an existing output directory.
just experiment-report data/experiments/pilot-<timestamp>
```

The pilot checks account availability of `gpt-6.1-sol`, then compares exactly two
variants, with three repetitions each:

- **Amount:** cleaned description and signed integer `amount_cents`.
- **Amount + booking date:** the same fields plus `booking_date` (`YYYY-MM-DD`).

Both use category names, short transaction references (`t1`, `t2`, ...), and
individual rows, even when descriptions repeat. Positive amounts mean incoming;
negative amounts mean outgoing. No currency field is sent. Category definitions,
confirmed examples, instructions, batching, and model stay fixed; only the date
field differs. Examples retain their existing description/category guidance and
are identical between variants. Run labels end in `amount1-date0-r1` or
`amount1-date1-r1` (and repetitions 2/3). Earlier matrix results remain readable.

Each of the six evaluations covers the entire frozen sample, in batches of at
most 25 transactions. With 40 reviewed rows, this is 12 inference requests.
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

The existing sample remains one row per description; a future sample with
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
evaluation with the latest two completed runs: one transaction per row and one
configuration/repetition per column. Selection happens before importing results,
so the browser does not need to load or hide a large matrix.
Column labels explain the settings, for example `6.1-sol | amount + date | repeat 1`.
The variables show the description, expected category, and available reviewed
amount/date context. Each result cell shows the predicted category and reason;
pass/fail now means that transaction is correct/incorrect, so column pass rates
are transaction accuracy. Missing predictions fail and receive coverage=0.
Use the viewer's failure filter to investigate errors and its column controls to
focus on the variants being compared. Larger comparisons can make the UI slow;
prefer a small imported view. The evaluation title includes the pilot
date, transaction/run counts, and frozen dataset identifier.

The comparison imports saved results, making no new inference or grading calls.
Batch token usage and latency stay at run level; they are not distributed across
transactions. Full run metrics and source artifact paths are retained in cell
evaluation metadata and the Markdown/JSON reports. Promptfoo may show zero default timing
or cost fields on imported cells; these are not measurements or dollar costs.
Shared reviewed context shown in the table does not mean every variant received
all fields; the source requests record the exact model inputs.

To create this view for an older or interrupted pilot:

```bash
just experiment-view-report data/experiments/pilot-<timestamp> --latest 2
# Optional: include every saved run (can be slow in the browser).
just experiment-view-report data/experiments/pilot-<timestamp> --all-runs
just experiment-view
```

The command prints the direct local evaluation URL and saves it in
`comparison-view.json`. The default is `--latest 2`; recorded evaluation
timestamps determine recency, falling back to result-file modification times when
there is no evaluation export. It uses that pilot's original frozen dataset, preserving
its predictions and reviewed labels. Reimporting creates a fresh comparison;
earlier evaluations remain available. New runs keep their dataset-level execution
exports on disk and import only the transaction comparison into viewer history.
Offline smoke comparisons are explicitly labeled **OFFLINE smoke**.
Repetitions are not independent samples; this small diagnostic pilot should guide
a larger held-out evaluation before changing production behavior.

To extend the experiment, add explicit factors to the Python matrix and provider
while retaining stable datasets and canonical output IDs. Follow-up suites can
compare example retrieval, batch sizes, extra transaction context, and other
models. Keep the current comparison isolated to booking date.

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
