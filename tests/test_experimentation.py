"""Benchmark isolation, scoring, dataset integrity, and reviewed private export."""

import csv
import json
import runpy
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
    load_experiment,
    prepare_review,
    provider_response,
    run_pilot,
    variants,
    write_json,
)

EXAMPLE_PLACEMENT = (
    Path(__file__).resolve().parents[1]
    / "experiments/categorization/experiments/example-placement/experiment.json"
)


@pytest.fixture
def sample() -> dict[str, Any]:
    dataset: dict[str, Any] = {
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
    dataset["cases"] = [
        {
            **case,
            "amount_cents": -1000 if index == 0 else 2000,
            "booking_date": "2026-01-15",
        }
        for index, case in enumerate(dataset["cases"])
    ]
    dataset["hash"] = content_hash(dataset)
    return dataset


class CapturingClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.schemas: list[dict[str, Any]] = []
        self.instructions: list[str] = []

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
        self.schemas.append(schema)
        self.instructions.append(instructions)
        return OfflineClient().request(model, payload, schema, instructions, operation=operation)


def test_variants_keep_duplicates_separate_and_omit_booking_date(
    sample: dict[str, Any],
) -> None:
    baseline_examples = None
    assert len(variants()) == 1
    assert {v["model"] for v in variants()} == {"gpt-6.1-sol"}
    # Even a stale caller requesting dates must not put them in the LLM query.
    for config in [variants(("mock",))[0], {**variants(("mock",))[0], "booking_date": True}]:
        client = CapturingClient()
        result = evaluate(client, sample, config)
        payload = client.payloads[0]
        assert len(payload["transactions"]) == 3
        assert not config["grouped"]
        for target, case in zip(payload["transactions"], sample["cases"], strict=True):
            assert target["amount_cents"] == case["amount_cents"]
            assert "booking_date" not in target
            assert "value_date" not in target
            assert "currency" not in target
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
        assert payload["confirmed_examples"][0]["category_name"] == (
            "Food" if config["category_names"] else "c-food"
        )
        example_ids = [e["example_id"] for e in payload["confirmed_examples"]]
        if baseline_examples is None:
            baseline_examples = example_ids
        assert example_ids == baseline_examples


def test_example_layouts_preserve_guidance_and_response_contract(sample: dict[str, Any]) -> None:
    configs, provider = load_experiment(EXAMPLE_PLACEMENT)
    transform = runpy.run_path(str(provider))["nest_examples"]
    assert [c["example_layout"] for c in configs] == ["flat", "nested"]
    sample["review_example_ids"] = ["example-1"]
    before = json.dumps(sample)
    clients = [CapturingClient(), CapturingClient()]
    results = [
        evaluate(client, sample, config, transform=transform if i else None)
        for i, (client, config) in enumerate(zip(clients, configs, strict=True))
    ]
    flat, nested = [client.payloads[0] for client in clients]
    assert flat["transactions"] == nested["transactions"]
    assert "confirmed_examples" not in nested
    assert nested["categories"][1]["confirmed_examples"] == []
    flattened = [
        {**example, "category_name": category["name"]}
        for category in nested["categories"]
        for example in category["confirmed_examples"]
    ]
    assert flattened == flat["confirmed_examples"]
    assert [
        {k: v for k, v in c.items() if k != "confirmed_examples"} for c in nested["categories"]
    ] == flat["categories"]
    assert clients[0].schemas == clients[1].schemas
    assert clients[0].instructions == clients[1].instructions
    assert results[0]["rows"] == results[1]["rows"]
    assert json.dumps(sample) == before


@pytest.mark.parametrize(
    "changes",
    [{"example_layout": "unknown"}, {"example_layout": "nested", "category_format": "legacy"}],
)
def test_invalid_example_layout_fails_before_inference(
    changes: dict[str, str],
) -> None:
    configs, provider = load_experiment(EXAMPLE_PLACEMENT)
    call_api = runpy.run_path(str(provider))["call_api"]
    with pytest.raises(ValueError, match="This experiment requires"):
        call_api("", {"config": {"variant": {**configs[0], **changes}}}, {})


def test_experiment_config_defaults_and_relative_provider(tmp_path: Path) -> None:
    (tmp_path / "provider.py").write_text("# Local provider fixture.\n")
    path = tmp_path / "experiment.json"
    write_json(
        path,
        {
            "defaults": variants(("mock",))[0],
            "variants": [{"label": "baseline", "baseline": True}, {"label": "alternative"}],
            "provider": "provider.py",
        },
    )
    configs, provider = load_experiment(path)
    assert provider == tmp_path / "provider.py"
    assert [c["label"] for c in configs] == ["baseline", "alternative"]
    assert all(c["model"] == "mock" for c in configs)
    assert all(c["category_format"] == "name-only-v1" for c in configs)


@pytest.mark.parametrize(
    "changes",
    [
        {"variants": []},
        {"variants": ["invalid"]},
        {"defaults": []},
        {"defaults": {}},
        {"variants": [{}, {}]},
        {"provider": "missing.py"},
    ],
)
def test_experiment_config_rejects_invalid_matrix(tmp_path: Path, changes: dict[str, Any]) -> None:
    path = tmp_path / "experiment.json"
    write_json(path, {"defaults": variants(("mock",))[0], "variants": [{}], **changes})
    with pytest.raises(ValueError):
        load_experiment(path)


def test_experiment_config_can_use_shared_provider(tmp_path: Path) -> None:
    path = tmp_path / "experiment.json"
    write_json(path, {"defaults": variants(("mock",))[0], "variants": [{}]})
    configs, provider = load_experiment(path)
    assert configs == variants(("mock",))
    assert provider == experimentation.TOOLS / "provider.py"


@pytest.mark.parametrize("index", [0, 1])
def test_experiment_local_provider_uses_shared_scoring(
    tmp_path: Path, sample: dict[str, Any], index: int
) -> None:
    configs, provider = load_experiment(EXAMPLE_PLACEMENT)
    dataset_path = tmp_path / "dataset.json"
    write_json(dataset_path, sample)
    response = runpy.run_path(str(provider))["call_api"](
        "",
        {
            "config": {
                "dataset": str(dataset_path),
                "output": str(tmp_path / "run"),
                "offline": True,
                "variant": configs[index],
            }
        },
        {},
    )
    result = json.loads(response["output"])
    assert result["config"] == configs[index]
    assert result["metrics"]["coverage"] == 1
    assert result["metrics"]["accuracy"] == pytest.approx(2 / 3)
    assert result["offline"]


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
                    output["assignments"][0]["category_name"] = "invented"
            return output

    result = evaluate(BadClient(), sample, variants(("mock",))[0])
    assert result["metrics"]["accuracy"] == 0
    assert result["metrics"]["coverage"] == 0
    assert result["metrics"]["failed_batch_rate"] == 1
    assert result["metrics"]["invalid_output_rate"] == (0 if failure == "transport" else 1)


@pytest.mark.parametrize("invalid", ["missing", "float", "boolean", "date"])
def test_context_is_validated_before_inference(sample: dict[str, Any], invalid: str) -> None:
    match invalid:
        case "missing":
            del sample["cases"][0]["amount_cents"]
        case "float":
            sample["cases"][0]["amount_cents"] = 1.5
        case "boolean":
            sample["cases"][0]["amount_cents"] = True
        case "date":
            sample["cases"][0]["booking_date"] = "20260115"
    client = CapturingClient()
    with pytest.raises(ValueError):
        evaluate(client, sample, variants(("mock",))[0])
    assert not client.payloads


def test_review_roundtrip_does_not_modify_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE categories (id, name, definition, kind, archived);"
            "CREATE TABLE transactions (id, description, account, "
            "amount_cents INTEGER DEFAULT -1250, booking_date TEXT DEFAULT '2026-01-15');"
            "CREATE TABLE assignments (transaction_id, category_id, active, source, protected);"
            "CREATE TABLE confirmed_examples (transaction_id, category_id, confirmed_at);"
            "INSERT INTO categories VALUES ('food', 'Food', 'Food purchases', 'spending', 0);"
            "INSERT INTO transactions (id, description, account) "
            "VALUES ('one', 'Market', 'private-account');"
            "INSERT INTO transactions (id, description, account) "
            "VALUES ('two', 'Market', 'private-account');"
            "INSERT INTO assignments VALUES ('one', 'food', 1, 'ai', 0);"
            "INSERT INTO confirmed_examples VALUES ('one', 'food', '2026-01-01');"
        )
    original = database.read_bytes()
    review = tmp_path / "review.csv"
    export_review(database, review)
    assert database.read_bytes() == original
    assert "private-account" not in review.read_text()
    snapshot = json.loads(review.with_suffix(".snapshot.json").read_text())
    assert snapshot["cases"][0]["amount_cents"] == -1250
    assert snapshot["cases"][0]["booking_date"] == "2026-01-15"

    def deny_database(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Preparing the dataset must not access SQLite.")

    monkeypatch.setattr(sqlite3, "connect", deny_database)
    frozen = tmp_path / "dataset.json"
    with pytest.raises(ValueError, match="reviewed=yes"):
        prepare_review(review, frozen, "Reviewer")
    with review.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row["reviewed"] = "yes"
        row["expected_category"] = "Food"
        assert row["amount_cents"] == "-1250"
        assert row["booking_date"] == "2026-01-15"
        row["amount_cents"] = "250000"
        row["booking_date"] = "2026-02-28"
    with review.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    prepare_review(review, frozen, "Reviewer")
    dataset = load_dataset(frozen)
    assert dataset["examples"] == []
    assert len(dataset["cases"]) == 1
    assert dataset["cases"][0]["amount_cents"] == 250000
    assert dataset["cases"][0]["booking_date"] == "2026-02-28"
    assert database.read_bytes() == original
    with pytest.raises(FileExistsError):
        prepare_review(review, frozen, "Reviewer")


@pytest.mark.parametrize(
    ("field", "value"),
    [("amount_cents", "12.50"), ("amount_cents", ""), ("booking_date", "2026-02-30")],
)
def test_prepare_rejects_invalid_review_context(tmp_path: Path, field: str, value: str) -> None:
    review = tmp_path / "review.csv"
    row = {
        "id": "one",
        "description": "Market",
        "amount_cents": "-1250",
        "booking_date": "2026-01-15",
        "expected_category": "Food",
        "reviewed": "yes",
        "notes": "",
        field: value,
    }
    write_json(
        review.with_suffix(".snapshot.json"),
        {"categories": [{"id": "food", "name": "Food"}], "cases": [row]},
    )
    with review.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    output = tmp_path / "dataset.json"
    with pytest.raises(ValueError):
        prepare_review(review, output, "Reviewer")
    assert not output.exists()


def test_runner_rejects_missing_context_without_database_lookup(
    tmp_path: Path, sample: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny_database(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Runner must not look up missing context in SQLite.")

    monkeypatch.setattr(sqlite3, "connect", deny_database)
    del sample["cases"][0]["amount_cents"]
    sample["hash"] = content_hash({k: v for k, v in sample.items() if k != "hash"})
    path = tmp_path / "dataset.json"
    write_json(path, sample)
    output = tmp_path / "run"
    with pytest.raises(ValueError, match="amount_cents"):
        run_pilot(path, output)
    assert not output.exists()


@pytest.fixture
def export_database(tmp_path: Path) -> Path:
    database = tmp_path / "export.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE categories (id, name, definition, kind, archived);"
            "CREATE TABLE transactions (id, description, account, "
            "amount_cents INTEGER DEFAULT -1250, booking_date TEXT DEFAULT '2026-01-15');"
            "CREATE TABLE assignments (transaction_id, category_id, active, source, protected);"
            "CREATE TABLE confirmed_examples (transaction_id, category_id, confirmed_at);"
        )
        connection.executemany(
            "INSERT INTO categories VALUES (?, ?, '', 'spending', ?)",
            [(name, name, 0) for name in ("Food", "Shopping", "Travel", "Health", "Empty")]
            + [("Archived", "Archived", 1)],
        )
        connection.executemany(
            "INSERT INTO transactions (id, description, account) VALUES (?, ?, '')",
            [(name, "Shared merchant") for name in ("Food", "Shopping", "Travel", "Health")]
            + [("unassigned", "Unassigned merchant"), ("archived", "Archived merchant")],
        )
        connection.executemany(
            "INSERT INTO assignments (transaction_id, category_id, active) VALUES (?, ?, ?)",
            [(name, name, 1) for name in ("Food", "Shopping", "Travel", "Health")]
            + [("archived", "Archived", 1), ("unassigned", "Empty", 0)],
        )
        connection.execute("INSERT INTO confirmed_examples VALUES ('Food', 'Food', '2026-01-01')")
    return database


def test_export_deduplicates_conflicts_and_reports_unrepresented_categories(
    export_database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = export_database.read_bytes()
    review = tmp_path / "review.csv"
    export_review(export_database, review, limit=1)
    snapshot = json.loads(review.with_suffix(".snapshot.json").read_text())
    rows = snapshot["cases"]
    assert {r["candidate_category"] for r in rows} == {"Food"}
    assert len(rows) == 1
    assert len({r["description"] for r in rows}) == 1
    assert snapshot["examples"] == []
    output = capsys.readouterr().out
    assert "Categories without assigned transactions: Empty" in output
    assert "Categories without a distinct description representative: Health, Shopping, Travel" in (
        output
    )
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
    assert len(rows) == 3
    assert len({r["id"] for r in rows}) == 3
    assert len({r["description"] for r in rows}) == 3
    assert {r["candidate_category"] for r in rows} == {"Food", ""}


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


@pytest.mark.parametrize("repeated", [False, True])
def test_export_includes_all_current_manual_choices_beyond_sample_limits(
    export_database: Path, tmp_path: Path, *, repeated: bool
) -> None:
    manual_ids = {f"manual-{index:02}" for index in range(55)}
    with sqlite3.connect(export_database) as connection:
        connection.execute("UPDATE transactions SET description=id")
        connection.executemany(
            "INSERT INTO transactions (id, description, account) VALUES (?, ?, '')",
            [
                (tid, "Manual merchant" if repeated else f"Manual merchant {tid}")
                for tid in sorted(manual_ids)
            ],
        )
        connection.executemany(
            "INSERT INTO assignments VALUES (?, 'Food', 1, 'manual', 0)",
            [(tid,) for tid in sorted(manual_ids)],
        )
        # Undo and category merges preserve manual protection with a different source.
        connection.execute(
            "UPDATE assignments SET source='undo', protected=1 WHERE transaction_id='manual-53'"
        )
        connection.execute(
            "UPDATE assignments SET source='system', protected=1 WHERE transaction_id='manual-54'"
        )
        connection.execute("INSERT INTO assignments VALUES ('unassigned', 'Food', 0, 'manual', 1)")
        connection.executemany(
            "INSERT INTO confirmed_examples VALUES (?, 'Food', '2026-01-01')",
            [(tid,) for tid in sorted(manual_ids)],
        )
    original = export_database.read_bytes()
    review = tmp_path / "review.csv"
    export_review(export_database, review, limit=4)
    snapshot = json.loads(review.with_suffix(".snapshot.json").read_text())
    rows = snapshot["cases"]
    exported_ids = {r["id"] for r in rows}
    if repeated:
        assert len(manual_ids & exported_ids) == 1
    else:
        assert manual_ids <= exported_ids
    assert "unassigned" not in exported_ids
    assert len(rows) == len(exported_ids)
    assert {r["candidate_category"] for r in rows} == {"Food", "Health", "Shopping", "Travel"}
    manual_descriptions = {
        "Manual merchant" if repeated else f"Manual merchant {tid}" for tid in manual_ids
    }
    assert manual_descriptions <= {r["description"] for r in rows}
    assert len(rows) == len({r["description"] for r in rows})
    with review.open(newline="") as stream:
        assert {r["id"] for r in csv.DictReader(stream)} == exported_ids
    assert not manual_ids & {e["example_id"] for e in snapshot["examples"]}
    assert not {e["description"] for e in snapshot["examples"]} & {r["description"] for r in rows}
    assert export_database.read_bytes() == original


def test_export_reassigns_shared_description_to_preserve_category_coverage(
    export_database: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(export_database) as connection:
        connection.executemany(
            "INSERT INTO transactions (id, description, account) VALUES (?, ?, '')",
            [(f"other-{name}", f"Unique {name}") for name in ("Food", "Health", "Shopping")],
        )
        connection.executemany(
            "INSERT INTO assignments VALUES (?, ?, 1, 'ai', 0)",
            [(f"other-{name}", name) for name in ("Food", "Health", "Shopping")],
        )
        # The shared merchant must represent Travel; its manual choice wins over AI Food.
        connection.execute("UPDATE assignments SET source='manual' WHERE transaction_id='Travel'")
    review = tmp_path / "review.csv"
    export_review(export_database, review, limit=4)
    snapshot = json.loads(review.with_suffix(".snapshot.json").read_text())
    rows = snapshot["cases"]
    assert len(rows) == len({r["description"] for r in rows}) == 4
    assert {r["candidate_category"] for r in rows} == {"Food", "Health", "Shopping", "Travel"}
    assert next(r for r in rows if r["description"] == "Shared merchant")["id"] == "Travel"
    assert snapshot["examples"] == []


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
    tmp_path: Path, sample: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny_database(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Running the provider must use the frozen dataset, not SQLite.")

    monkeypatch.setattr(sqlite3, "connect", deny_database)
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
                "variant": variants(("mock",))[0],
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
        {
            "id": f"row-{i}",
            "description": "Market",
            "expected_category_id": "c-food",
            "amount_cents": -100,
            "booking_date": "2026-01-15",
        }
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
    write_json(tmp_path / "dataset.json", sample)
    baseline = evaluate(OfflineClient(), sample, variants(("mock",))[0])
    baseline.update(tokenUsage=None, offline=True)
    changed = json.loads(json.dumps(baseline))
    changed["config"] = {**variants(("mock",))[0], "booking_date": True}
    changed["rows"][1]["prediction"]["category_id"] = "c-shop"
    write_json(tmp_path / "mock-amount1-date0-r1/result.json", baseline)
    write_json(tmp_path / "mock-amount1-date1-r1/result.json", changed)
    changed["rows"][1]["prediction"]["category_id"] = "c-food"
    write_json(tmp_path / "mock-amount1-date1-r2/result.json", changed)
    generate_report(tmp_path)
    generate_report(tmp_path)  # Regeneration preserves source artifacts.
    report = json.loads((tmp_path / "comparison.json").read_text())
    assert report["runs"][1]["improved"] == 1
    assert report["runs"][2]["improved"] is None  # Baseline repetition 2 is absent.
    assert report["stability"]["mock-amount1-date1"] == pytest.approx(2 / 3)
    assert "not model accuracy evidence" in (tmp_path / "comparison.md").read_text()


def test_layout_report_uses_flat_baseline_even_when_nested_sorts_first(
    tmp_path: Path, sample: dict[str, Any]
) -> None:
    write_json(tmp_path / "dataset.json", sample)
    configs, _ = load_experiment(EXAMPLE_PLACEMENT)
    flat, nested = [{**config, "model": "mock"} for config in configs]
    baseline = evaluate(OfflineClient(), sample, flat)
    baseline.update(tokenUsage=None, offline=True)
    changed = json.loads(json.dumps(baseline))
    changed["config"] = nested
    changed["rows"][1]["prediction"]["category_id"] = "c-shop"
    write_json(tmp_path / "a-nested-r1/result.json", changed)
    write_json(tmp_path / "z-flat-r1/result.json", baseline)
    generate_report(tmp_path)
    report = json.loads((tmp_path / "comparison.json").read_text())
    assert report["runs"][0]["improved"] == 1
    assert report["runs"][0]["worsened"] == 0
    assert report["runs"][1]["improved"] == 0
    assert {v["label"] for v in report["variants"]} == {
        "mock · flat examples",
        "mock · nested examples",
    }
