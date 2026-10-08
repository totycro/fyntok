"""Summarize saved, paired Sol/Luna runs; Sol agreement is not ground-truth accuracy."""

import json
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
PAIRS = [
    ("supermarket", "initial-comparison", "luna-supermarket"),
    ("dm_personal_care", "dm-personal-care", "luna-dm-personal-care"),
]


def main():
    all_runs = {"sol": [], "luna": []}
    comparisons = []
    for scenario, sol_dir, luna_dir in PAIRS:
        directories = {"sol": HERE / "results" / sol_dir, "luna": HERE / "results" / luna_dir}
        fixtures = {name: json.loads((path / "fixture.json").read_text()) for name, path in directories.items()}
        if fixtures["sol"] != fixtures["luna"]:
            raise ValueError(f"Fixture mismatch for {scenario}")
        paired = {}
        for name, path in directories.items():
            runs = json.loads((path / "runs.json").read_text())
            all_runs[name].extend(runs)
            paired[name] = {(r["repeat"], r["condition"]): r for r in runs}
        if paired["sol"].keys() != paired["luna"].keys():
            raise ValueError(f"Incomplete paired runs for {scenario}")
        for (repeat, condition), sol_run in paired["sol"].items():
            # Check exact requests differ only in the model name.
            sol_request = json.loads((directories["sol"] / f"repeat-{repeat}-{condition}.request.json").read_text())
            luna_request = json.loads((directories["luna"] / f"repeat-{repeat}-{condition}.request.json").read_text())
            del sol_request["model"], luna_request["model"]
            if sol_request != luna_request:
                raise ValueError(f"Request mismatch for {scenario}/{repeat}/{condition}")
            luna_run = paired["luna"][(repeat, condition)]
            sol = {a["transaction_id"]: a for a in sol_run["result"]["classification"]["assignments"]}
            luna = {a["transaction_id"]: a for a in luna_run["result"]["classification"]["assignments"]}
            if sol.keys() != luna.keys():
                raise ValueError("Assignment ID mismatch")
            for tid in sol:
                comparisons.append({"scenario": scenario, "repeat": repeat, "condition": condition,
                                    "transaction_id": tid, "sol_category": sol[tid]["category_id"],
                                    "luna_category": luna[tid]["category_id"],
                                    "agrees": sol[tid]["category_id"] == luna[tid]["category_id"],
                                    "sol_example_ids_used": sol[tid]["example_ids_used"],
                                    "luna_example_ids_used": luna[tid]["example_ids_used"]})
    metrics = {}
    for name, runs in all_runs.items():
        metrics[name] = {"requests": len(runs), "completed": sum(r["result"]["status"] == "completed" for r in runs),
                         "mean_elapsed_seconds": round(mean(r["result"]["elapsed_seconds"] for r in runs), 2),
                         "min_elapsed_seconds": min(r["result"]["elapsed_seconds"] for r in runs),
                         "max_elapsed_seconds": max(r["result"]["elapsed_seconds"] for r in runs),
                         "input_tokens": sum(r["result"]["usage"]["input_tokens"] for r in runs),
                         "output_tokens": sum(r["result"]["usage"]["output_tokens"] for r in runs),
                         "total_tokens": sum(r["result"]["usage"]["total_tokens"] for r in runs)}
    summary = {"metrics": metrics, "compared_assignments": len(comparisons),
               "agreeing_assignments": sum(c["agrees"] for c in comparisons), "comparisons": comparisons}
    output = HERE / "results/model-comparison.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "comparisons"}, indent=2))


if __name__ == "__main__":
    main()
