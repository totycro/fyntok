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
Each description contributes up to three rows, or more when needed to retain
every conflicting category label. The sample is intentionally diagnostic,
not a prevalence-weighted estimate of all spending. Every expected label needs
review, including any candidate copied from an existing assignment. Use exact
saved category names; leave descriptions and row IDs unchanged. Notes stay local
and never enter inference. A new export requires a new output path.

Preparation freezes a hashed dataset. Confirmed examples exclude the selected
transaction IDs. Similar descriptions in examples are retained intentionally to
measure prediction under the application's guidance; the same retrieved example
set is used for all configurations. Identical descriptions can have different
reviewed labels, and those conflicts remain in the benchmark.

After review, sign in through the dashboard (`just dev`) if needed, then:

```bash
just experiment-run
just experiment-view
# Optional: regenerate reports from an existing output directory.
just experiment-report data/experiments/pilot-<timestamp>
```

The pilot checks account availability of `gpt-6.1-sol` and `gpt-6-luna`, then runs
all eight combinations of category UUIDs/names, transaction UUIDs/short references,
and individual rows/grouped descriptions, with three repetitions on each model.
Each of the 48 evaluations covers the entire frozen sample. Batches contain at
most 25 targets; grouping occurs across the sample before batching. Consequently,
grouping also changes batch composition, as it would in the proposed application
behavior. Requests run sequentially in recorded, seeded configuration order, with
fresh responses rather than Promptfoo's response cache. Provider-side prompt
caching can still occur; raw usage is retained to make it visible.

The command prints the planned request count before inference. Each evaluation
can make multiple requests. This consumes the connected account's plan usage.
Missing models stop the pilot without substitution. Failed batches produce no
predictions for their rows, lower accuracy and coverage, and remain in reports.
An interrupted pilot keeps completed evaluations; use a new directory for another
pilot. Reports can also be generated from the completed portion.

Each run retains normalized per-row predictions, scores, errors, request bodies,
raw response text, and transport usage. The pilot manifest records the dataset
hash, Git revision, dirty-workspace flag, seed, variants, and request count.
`comparison.md` and `comparison.json` report accuracy over original rows,
accuracy averaged over distinct descriptions, coverage, failed/invalid batches,
tokens, latency, duplicate disagreement, conflicting labels, paired improvements
and regressions against the same-model/repetition baseline, and stability across
repetitions. Category breakdowns are in individual `result.json` artifacts.
Unknown token usage is shown as unavailable; dollar costs are not invented.

The viewer shows each configuration/repetition as a named evaluation. Its single
dataset-level cell contains every prediction; use its details view or the saved
per-row JSON to investigate errors. Pass/fail means every row was correct;
accuracy scores provide the useful comparison even when a run fails that strict
criterion. Repetitions and duplicated rows are not independent samples, and this
small diagnostic pilot should guide a larger held-out evaluation before changing
production behavior.

To extend the experiment, add explicit factors to the Python matrix and provider
while retaining stable datasets and canonical output IDs. Follow-up suites can
compare example retrieval, batch sizes, extra transaction context, and other
models. Keep the first pilot's three factors isolated.

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
