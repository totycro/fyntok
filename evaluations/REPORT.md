# Milestone 4 evaluation record

October 8, 2026. **Human-reviewed live accuracy evaluation: pending.**

`sample.json` contains seven synthetic held-out cases covering common merchants, unfamiliar descriptions, own-account and ambiguous transfers, and a marketplace correction with a proposed human preference different from a plausible general-retail baseline. Its labels are candidate labels, not confirmed user decisions. `reviewer` is intentionally null. No private CSV data or credentials are included.

The harness makes two requests per case: one without examples and one with up to 40 examples selected by the application's relevance function. It records predictions and reasons, expected labels, retrieved example IDs, per-group correct counts, observed baseline disagreements, and how many disagreements examples corrected. Fifty unrelated recent examples stress retrieval of older relevant examples. Case IDs are excluded from the example IDs to prevent direct held-out leakage. Similar descriptions across different transactions intentionally measure propagation of individual guidance.

Before running, a person must review all descriptions, definitions, expected categories, and example labels, then replace `reviewer` with their name or review identifier. Add more cases to reflect actual banking conventions, retaining distinct transaction/example IDs. Sign in using the application's ChatGPT controls and use an account-supported model:

```bash
uv run --locked python -m fyntok.evaluation \
  --sample evaluations/sample.json --model <account-supported-model> \
  --output data/evaluation-results.json
```

This command explicitly sends the sample to the selected ChatGPT-plan model and consumes plan usage. It does not open or modify the transaction database. Store outputs locally under ignored `data/`, review the errors, and record the model, reviewer, sample, and result counts here. Repeat for consistency; a seven-case evaluation alone cannot establish general accuracy.

Automated evidence: `tests/test_evaluation.py` verifies paired scoring, older relevant-example retrieval despite 50 distractors, held-out checks, and refusal to request inference without recorded review. A scripted advisor supplies a wrong baseline marketplace assignment and a corrected guided assignment. Those scripted outputs test the harness and are **not model accuracy results**. Existing acceptance stories verify correction propagation, updated examples, and new imports independently.

Remaining acceptance work: confirm labels with a human reviewer; run an account-supported model; inspect common/unfamiliar/transfer accuracy; identify an actual baseline disagreement and measure whether retrieved guidance changes it; record live results and limitations. No new live inference or human review occurred in this implementation. Free-text minimization remains heuristic, and relevance ranking uses word overlap with recent fallback rather than semantic retrieval.
