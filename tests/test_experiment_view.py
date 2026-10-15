"""Saved-result views preserve predictions while exposing transaction accuracy."""

import json
from pathlib import Path
from typing import Any

import pytest

from fyntok.experiment_view import comparison_export, run_label
from fyntok.experimentation import OfflineClient, evaluate, variants, write_json


@pytest.fixture
def saved_pilot(tmp_path: Path) -> Path:
    dataset: dict[str, Any] = {
        "hash": "frozen-hash",
        "categories": [
            {"id": "food", "name": "Food"},
            {"id": "shop", "name": "Shopping"},
        ],
        "cases": [
            {
                "id": "one",
                "description": "Same merchant",
                "expected_category_id": "food",
                "amount_cents": -100,
                "booking_date": "2026-01-15",
            },
            {
                "id": "two",
                "description": "Same merchant",
                "expected_category_id": "shop",
                "amount_cents": 200,
                "booking_date": "2026-01-16",
            },
        ],
        "examples": [],
    }
    write_json(tmp_path / "dataset.json", dataset)
    for index, variant in enumerate(variants(("mock",))):
        result = evaluate(OfflineClient(), dataset, variant)
        result.update(offline=True, tokenUsage={"total": 120, "prompt": 100, "completion": 20})
        if index:
            result["rows"][1]["prediction"] = None
            result["errors"] = [{"message": "Stream interrupted."}]
        write_json(tmp_path / f"mock-amount1-date{index}-r1/result.json", result)
    return tmp_path


def test_comparison_has_transaction_rows_and_readable_columns(saved_pilot: Path) -> None:
    original = {p: p.read_bytes() for p in saved_pilot.rglob("*.json")}
    exported = comparison_export(saved_pilot)
    assert len(exported["config"]["tests"]) == 2
    assert len(exported["results"]["prompts"]) == 2
    assert len(exported["results"]["results"]) == 4
    assert exported["config"]["description"].startswith("OFFLINE smoke | Categorization comparison")
    prompts = exported["results"]["prompts"]
    assert prompts[0]["label"] == "mock | amount only | repeat 1"
    assert prompts[1]["label"] == "mock | amount + date | repeat 1"
    for prompt in prompts:
        assert prompt["metrics"]["testPassCount"] == 1
        assert prompt["metrics"]["testFailCount"] == 1
        assert prompt["metrics"]["tokenUsage"]["total"] == 120
    cells = exported["results"]["results"]
    assert [c["success"] for c in cells] == [True, False, True, False]
    assert "Food" in cells[0]["response"]["output"]
    assert cells[1]["gradingResult"]["reason"] == "Expected: Shopping. Predicted: Food."
    assert "NO PREDICTION" in cells[3]["response"]["output"]
    assert "Stream interrupted." in cells[3]["response"]["output"]
    assert cells[3]["namedScores"] == {"accuracy": 0, "coverage": 0}
    assert all("tokenUsage" not in cell and "latencyMs" not in cell for cell in cells)
    assert {p: p.read_bytes() for p in original} == original


@pytest.mark.parametrize("mismatch", ["hash", "row_id", "expected", "duplicate", "offline"])
def test_comparison_rejects_incompatible_evidence(saved_pilot: Path, mismatch: str) -> None:
    path = saved_pilot / "mock-amount1-date1-r1/result.json"
    saved = json.loads(path.read_text())
    match mismatch:
        case "hash":
            saved["dataset_hash"] = "different"
        case "row_id":
            saved["rows"][0]["id"] = "different"
        case "expected":
            saved["rows"][0]["expected"] = "shop"
        case "duplicate":
            saved["rows"].append(saved["rows"][0])
        case "offline":
            saved["offline"] = False
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        comparison_export(saved_pilot)


def test_legacy_labels_explain_original_factors() -> None:
    assert (
        run_label(
            "gpt-6.1-sol-names0-short0-group0-r2",
            {
                "model": "gpt-6.1-sol",
                "category_names": False,
                "short_refs": False,
                "grouped": False,
            },
        )
        == "6.1-sol | category UUIDs, transaction UUIDs, individual | repeat 2"
    )


def test_latest_runs_are_selected_before_building_cells(saved_pilot: Path) -> None:
    source = saved_pilot / "mock-amount1-date0-r1/result.json"
    for name, timestamp in (
        ("mock-amount1-date0-r1", "2026-01-15T10:00:00Z"),
        ("mock-amount1-date1-r1", "2026-01-15T11:00:00Z"),
        ("mock-amount1-date0-r2", "2026-01-15T12:00:00Z"),
    ):
        if name.endswith("r2"):
            write_json(saved_pilot / name / "result.json", json.loads(source.read_text()))
        write_json(saved_pilot / f"{name}.eval.json", {"results": {"timestamp": timestamp}})
    exported = comparison_export(saved_pilot)
    labels = [p["label"] for p in exported["results"]["prompts"]]
    assert labels == ["mock | amount + date | repeat 1", "mock | amount only | repeat 2"]
    assert len(exported["results"]["results"]) == 4
    assert len(comparison_export(saved_pilot, latest=1)["results"]["prompts"]) == 1
    assert len(comparison_export(saved_pilot, latest=None)["results"]["prompts"]) == 3
    assert len(exported["config"]["metadata"]["saved_runs"]) == 2
    assert all("run_metrics" not in c["metadata"] for c in exported["results"]["results"])
    with pytest.raises(ValueError, match="at least one"):
        comparison_export(saved_pilot, latest=0)
