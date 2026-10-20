"""Review roles split teaching inputs from scored experiment transactions."""

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from fyntok.experiment_view import comparison_export
from fyntok.experimentation import (
    OfflineClient,
    content_hash,
    evaluate,
    generate_report,
    load_dataset,
    prepare_review,
    variants,
    write_json,
)


def write_review(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def review(tmp_path: Path) -> Path:
    path = tmp_path / "review.csv"
    rows = [
        {
            "id": f"row-{index}",
            "description": description,
            "amount_cents": -100,
            "booking_date": "2026-01-15",
            "expected_category": "Shopping" if index == 0 else "Food",
            "role": "example" if index == 0 else "test",
            "reviewed": "yes",
            "notes": "Private review notes must not enter inference",
        }
        for index, description in enumerate(("Example insurer", "Market", "Bakery"))
    ]
    write_review(path, rows)
    write_json(
        path.with_suffix(".snapshot.json"),
        {
            "version": 1,
            "categories": [
                {"id": "food", "name": "Food", "definition": "Food", "kind": "spending"},
                {"id": "shop", "name": "Shopping", "definition": "Retail", "kind": "spending"},
            ],
            "cases": rows,
            "examples": [
                {"example_id": "old", "description": "Old merchant", "category_id": "food"}
            ],
        },
    )
    return path


def test_review_examples_are_inputs_and_never_scored(
    review: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "dataset.json"
    snapshot = review.with_suffix(".snapshot.json")
    original = (review.read_bytes(), snapshot.read_bytes())
    prepare_review(review, output, "Reviewer")
    dataset = load_dataset(output)
    assert dataset["review_example_ids"] == ["row-0"]
    assert [c["id"] for c in dataset["cases"]] == ["row-1", "row-2"]
    assert dataset["examples"] == [
        {"example_id": "old", "description": "Old merchant", "category_id": "food"},
        {"example_id": "row-0", "description": "Example insurer", "category_id": "shop"},
    ]
    assert "2 test rows, 1 review examples, 1 existing examples" in capsys.readouterr().out
    assert (review.read_bytes(), snapshot.read_bytes()) == original
    captured = []

    class Client:
        def request(
            self,
            model: str,
            payload: dict[str, Any],
            schema: dict[str, Any],
            instructions: str,
            *,
            operation: str = "request",
        ) -> dict[str, Any]:
            captured.append(payload)
            return OfflineClient().request(
                model, payload, schema, instructions, operation=operation
            )

    config = variants(("mock",))[0]
    result = evaluate(Client(), dataset, config)
    assert len(captured) == 1
    assert captured[0]["confirmed_examples"] == [
        {"example_id": "row-0", "description": "Example insurer", "category_name": "Shopping"},
        {"example_id": "old", "description": "Old merchant", "category_name": "Food"},
    ]
    assert [t["description"] for t in captured[0]["transactions"]] == ["Market", "Bakery"]
    assert all(
        set(t) == {"transaction_id", "description", "amount_cents"}
        for t in captured[0]["transactions"]
    )
    assert "Private review notes" not in json.dumps(captured)
    assert "expected_category" not in json.dumps(captured)
    assert result["metrics"]["accuracy"] == 1
    assert [r["id"] for r in result["rows"]] == ["row-1", "row-2"]
    result.update(tokenUsage=None, offline=True)
    write_json(tmp_path / "mock-r1/result.json", result)
    generate_report(tmp_path)
    exported = comparison_export(tmp_path)
    assert len(exported["config"]["tests"]) == 2
    assert {t["metadata"]["transaction_id"] for t in exported["config"]["tests"]} == {
        "row-1",
        "row-2",
    }


@pytest.mark.parametrize("role", [None, "", " TEST "])
def test_legacy_and_blank_roles_remain_tests(
    review: Path,
    tmp_path: Path,
    role: str | None,
) -> None:
    with review.open() as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        if role is None:
            del row["role"]
        else:
            row["role"] = role
    write_review(review, rows)
    output = tmp_path / "dataset.json"
    prepare_review(review, output, "Reviewer")
    dataset = load_dataset(output)
    assert len(dataset["cases"]) == 3
    assert dataset["review_example_ids"] == []
    assert len(dataset["examples"]) == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("invalid_role", "role must be"),
        ("unreviewed_example", "reviewed=yes"),
        ("unknown_category", "exact saved category"),
        ("all_examples", "at least one test"),
    ],
)
def test_invalid_example_reviews_fail_before_writing(
    review: Path,
    tmp_path: Path,
    change: str,
    message: str,
) -> None:
    with review.open() as stream:
        rows = list(csv.DictReader(stream))
    match change:
        case "invalid_role":
            rows[0]["role"] = "train"
        case "unreviewed_example":
            rows[0]["reviewed"] = ""
        case "unknown_category":
            rows[0]["expected_category"] = "Missing"
        case "all_examples":
            for row in rows:
                row["role"] = "example"
    write_review(review, rows)
    output = tmp_path / "dataset.json"
    with pytest.raises(ValueError, match=message):
        prepare_review(review, output, "Reviewer")
    assert not output.exists()


@pytest.mark.parametrize("invalid", ["missing", "duplicate_pin", "duplicate_example", "test_id"])
def test_frozen_datasets_validate_review_example_ids(
    review: Path,
    tmp_path: Path,
    invalid: str,
) -> None:
    output = tmp_path / "dataset.json"
    prepare_review(review, output, "Reviewer")
    dataset = load_dataset(output)
    match invalid:
        case "missing":
            dataset["review_example_ids"] = ["missing"]
        case "duplicate_pin":
            dataset["review_example_ids"] *= 2
        case "duplicate_example":
            dataset["examples"].append(dataset["examples"][0])
        case "test_id":
            dataset["examples"][-1]["example_id"] = "row-1"
    dataset["hash"] = content_hash({k: v for k, v in dataset.items() if k != "hash"})
    output.write_text(json.dumps(dataset))
    with pytest.raises(ValueError):
        load_dataset(output)


def test_all_selected_examples_reach_every_batch_and_repetition(
    review: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "dataset.json"
    prepare_review(review, output, "Reviewer")
    dataset = load_dataset(output)
    template = dataset["cases"][0]
    dataset["cases"] = [{**template, "id": f"test-{i}"} for i in range(26)]
    dataset["examples"] = [
        {
            "example_id": f"example-{i}",
            "description": f"Teaching merchant {i}",
            "category_id": "shop",
        }
        for i in range(45)
    ]
    dataset["review_example_ids"] = [e["example_id"] for e in dataset["examples"]]
    dataset["examples"].extend(
        {"example_id": f"old-{i}", "description": f"Market {i}", "category_id": "food"}
        for i in range(50)
    )
    captured = []

    class Client:
        def request(
            self,
            model: str,
            payload: dict[str, Any],
            schema: dict[str, Any],
            instructions: str,
            *,
            operation: str = "request",
        ) -> dict[str, Any]:
            captured.append(payload)
            return OfflineClient().request(
                model, payload, schema, instructions, operation=operation
            )

    for _ in range(3):
        evaluate(Client(), dataset, variants(("mock",))[0])
    assert len(captured) == 6
    for payload in captured:
        ids = [e["example_id"] for e in payload["confirmed_examples"]]
        assert set(dataset["review_example_ids"]) <= set(ids)
        assert len(ids) == len(set(ids)) == 85
    assert all(p["confirmed_examples"] == captured[0]["confirmed_examples"] for p in captured)


def test_review_example_selection_is_hashed(review: Path, tmp_path: Path) -> None:
    output = tmp_path / "dataset.json"
    prepare_review(review, output, "Reviewer")
    dataset = load_dataset(output)
    dataset["review_example_ids"] = []
    output.write_text(json.dumps(dataset))
    with pytest.raises(ValueError, match="hash changed"):
        load_dataset(output)


@pytest.mark.parametrize("copied_id", [True, False])
def test_appended_examples_can_teach_scored_merchants(
    review: Path,
    tmp_path: Path,
    copied_id: bool,
) -> None:
    with review.open() as stream:
        rows = list(csv.DictReader(stream))
    rows[2]["description"] = "Reviewed bakery text"
    added = {**rows[1], "role": "example", "amount_cents": "", "booking_date": ""}
    if not copied_id:
        added["id"] = ""
    rows.append(added)
    # Changing the added example text is also allowed, without changing the source test.
    rows.append({**rows[2], "role": "example", "description": "Bakery variation"})
    write_review(review, rows)
    output = tmp_path / "dataset.json"
    prepare_review(review, output, "Reviewer")
    dataset = load_dataset(output)
    assert [c["id"] for c in dataset["cases"]] == ["row-1", "row-2"]
    assert dataset["cases"][1]["description"] == "Reviewed bakery text"
    assert len(dataset["review_example_ids"]) == 3
    assert [e["description"] for e in dataset["examples"]][-2:] == ["Market", "Bakery variation"]
    assert not set(dataset["review_example_ids"]) & {c["id"] for c in dataset["cases"]}
    assert dataset["examples"][-2]["category_id"] == "food"
    # IDs are reproducible across preparation, irrespective of output path.
    second = tmp_path / "second.json"
    prepare_review(review, second, "Reviewer")
    assert load_dataset(second)["review_example_ids"] == dataset["review_example_ids"]


@pytest.mark.parametrize("change", ["extra_test", "duplicate_test", "missing_original"])
def test_test_row_integrity_is_preserved_with_appended_examples(
    review: Path,
    tmp_path: Path,
    change: str,
) -> None:
    with review.open() as stream:
        rows = list(csv.DictReader(stream))
    match change:
        case "extra_test":
            rows.append({**rows[1], "id": "new-test"})
        case "duplicate_test":
            rows.append(dict(rows[1]))
        case "missing_original":
            rows.pop()
    write_review(review, rows)
    with pytest.raises(ValueError):
        prepare_review(review, tmp_path / "dataset.json", "Reviewer")
