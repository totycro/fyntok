"""Shared, read-only aggregation of frozen experiment evidence."""

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


def configuration_key(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def variant_label(config: dict[str, Any]) -> str:
    if "example_layout" in config:
        return f"{config['model']} · {config['example_layout']} examples"
    return str(config["model"])


def aggregate_variants(directory: Path) -> list[dict[str, Any]]:
    """Validate evidence, then average repetitions without changing their predictions."""
    dataset = json.loads((directory / "dataset.json").read_text())
    cases = dataset["cases"]
    if not cases:
        raise ValueError("The frozen dataset has no transaction cases.")
    categories = {c["id"] for c in dataset["categories"]}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    offline_flags = set()
    for path in sorted(directory.glob("*/result.json")):
        saved = json.loads(path.read_text())
        rows = {row["id"]: row for row in saved["rows"]}
        if (
            saved["dataset_hash"] != dataset["hash"]
            or len(rows) != len(saved["rows"])
            or set(rows) != {c["id"] for c in cases}
            or any(rows[c["id"]]["expected"] != c["expected_category_id"] for c in cases)
            or any(
                r["prediction"] and r["prediction"]["category_id"] not in categories
                for r in rows.values()
            )
        ):
            raise ValueError(f"Saved run does not match its frozen dataset: {path.parent.name}")
        offline_flags.add(saved["offline"])
        groups[configuration_key(saved["config"])].append(
            {"name": path.parent.name, "path": path, "saved": saved, "rows": rows}
        )
    if not groups:
        raise ValueError("No completed experiment results found.")
    if len(offline_flags) != 1:
        raise ValueError("Do not combine offline fixtures and live model evidence.")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    aggregates: list[dict[str, Any]] = []
    for key, runs in groups.items():
        count = len(runs)
        config = runs[0]["saved"]["config"]
        cells: list[dict[str, Any]] = []
        for case in cases:
            predictions = [r["rows"][case["id"]]["prediction"] for r in runs]
            ids = [p["category_id"] if p else None for p in predictions]
            cells.append(
                {
                    "transaction_id": case["id"],
                    "accuracy": sum(cid == case["expected_category_id"] for cid in ids) / count,
                    "coverage": sum(cid is not None for cid in ids) / count,
                    "consistency": int(None not in ids and len(set(ids)) == 1)
                    if count > 1
                    else None,
                }
            )
        accuracies = [
            sum(
                bool(row["prediction"] and row["prediction"]["category_id"] == row["expected"])
                for row in run["rows"].values()
            )
            / len(cases)
            for run in runs
        ]
        usage = [run["saved"]["tokenUsage"] for run in runs]
        planned = None
        if manifest:
            planned = manifest.get("repetitions", 1 if manifest["offline"] else 3)
        label = variant_label(config)
        aggregates.append(
            {
                "id": hashlib.sha256(key.encode()).hexdigest(),
                "name": runs[0]["name"].rsplit("-r", 1)[0],
                "label": label,
                "config": config,
                "runs": runs,
                "cells": cells,
                "repetitions": count,
                "planned_repetitions": planned,
                "incomplete": planned is not None and count != planned,
                "accuracy": mean(accuracies),
                "accuracy_min": min(accuracies),
                "accuracy_max": max(accuracies),
                "coverage": mean(cell["coverage"] for cell in cells),
                "consistency": mean(cell["consistency"] for cell in cells) if count > 1 else None,
                "mean_latency_seconds": mean(
                    r["saved"]["metrics"]["latency_seconds"] for r in runs
                ),
                "mean_token_usage": {k: mean(u[k] for u in usage) for k in usage[0]}
                if all(u is not None for u in usage)
                else None,
                "offline": runs[0]["saved"]["offline"],
            }
        )
    # Labels and legacy report keys must never collapse distinct configurations.
    for aggregate in aggregates:
        for field in ("label", "name"):
            if sum(a[field] == aggregate[field] for a in aggregates) > 1:
                original = aggregate[field]
                for other in aggregates:
                    if other[field] == original:
                        other[field] += f" [{other['id'][:8]}]"
    return aggregates


def missing_variants(directory: Path, aggregates: list[dict[str, Any]]) -> int:
    path = directory / "manifest.json"
    if not path.exists():
        return 0
    manifest = json.loads(path.read_text())
    present = {configuration_key(a["config"]) for a in aggregates}
    return len({configuration_key(c) for c in manifest["variants"]} - present)
