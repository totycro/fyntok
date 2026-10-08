# Categorization API experiment

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
