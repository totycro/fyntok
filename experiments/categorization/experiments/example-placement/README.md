# Example placement

Hypothesis: grouping human-confirmed examples with category definitions might
improve classification compared with a separate top-level example list.

Run from the repository root with the existing reviewed dataset:

```bash
just experiment-run --config experiments/categorization/experiments/example-placement/experiment.json
```

`experiment.json` defines both variants and the flat baseline. `provider.py`
contains only this experiment's input transformation: flattening the production
nested payload for the flat variant. The shared runner handles
dataset validation, transport, scoring, three repetitions, reports, and importing
the comparison into Promptfoo. No new CLI flag or recipe is needed for a new
experiment: add a config, and optionally a local provider for custom transformations.

Use `--dataset` and `--output` to select a frozen dataset and a fresh output
directory. Add `--offline --dataset experiments/categorization/smoke-fixture.json`
to verify integration without model requests. Offline scores are scripted, not
accuracy evidence.

The November 3 live comparison used 41 reviewed transactions and 12 examples.
Flat scored 121/123 across three repetitions; nested scored 120/123. All errors
involved one distinct transaction. Nesting saved 1.85% of input tokens without
demonstrating an accuracy benefit. These repetitions are not independent samples.
Existing evidence remains under ignored
`data/experiments/pilot-example-layouts-20261103/` and in Promptfoo history.
