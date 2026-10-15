"""Import saved classifications as transaction rows in the local Promptfoo viewer."""

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def run_label(name: str, config: dict[str, Any]) -> str:
    model = config["model"].removeprefix("gpt-")
    repetition = name.rsplit("-r", 1)[-1]
    if "booking_date" in config:
        detail = "amount + date" if config["booking_date"] else "amount only"
    else:
        detail = ", ".join(
            (
                "category names" if config["category_names"] else "category UUIDs",
                "short refs" if config["short_refs"] else "transaction UUIDs",
                "grouped" if config["grouped"] else "individual",
            )
        )
    return f"{model} | {detail} | repeat {repetition}"


def completed_at(path: Path) -> float:
    export_path = path.parent.parent / f"{path.parent.name}.eval.json"
    if export_path.exists():
        timestamp = json.loads(export_path.read_text())["results"]["timestamp"]
        return datetime.fromisoformat(timestamp).timestamp()
    return path.stat().st_mtime


def comparison_export(directory: Path, *, latest: int | None = 2) -> dict[str, Any]:
    """Reshape existing evidence; never infer, relabel, or redistribute batch usage."""
    dataset = json.loads((directory / "dataset.json").read_text())
    categories = {c["id"]: c["name"] for c in dataset["categories"]}
    cases = dataset["cases"]
    paths = sorted(directory.glob("*/result.json"))
    if not paths:
        raise ValueError("No completed experiment results found.")
    if latest is not None:
        if latest < 1:
            raise ValueError("Choose at least one latest run.")
        paths = sorted(paths, key=lambda path: (completed_at(path), path.parent.name))[-latest:]
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
    offline_flags = set()
    saved_runs = []
    for prompt_index, path in enumerate(paths):
        saved = json.loads(path.read_text())
        rows = {row["id"]: row for row in saved["rows"]}
        if (
            saved["dataset_hash"] != dataset["hash"]
            or len(rows) != len(saved["rows"])
            or set(rows) != {c["id"] for c in cases}
            or any(rows[c["id"]]["expected"] != c["expected_category_id"] for c in cases)
        ):
            raise ValueError(f"Saved run does not match its frozen dataset: {path.parent.name}")
        offline_flags.add(saved["offline"])
        saved_runs.append(
            {
                "name": path.parent.name,
                "source_result": str(path.resolve()),
                "metrics": saved["metrics"],
                "token_usage": saved["tokenUsage"],
            }
        )
        label = run_label(path.parent.name, saved["config"])
        prompt_id = hashlib.sha256(label.encode()).hexdigest()
        provider = {"id": f"saved:{path.parent.name}", "label": label}
        prompt = {"raw": label, "label": label}
        correct_count = 0
        covered_count = 0
        for test_index, case in enumerate(cases):
            row = rows[case["id"]]
            prediction = row["prediction"]
            covered = prediction is not None
            correct = covered and prediction["category_id"] == row["expected"]
            correct_count += correct
            covered_count += covered
            predicted = categories[prediction["category_id"]] if covered else "NO PREDICTION"
            reason = (
                prediction["reason"]
                if covered
                else "; ".join(error["message"] for error in saved["errors"])
                or "The saved run produced no prediction for this transaction."
            )
            grading = {
                "pass": correct,
                "score": int(correct),
                "reason": f"Expected: {categories[row['expected']]}. Predicted: {predicted}.",
                "namedScores": {"accuracy": int(correct), "coverage": int(covered)},
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
                    "response": {"output": f"{predicted}\n\n{reason}"},
                    "success": correct,
                    "score": int(correct),
                    "failureReason": 0 if correct else 1,
                    "gradingResult": grading,
                    "namedScores": grading["namedScores"],
                    "metadata": {
                        "source_result": str(path.resolve()),
                        "transaction_id": case["id"],
                        "offline": saved["offline"],
                        "timing_note": "Latency and token usage belong to the full saved run.",
                    },
                }
            )
        prompts.append(
            {
                "id": prompt_id,
                **prompt,
                "provider": provider["id"],
                "metrics": {
                    "score": correct_count,
                    "testPassCount": correct_count,
                    "testFailCount": len(cases) - correct_count,
                    "testErrorCount": 0,
                    "assertPassCount": correct_count,
                    "assertFailCount": len(cases) - correct_count,
                    "totalLatencyMs": round(saved["metrics"]["latency_seconds"] * 1000),
                    **({"tokenUsage": saved["tokenUsage"]} if saved["tokenUsage"] else {}),
                    "namedScores": {"accuracy": correct_count, "coverage": covered_count},
                    "namedScoresCount": {"accuracy": len(cases), "coverage": len(cases)},
                    "namedScoreWeights": {"accuracy": 1, "coverage": 1},
                },
            }
        )
    if len(offline_flags) != 1:
        raise ValueError("Do not combine offline fixtures and live model evidence.")
    title = (
        f"{'OFFLINE smoke | ' if True in offline_flags else ''}Categorization comparison | "
        f"{directory.name.removeprefix('pilot-')[:8]} | {len(cases)} transactions | "
        f"{len(paths)} runs | {dataset['hash'][:8]}"
    )
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
            },
        },
    }


def import_comparison_view(directory: Path, *, latest: int | None = 2) -> str:
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
