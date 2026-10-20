"""Saved-result views preserve predictions while exposing transaction accuracy."""

import json
from pathlib import Path
from typing import Any

import pytest

from fyntok.experiment_view import comparison_export, run_label
from fyntok.experimentation import OfflineClient, evaluate, write_json


def legacy_variants() -> list[dict[str, Any]]:
    """Frozen historical configurations remain readable after the matrix changes."""
    return [
        {
            "model": "mock",
            "category_names": True,
            "short_refs": True,
            "grouped": False,
            "booking_date": enabled,
        }
        for enabled in (False, True)
    ]


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
    for index, variant in enumerate(legacy_variants()):
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
    assert all(prompt["label"].startswith("mock [") for prompt in prompts)
    for prompt in prompts:
        assert prompt["metrics"]["testPassCount"] == 1
        assert prompt["metrics"]["testFailCount"] == 1
        assert prompt["metrics"]["tokenUsage"]["total"] == 120
    cells = exported["results"]["results"]
    assert [c["success"] for c in cells] == [True, False, True, False]
    assert "Food" in cells[0]["response"]["output"]
    assert "0/1 correct (0.0%). Expected: Shopping." in cells[1]["gradingResult"]["reason"]
    assert "NO PREDICTION" in cells[3]["response"]["output"]
    assert "Stream interrupted." in cells[3]["response"]["output"]
    assert cells[3]["namedScores"] == {"accuracy": 0, "coverage": 0}
    assert all("tokenUsage" not in cell and "latencyMs" not in cell for cell in cells)
    assert {p: p.read_bytes() for p in original} == original


def test_current_comparison_uses_model_only_label(saved_pilot: Path) -> None:
    (saved_pilot / "mock-amount1-date1-r1/result.json").unlink()
    exported = comparison_export(saved_pilot)
    assert [p["label"] for p in exported["results"]["prompts"]] == ["mock"]
    assert {r["provider"]["label"] for r in exported["results"]["results"]} == {"mock"}


def test_comparison_prompt_contains_original_requests(saved_pilot: Path) -> None:
    run = saved_pilot / "mock-amount1-date0-r1"
    bodies = [
        {
            "model": "mock",
            "instructions": "Historical instructions, unchanged by current code.",
            "input": [{"role": "user", "content": f"Original batch {batch}: Café"}],
            "text": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
        }
        for batch in range(2)
    ]
    for batch, body in enumerate(bodies):
        write_json(run / f"traces/batch-{batch}/request.json", body)
    exported = comparison_export(saved_pilot)
    prompt = exported["results"]["prompts"][0]
    assert json.loads(prompt["raw"])["runs"] == [{"run": run.name, "requests": bodies}]
    assert all(
        cell["prompt"]["raw"] == prompt["raw"] for cell in exported["results"]["results"][:2]
    )
    missing = json.loads(exported["results"]["prompts"][1]["raw"])
    assert missing["runs"][0]["requests"] == []
    assert "no request traces were retained" in missing["note"]


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


def test_run_labels_only_show_the_model() -> None:
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
        == "gpt-6.1-sol"
    )


def test_latest_variants_keep_all_their_repetitions(saved_pilot: Path) -> None:
    source = saved_pilot / "mock-amount1-date0-r1/result.json"
    for name, timestamp in (
        ("mock-amount1-date0-r1", "2026-01-15T10:00:00Z"),
        ("mock-amount1-date1-r1", "2026-01-15T11:00:00Z"),
        ("mock-amount1-date0-r2", "2026-01-15T12:00:00Z"),
    ):
        if name.endswith("r2"):
            write_json(saved_pilot / name / "result.json", json.loads(source.read_text()))
        write_json(saved_pilot / f"{name}.eval.json", {"results": {"timestamp": timestamp}})
    exported = comparison_export(saved_pilot, latest=1)
    labels = [p["label"] for p in exported["results"]["prompts"]]
    assert len(labels) == 1
    assert labels[0].startswith("mock [")
    assert len(exported["results"]["results"]) == 2
    assert len(comparison_export(saved_pilot)["results"]["prompts"]) == 2
    assert len(exported["config"]["metadata"]["saved_runs"]) == 2
    assert len(exported["results"]["results"][0]["metadata"]["repetitions"]) == 2
    with pytest.raises(ValueError, match="at least one"):
        comparison_export(saved_pilot, latest=0)


@pytest.fixture
def repeated_pilot(saved_pilot: Path) -> Path:
    # Variant 0: 2/6 correct; variant 1: 5/6 correct. Missing predictions are wrong.
    predictions = (
        (("food", "food"), ("food", "food"), ("shop", None)),
        (("food", "shop"), ("food", "shop"), ("food", "food")),
    )
    for variant_index, repeats in enumerate(predictions):
        source = saved_pilot / f"mock-amount1-date{variant_index}-r1/result.json"
        template = json.loads(source.read_text())
        source.unlink()
        for repetition, pair in enumerate(repeats, 1):
            result = json.loads(json.dumps(template))
            for row, category in zip(result["rows"], pair, strict=True):
                row["prediction"] = (
                    {"category_id": category, "reason": f"Reason from repeat {repetition}"}
                    if category is not None
                    else None
                )
            result["metrics"]["latency_seconds"] = repetition * 2
            result["tokenUsage"] = {
                "total": repetition * 120,
                "prompt": repetition * 100,
                "completion": repetition * 20,
            }
            write_json(
                saved_pilot / f"mock-amount1-date{variant_index}-r{repetition}/result.json", result
            )
    write_json(
        saved_pilot / "manifest.json",
        {
            "variants": legacy_variants(),
            "repetitions": 3,
            "offline": True,
        },
    )
    return saved_pilot


def test_fractional_accuracy_averages_repetitions_and_preserves_details(
    repeated_pilot: Path,
) -> None:
    from fyntok.experimentation import generate_report

    original = {p: p.read_bytes() for p in repeated_pilot.rglob("*.json")}
    exported = comparison_export(repeated_pilot)
    assert len(exported["results"]["prompts"]) == 2
    assert len(exported["results"]["results"]) == 4
    first, second = exported["results"]["prompts"]
    for prompt, accuracy in ((first, 1 / 3), (second, 5 / 6)):
        metrics = prompt["metrics"]
        assert metrics["namedScores"]["accuracy"] / metrics["namedScoreWeights"][
            "accuracy"
        ] == pytest.approx(accuracy)
        assert metrics["namedScoresCount"]["accuracy"] == 2
        assert metrics["tokenUsage"]["total"] == 720
        assert metrics["totalLatencyMs"] == 12000
        assert "repetition" not in prompt["label"]
    assert all(v["repetitions"] == 3 for v in exported["config"]["metadata"]["variants"])
    cells = exported["results"]["results"]
    assert cells[0]["score"] == pytest.approx(2 / 3)
    assert cells[0]["success"] is False
    assert "2/3 correct (66.7%)" in cells[0]["response"]["output"]
    assert all(f"Repeat {n}:" in cells[0]["response"]["output"] for n in (1, 2, 3))
    assert cells[1]["score"] == 0
    assert cells[1]["namedScores"]["coverage"] == pytest.approx(2 / 3)
    assert cells[1]["namedScores"]["consistency"] == 0
    assert cells[2]["success"] is True
    assert cells[2]["namedScores"]["consistency"] == 1
    assert cells[3]["score"] == pytest.approx(2 / 3)
    assert exported["config"]["metadata"]["incomplete"] is False
    generate_report(repeated_pilot)
    report = json.loads((repeated_pilot / "comparison.json").read_text())
    assert [a["accuracy"] for a in report["variants"]] == pytest.approx([1 / 3, 5 / 6])
    assert report["variants"][0]["accuracy_min"] == 0
    assert report["variants"][0]["accuracy_max"] == 0.5
    assert report["variants"][1]["consistency"] == 0.5
    assert report["variants"][0]["mean_latency_seconds"] == 4
    assert report["variants"][0]["mean_token_usage"]["total"] == 240
    assert "## Variant averages" in (repeated_pilot / "comparison.md").read_text()
    assert {p: p.read_bytes() for p in original} == original


def test_consistently_wrong_is_consistent_and_single_repeat_is_unavailable(
    saved_pilot: Path,
) -> None:
    source = saved_pilot / "mock-amount1-date0-r1/result.json"
    write_json(saved_pilot / "mock-amount1-date0-r2/result.json", json.loads(source.read_text()))
    exported = comparison_export(saved_pilot)
    wrong = exported["results"]["results"][1]
    assert wrong["score"] == 0
    assert wrong["namedScores"]["consistency"] == 1
    single = exported["results"]["results"][2]
    assert "consistency" not in single["namedScores"]
    assert single["metadata"]["consistency"] is None


def test_third_variant_and_incomplete_repetitions_are_explicit(repeated_pilot: Path) -> None:
    from fyntok.experimentation import generate_report

    source = repeated_pilot / "mock-amount1-date0-r1/result.json"
    saved = json.loads(source.read_text())
    saved["config"]["short_refs"] = False
    saved["tokenUsage"] = None
    write_json(repeated_pilot / "third-r1/result.json", saved)
    manifest_path = repeated_pilot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["variants"].append(saved["config"])
    manifest["variants"].append({**saved["config"], "grouped": True})
    manifest_path.write_text(json.dumps(manifest))
    exported = comparison_export(repeated_pilot)
    assert len(exported["results"]["prompts"]) == 3
    assert len(exported["results"]["results"]) == 6
    third = exported["results"]["prompts"][2]
    assert third["label"].startswith("mock [")
    assert exported["config"]["metadata"]["variants"][2]["incomplete"] is True
    assert "tokenUsage" not in third["metrics"]
    assert "INCOMPLETE" in exported["config"]["description"]
    assert exported["config"]["metadata"]["missing_variants"] == 1
    generate_report(repeated_pilot)
    report = json.loads((repeated_pilot / "comparison.json").read_text())
    assert report["incomplete"] is True
    assert report["variants"][2]["repetitions"] == 1
    assert report["variants"][2]["mean_token_usage"] is None
    assert report["variants"][2]["consistency"] is None


def test_legacy_manifest_and_unknown_usage_do_not_invent_measurements(repeated_pilot: Path) -> None:
    from fyntok.experiment_aggregation import aggregate_variants

    manifest_path = repeated_pilot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["repetitions"]
    manifest["offline"] = False
    manifest_path.write_text(json.dumps(manifest))
    source = repeated_pilot / "mock-amount1-date0-r2/result.json"
    saved = json.loads(source.read_text())
    saved["tokenUsage"] = None
    source.write_text(json.dumps(saved))
    aggregates = aggregate_variants(repeated_pilot)
    assert aggregates[0]["planned_repetitions"] == 3
    assert aggregates[0]["incomplete"] is False
    assert aggregates[0]["mean_token_usage"] is None


def test_grouping_uses_full_configuration_and_disambiguates_labels(saved_pilot: Path) -> None:
    source = saved_pilot / "mock-amount1-date0-r1/result.json"
    saved = json.loads(source.read_text())
    # Reordered keys remain the same variant.
    saved["config"] = dict(reversed(list(saved["config"].items())))
    write_json(saved_pilot / "mock-amount1-date0-r2/result.json", saved)
    # A different model must remain a separate column.
    saved["config"]["model"] = "gpt-mock"
    write_json(saved_pilot / "other-r1/result.json", saved)
    exported = comparison_export(saved_pilot)
    prompts = exported["results"]["prompts"]
    assert len(prompts) == 3
    assert len({p["label"] for p in prompts}) == 3
    assert len({p["id"] for p in prompts}) == 3
    assert exported["config"]["metadata"]["variants"][0]["repetitions"] == 2


@pytest.mark.parametrize(
    ("selection", "expected_latest"),
    [((), None), (("--all-runs",), None), (("--latest", "2"), 2)],
)
def test_view_command_defaults_to_all_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: tuple[str, ...],
    expected_latest: int | None,
) -> None:
    from fyntok import experimentation

    calls = []
    monkeypatch.setattr("sys.argv", ["experiment", "view-report", str(tmp_path), *selection])
    monkeypatch.setattr(
        experimentation,
        "import_comparison_view",
        lambda directory, *, latest: calls.append((directory, latest)),
    )
    experimentation.main()
    assert calls == [(tmp_path, expected_latest)]
