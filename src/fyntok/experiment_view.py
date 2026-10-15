"""Import saved classifications as transaction rows in the local Promptfoo viewer."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .experiment_aggregation import aggregate_variants, missing_variants, variant_label


def run_label(name: str, config: dict[str, Any]) -> str:
    del name
    return variant_label(config)


def completed_at(path: Path) -> float:
    export_path = path.parent.parent / f"{path.parent.name}.eval.json"
    if export_path.exists():
        timestamp = json.loads(export_path.read_text())["results"]["timestamp"]
        return datetime.fromisoformat(timestamp).timestamp()
    return path.stat().st_mtime


def comparison_export(directory: Path, *, latest: int | None = None) -> dict[str, Any]:
    """Export one column per variant, averaging all its saved repetitions."""
    dataset = json.loads((directory / "dataset.json").read_text())
    categories = {c["id"]: c["name"] for c in dataset["categories"]}
    cases = dataset["cases"]
    aggregates = aggregate_variants(directory)
    missing = missing_variants(directory, aggregates)
    incomplete = missing > 0 or any(a["incomplete"] for a in aggregates)
    if latest is not None:
        if latest < 1:
            raise ValueError("Choose at least one latest variant.")
        aggregates = sorted(
            aggregates,
            key=lambda a: (max(completed_at(r["path"]) for r in a["runs"]), a["id"]),
        )[-latest:]
    tests = [
        {
            "description": case["description"],
            "vars": {
                "description": case["description"],
                "expected_category": categories[case["expected_category_id"]],
                **({"amount_cents": case["amount_cents"]} if "amount_cents" in case else {}),
                **({"booking_date": case["booking_date"]} if "booking_date" in case else {}),
            },
            "metadata": {"transaction_id": case["id"]},
        }
        for case in cases
    ]
    prompts = []
    results = []
    saved_runs = []
    summaries = []
    for prompt_index, aggregate in enumerate(aggregates):
        runs = aggregate["runs"]
        count = aggregate["repetitions"]
        label = aggregate["label"]
        prompt_id = aggregate["id"]
        provider = {"id": f"saved:{prompt_id}", "label": label}
        prompt = {"raw": label, "label": label}
        correct_count = 0
        totals: dict[str, float] = {"accuracy": 0, "coverage": 0}
        if count > 1:
            totals["consistency"] = 0
        for run in runs:
            saved_runs.append(
                {
                    "name": run["name"],
                    "source_result": str(run["path"].resolve()),
                    "metrics": run["saved"]["metrics"],
                    "token_usage": run["saved"]["tokenUsage"],
                }
            )
        summaries.append({k: v for k, v in aggregate.items() if k not in {"runs", "cells"}})
        for test_index, (case, cell) in enumerate(zip(cases, aggregate["cells"], strict=True)):
            score = cell["accuracy"]
            correct = score == 1
            correct_count += correct
            scores = {k: cell[k] for k in totals}
            for metric, value in scores.items():
                totals[metric] += value
            reason = (
                f"{round(score * count)}/{count} correct ({score:.1%}). "
                f"Expected: {categories[case['expected_category_id']]}. "
                "Pass means every repetition was correct."
            )
            lines = [reason]
            repeat_details = []
            for run in runs:
                prediction = run["rows"][case["id"]]["prediction"]
                predicted = categories[prediction["category_id"]] if prediction else "NO PREDICTION"
                explanation = (
                    prediction["reason"]
                    if prediction
                    else (
                        "; ".join(e["message"] for e in run["saved"]["errors"])
                        or "The saved run produced no prediction for this transaction."
                    )
                )
                repetition = run["name"].rsplit("-r", 1)[-1]
                lines.append(f"Repeat {repetition}: {predicted}\n{explanation}")
                repeat_details.append(
                    {
                        "run": run["name"],
                        "prediction": prediction,
                        "source_result": str(run["path"].resolve()),
                    }
                )
            grading = {
                "pass": correct,
                "score": score,
                "reason": reason,
                "namedScores": scores,
                "namedScoreWeights": dict.fromkeys(scores, 1),
            }
            results.append(
                {
                    "promptIdx": prompt_index,
                    "testIdx": test_index,
                    "promptId": prompt_id,
                    "provider": provider,
                    "prompt": prompt,
                    "testCase": tests[test_index],
                    "vars": tests[test_index]["vars"],
                    "response": {"output": "\n\n".join(lines)},
                    "success": correct,
                    "score": score,
                    "failureReason": 0 if correct else 1,
                    "gradingResult": grading,
                    "namedScores": scores,
                    "namedScoreWeights": grading["namedScoreWeights"],
                    "metadata": {
                        "transaction_id": case["id"],
                        "offline": aggregate["offline"],
                        "repetitions": repeat_details,
                        "consistency": cell["consistency"],
                        "timing_note": "Latency and token usage belong to complete saved runs.",
                    },
                }
            )
        usage = [r["saved"]["tokenUsage"] for r in runs]
        tokens = (
            {k: sum(u[k] for u in usage) for k in usage[0]}
            if all(u is not None for u in usage)
            else None
        )
        prompts.append(
            {
                "id": prompt_id,
                **prompt,
                "provider": provider["id"],
                "metrics": {
                    "score": totals["accuracy"],
                    "testPassCount": correct_count,
                    "testFailCount": len(cases) - correct_count,
                    "testErrorCount": 0,
                    "assertPassCount": correct_count,
                    "assertFailCount": len(cases) - correct_count,
                    "totalLatencyMs": round(
                        sum(r["saved"]["metrics"]["latency_seconds"] for r in runs) * 1000
                    ),
                    **({"tokenUsage": tokens} if tokens is not None else {}),
                    "namedScores": totals,
                    "namedScoresCount": dict.fromkeys(totals, len(cases)),
                    "namedScoreWeights": dict.fromkeys(totals, len(cases)),
                },
            }
        )
    title = (
        f"{'OFFLINE smoke | ' if aggregates[0]['offline'] else ''}Categorization comparison | "
        f"{directory.name.removeprefix('pilot-')[:8]} | {len(cases)} transactions | "
        f"{len(aggregates)} variants | {len(saved_runs)} runs | {dataset['hash'][:8]}"
    )
    if incomplete:
        title += f" | INCOMPLETE ({missing} variants without results)"
    return {
        "evalId": f"eval-categorization-{uuid4().hex[:12]}",
        "results": {
            "version": 3,
            "timestamp": datetime.now(UTC).isoformat(),
            "prompts": prompts,
            "results": results,
        },
        "config": {
            "description": title,
            "tests": tests,
            "sharing": False,
            "metadata": {
                "source_directory": str(directory.resolve()),
                "saved_results_only": True,
                "latest": latest,
                "saved_runs": saved_runs,
                "variants": summaries,
                "incomplete": incomplete,
                "missing_variants": missing,
            },
        },
    }


def import_comparison_view(directory: Path, *, latest: int | None = None) -> str:
    export = comparison_export(directory, latest=latest)
    path = directory / "comparison.eval.json"
    path.write_text(json.dumps(export, ensure_ascii=False, indent=2) + "\n")
    root = Path(__file__).resolve().parents[2]
    process = subprocess.run(  # noqa: S603 - Fixed pinned CLI, local derived export, no shell.
        [str(root / "experiments/categorization/node_modules/.bin/promptfoo"), "import", str(path)],
        cwd=root,
        env={
            **os.environ,
            "PROMPTFOO_CONFIG_DIR": str(root / "data/experiments/promptfoo"),
            "PROMPTFOO_DISABLE_TELEMETRY": "1",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    if process.stdout:
        print(process.stdout.strip())
    url = f"http://localhost:15500/eval/{export['evalId']}"
    (directory / "comparison-view.json").write_text(
        json.dumps({"eval_id": export["evalId"], "url": url}, indent=2) + "\n"
    )
    print(f"Transaction comparison: {url}")
    return url
