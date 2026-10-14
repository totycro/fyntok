"""Benchmark isolation, scoring, dataset integrity, and reviewed private export."""

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any, override

import httpx
import pytest

from fyntok import experimentation
from fyntok.experimentation import (
    OfflineClient,
    content_hash,
    evaluate,
    export_review,
    generate_report,
    load_dataset,
    prepare_review,
    provider_response,
    run_pilot,
    variants,
    write_json,
)


@pytest.fixture
def sample() -> dict[str, Any]:
    dataset = {
        "version": 1,
        "reviewer": "Synthetic test expectations, not live accuracy evidence",
        "categories": [
            {"id": "c-food", "name": "Food", "definition": "Food purchases.", "kind": "spending"},
            {"id": "c-shop", "name": "Shopping", "definition": "Other retail.", "kind": "spending"},
        ],
        "cases": [
            {"id": "row-1", "description": "Market", "expected_category_id": "c-food"},
            {"id": "row-2", "description": "Market", "expected_category_id": "c-shop"},
            {"id": "row-3", "description": "Bakery", "expected_category_id": "c-food"},
        ],
        "examples": [{"example_id": "example-1", "description": "Bakery", "category_id": "c-food"}],
    }
    dataset["hash"] = content_hash(dataset)
    return dataset


class CapturingClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def request(
        self,
        model: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        instructions: str,
        *,
        operation: str = "request",
    ) -> dict[str, Any]:
        self.payloads.append(payload)
        return OfflineClient().request(model, payload, schema, instructions, operation=operation)


def test_factor_matrix_preserves_context_and_expands_conflicting_duplicates(
    sample: dict[str, Any],
) -> None:
    baseline_examples = None
    for config in variants(("mock",)):
        client = CapturingClient()
        result = evaluate(client, sample, config)
        payload = client.payloads[0]
        assert len(payload["transactions"]) == (2 if config["grouped"] else 3)
        assert len(result["rows"]) == 3
        assert result["metrics"]["accuracy"] == pytest.approx(2 / 3)
        assert result["metrics"]["description_accuracy"] == 0.75
        assert result["metrics"]["conflicting_label_groups"] == 1
        assert result["metrics"]["coverage"] == 1
        assert all(row["prediction"]["category_id"] == "c-food" for row in result["rows"])
        assert "expected" not in json.dumps(payload)
        assert "reviewer" not in payload
        expected_ref = "t1" if config["short_refs"] else "row-1"
        assert payload["transactions"][0]["transaction_id"] == expected_ref
        assert payload["confirmed_examples"][0]["category_id"] == (
            "Food" if config["category_names"] else "c-food"
        )
        example_ids = [e["example_id"] for e in payload["confirmed_examples"]]
        if baseline_examples is None:
            baseline_examples = example_ids
        assert example_ids == baseline_examples


@pytest.mark.parametrize("failure", ["missing", "duplicate", "unknown", "transport"])
def test_failed_batches_count_against_accuracy(sample: dict[str, Any], failure: str) -> None:
    class BadClient(CapturingClient):
        @override
        def request(
            self,
            model: str,
            payload: dict[str, Any],
            schema: dict[str, Any],
            instructions: str,
            *,
            operation: str = "request",
        ) -> dict[str, Any]:
            if failure == "transport":
                raise httpx.ReadTimeout("mock timeout")
            output = super().request(model, payload, schema, instructions, operation=operation)
            match failure:
                case "missing":
                    output["assignments"].pop()
                case "duplicate":
                    output["assignments"][1] = output["assignments"][0]
                case "unknown":
                    output["assignments"][0]["category_id"] = "invented"
            return output

    result = evaluate(BadClient(), sample, variants(("mock",))[0])
    assert result["metrics"]["accuracy"] == 0
    assert result["metrics"]["coverage"] == 0
    assert result["metrics"]["failed_batch_rate"] == 1
    assert result["metrics"]["invalid_output_rate"] == (0 if failure == "transport" else 1)


def test_review_roundtrip_does_not_modify_database(tmp_path: Path) -> None:
    database = tmp_path / "private.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE categories (id, name, definition, kind, archived);"
            "CREATE TABLE transactions (id, description, account);"
            "CREATE TABLE assignments (transaction_id, category_id, active);"
            "CREATE TABLE confirmed_examples (transaction_id, category_id, confirmed_at);"
            "INSERT INTO categories VALUES ('food', 'Food', 'Food purchases', 'spending', 0);"
            "INSERT INTO transactions VALUES ('one', 'Market', 'private-account');"
            "INSERT INTO transactions VALUES ('two', 'Market', 'private-account');"
            "INSERT INTO assignments VALUES ('one', 'food', 1);"
            "INSERT INTO confirmed_examples VALUES ('one', 'food', '2026-01-01');"
        )
    original = database.read_bytes()
    review = tmp_path / "review.csv"
    export_review(database, review)
    assert database.read_bytes() == original
    assert "private-account" not in review.read_text()
    frozen = tmp_path / "dataset.json"
    with pytest.raises(ValueError, match="reviewed=yes"):
        prepare_review(review, frozen, "Reviewer")
    with review.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row["reviewed"] = "yes"
        row["expected_category"] = "Food"
    with review.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    prepare_review(review, frozen, "Reviewer")
    dataset = load_dataset(frozen)
    assert dataset["examples"] == []
    assert len(dataset["cases"]) == 2
    assert database.read_bytes() == original
    with pytest.raises(FileExistsError):
        prepare_review(review, frozen, "Reviewer")


@pytest.fixture
def export_database(tmp_path: Path) -> Path:
    database = tmp_path / "export.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE categories (id, name, definition, kind, archived);"
            "CREATE TABLE transactions (id, description, account);"
            "CREATE TABLE assignments (transaction_id, category_id, active);"
            "CREATE TABLE confirmed_examples (transaction_id, category_id, confirmed_at);"
        )
        connection.executemany(
            "INSERT INTO categories VALUES (?, ?, '', 'spending', ?)",
            [(name, name, 0) for name in ("Food", "Shopping", "Travel", "Health", "Empty")]
            + [("Archived", "Archived", 1)],
        )
        connection.executemany(
            "INSERT INTO transactions VALUES (?, ?, '')",
            [(name, "Shared merchant") for name in ("Food", "Shopping", "Travel", "Health")]
            + [("unassigned", "Unassigned merchant"), ("archived", "Archived merchant")],
        )
        connection.executemany(
            "INSERT INTO assignments VALUES (?, ?, ?)",
            [(name, name, 1) for name in ("Food", "Shopping", "Travel", "Health")]
            + [("archived", "Archived", 1), ("unassigned", "Empty", 0)],
        )
        connection.execute("INSERT INTO confirmed_examples VALUES ('Food', 'Food', '2026-01-01')")
    return database


def test_export_covers_all_assigned_categories_including_conflicts(
    export_database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = export_database.read_bytes()
    review = tmp_path / "review.csv"
    export_review(export_database, review, limit=1)
    snapshot = json.loads(review.with_suffix(".snapshot.json").read_text())
    rows = snapshot["cases"]
    assert {r["candidate_category"] for r in rows} == {"Food", "Shopping", "Travel", "Health"}
    assert len(rows) == 4
    assert len({r["description"] for r in rows}) == 1
    assert snapshot["examples"] == []
    assert "Categories without assigned transactions: Empty" in capsys.readouterr().out
    assert export_database.read_bytes() == original
    repeated = tmp_path / "repeated.csv"
    export_review(export_database, repeated, limit=1)
    assert repeated.read_bytes() == review.read_bytes()


def test_export_fills_remaining_limit_without_duplicate_rows(
    export_database: Path, tmp_path: Path
) -> None:
    review = tmp_path / "review.csv"
    export_review(export_database, review, limit=3)
    snapshot = json.loads(review.with_suffix(".snapshot.json").read_text())
    rows = snapshot["cases"]
    assert len(rows) == 6
    assert len({r["id"] for r in rows}) == 6
    assert len({r["description"] for r in rows}) == 3
    assert {r["candidate_category"] for r in rows} == {"Food", "Shopping", "Travel", "Health", ""}


def test_export_rejects_limit_that_cannot_cover_categories(
    export_database: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(export_database) as connection:
        connection.execute("UPDATE transactions SET description=id")
    review = tmp_path / "review.csv"
    with pytest.raises(ValueError, match="too small to cover every assigned category"):
        export_review(export_database, review, limit=3)
    assert not review.exists()
    assert not review.with_suffix(".snapshot.json").exists()
    export_review(export_database, review, limit=4)
    snapshot = json.loads(review.with_suffix(".snapshot.json").read_text())
    assert {r["candidate_category"] for r in snapshot["cases"]} == {
        "Food",
        "Shopping",
        "Travel",
        "Health",
    }


def test_dataset_rejects_edits_and_held_out_leakage(tmp_path: Path, sample: dict[str, Any]) -> None:
    path = tmp_path / "dataset.json"
    sample["examples"][0]["example_id"] = "row-1"
    sample["hash"] = content_hash({k: v for k, v in sample.items() if k != "hash"})
    write_json(path, sample)
    with pytest.raises(ValueError, match="held-out"):
        load_dataset(path)
    sample["reviewer"] = "Edited"
    path.write_text(json.dumps(sample))
    with pytest.raises(ValueError, match="hash"):
        load_dataset(path)


def test_offline_provider_keeps_artifacts_without_fake_token_counts(
    tmp_path: Path, sample: dict[str, Any]
) -> None:
    path = tmp_path / "dataset.json"
    write_json(path, sample)
    output = tmp_path / "run"
    response = provider_response(
        "ignored",
        {
            "config": {
                "dataset": str(path),
                "output": str(output),
                "offline": True,
                "variant": variants(("mock",))[7],
            }
        },
        {},
    )
    assert "tokenUsage" not in response
    assert response["metadata"]["offline"] is True
    assert json.loads((output / "result.json").read_text())["offline"] is True


def test_scripted_fixture_cannot_run_live(tmp_path: Path, sample: dict[str, Any]) -> None:
    sample["offline_only"] = True
    sample["hash"] = content_hash({k: v for k, v in sample.items() if k != "hash"})
    path = tmp_path / "dataset.json"
    write_json(path, sample)
    with pytest.raises(ValueError, match="offline integration"):
        run_pilot(path, tmp_path / "run")


def test_provider_aggregates_real_transport_usage(
    tmp_path: Path, sample: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Login:
        def active_account(self) -> str:
            return "mock-account"

        def access_token(self) -> str:
            return "mock-token"

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["input"][0]["content"])
        output = OfflineClient().request("mock", payload, {}, "")
        event = {
            "type": "response.completed",
            "response": {
                "output": [{"content": [{"type": "output_text", "text": json.dumps(output)}]}],
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            },
        }
        return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")

    factory = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs)
    )
    monkeypatch.setattr(experimentation, "ChatGPTSession", Login)
    sample["cases"] = [
        {"id": f"row-{i}", "description": "Market", "expected_category_id": "c-food"}
        for i in range(26)
    ]
    sample["hash"] = content_hash({k: v for k, v in sample.items() if k != "hash"})
    path = tmp_path / "dataset.json"
    write_json(path, sample)
    response = provider_response(
        "ignored",
        {
            "config": {
                "dataset": str(path),
                "output": str(tmp_path / "run"),
                "variant": variants(("mock",))[0],
            }
        },
        {},
    )
    assert response["tokenUsage"] == {"prompt": 200, "completion": 40, "total": 240}
    output = json.loads(response["output"])
    assert output["metrics"]["requests"] == 2
    assert output["metrics"]["accuracy"] == 1
    assert len(list((tmp_path / "run/traces").glob("*/request.json"))) == 2


def test_report_pairs_changes_and_repetition_stability(
    tmp_path: Path, sample: dict[str, Any]
) -> None:
    baseline = evaluate(OfflineClient(), sample, variants(("mock",))[0])
    baseline.update(tokenUsage=None, offline=True)
    changed = json.loads(json.dumps(baseline))
    changed["config"] = variants(("mock",))[7]
    changed["rows"][1]["prediction"]["category_id"] = "c-shop"
    write_json(tmp_path / "mock-names0-short0-group0-r1/result.json", baseline)
    write_json(tmp_path / "mock-names1-short1-group1-r1/result.json", changed)
    changed["rows"][1]["prediction"]["category_id"] = "c-food"
    write_json(tmp_path / "mock-names1-short1-group1-r2/result.json", changed)
    generate_report(tmp_path)
    generate_report(tmp_path)  # Regeneration preserves source artifacts.
    report = json.loads((tmp_path / "comparison.json").read_text())
    assert report["runs"][1]["improved"] == 1
    assert report["runs"][2]["improved"] is None  # Baseline repetition 2 is absent.
    assert report["stability"]["mock-names1-short1-group1"] == pytest.approx(2 / 3)
    assert "not model accuracy evidence" in (tmp_path / "comparison.md").read_text()
