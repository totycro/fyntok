"""Name-only requests preserve stable IDs and reject an invalid batch atomically."""

import copy
import json
from typing import Any, override

import pytest

from fyntok.ai import ResponsesClient
from fyntok.classification import CLASSIFICATION_INSTRUCTIONS, named_classification
from fyntok.experimentation import OfflineClient, evaluate, variants


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "categories": [
            {"id": "food-id", "name": "Food & Café", "definition": "Meals", "kind": "spending"},
            {"id": "income-id", "name": "Income", "definition": "Income", "kind": "income"},
        ],
        "confirmed_examples": [
            {"example_id": "example", "description": "Market", "category_id": "food-id"}
        ],
        "transactions": [
            {"transaction_id": "purchase", "description": "Market", "amount_cents": -1250},
            {"transaction_id": "incoming", "description": "Market", "amount_cents": 2000},
        ],
    }


def predictions() -> dict[str, Any]:
    return {
        "assignments": [
            {"transaction_id": "t2", "category_name": "Income", "reason": "Incoming"},
            {"transaction_id": "t1", "category_name": "Food & Café", "reason": "Food"},
        ]
    }


def test_named_response_uses_snapshot_and_accepts_reordered_rows(payload: dict[str, Any]) -> None:
    original = copy.deepcopy(payload)
    batch = named_classification(payload)
    assert payload == original
    assert batch.payload["categories"] == [
        {"name": "Food & Café", "definition": "Meals", "kind": "spending"},
        {"name": "Income", "definition": "Income", "kind": "income"},
    ]
    assert batch.payload["confirmed_examples"] == [
        {"example_id": "example", "description": "Market", "category_name": "Food & Café"}
    ]
    assert "category_id" not in json.dumps(batch.payload)
    assert [t["amount_cents"] for t in batch.payload["transactions"]] == [-1250, 2000]
    schema = batch.schema()["properties"]["assignments"]["items"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["category_name"]["enum"] == ["Food & Café", "Income"]
    # A later rename cannot change the mapping used to decode this saved request.
    payload["categories"][0]["name"] = "Renamed"
    assert batch.decode(predictions()) == {
        "assignments": [
            {"transaction_id": "incoming", "category_id": "income-id", "reason": "Incoming"},
            {"transaction_id": "purchase", "category_id": "food-id", "reason": "Food"},
        ]
    }
    payload["transactions"] = [
        {"transaction_id": "next-batch", "description": "Market", "amount_cents": -100}
    ]
    next_batch = named_classification(payload)
    assert next_batch.transaction_ids == {"t1": "next-batch"}
    assert batch.transaction_ids == {"t1": "purchase", "t2": "incoming"}


@pytest.mark.parametrize(
    "failure",
    ["missing", "duplicate", "unknown_transaction", "unknown_category", "case", "id", "reason"],
)
def test_invalid_wire_response_rejects_entire_batch(payload: dict[str, Any], failure: str) -> None:
    output = predictions()
    first = output["assignments"][0]
    match failure:
        case "missing":
            output["assignments"].pop()
        case "duplicate":
            output["assignments"][1] = first
        case "unknown_transaction":
            first["transaction_id"] = "purchase"
        case "unknown_category":
            first["category_name"] = "Invented"
        case "case":
            first["category_name"] = "income"
        case "id":
            first["category_id"] = first.pop("category_name")
        case "reason":
            first["reason"] = "x" * 2001
    with pytest.raises(ValueError):
        named_classification(payload).decode(output)


class Login:
    def active_account(self) -> str | None:
        return "test-account"

    def access_token(self) -> str:
        return "test-token"


class CapturingClient(ResponsesClient):
    def __init__(self) -> None:
        super().__init__(Login())
        self.calls: list[tuple[dict[str, Any], dict[str, Any], str]] = []

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
        self.calls.append((payload, schema, instructions))
        return OfflineClient().request(model, payload, schema, instructions, operation=operation)


def test_production_and_experiment_share_wire_format(payload: dict[str, Any]) -> None:
    client = CapturingClient()
    actual = client.classify("mock", payload)
    assert [a["transaction_id"] for a in actual["assignments"]] == ["purchase", "incoming"]
    assert {a["category_id"] for a in actual["assignments"]} == {"food-id"}
    dataset = {
        "categories": payload["categories"],
        "examples": payload["confirmed_examples"],
        "cases": [
            {
                "id": t["transaction_id"],
                "description": t["description"],
                "amount_cents": t["amount_cents"],
                "booking_date": "2026-01-15",
                "expected_category_id": "food-id",
            }
            for t in payload["transactions"]
        ],
        "hash": "synthetic",
    }
    result = evaluate(client, dataset, variants(("mock",))[0])
    assert result["metrics"]["accuracy"] == 1
    assert client.calls[0] == client.calls[1]
    assert client.calls[0][2] == CLASSIFICATION_INSTRUCTIONS


@pytest.mark.parametrize("names", [False, True])
def test_legacy_experiment_formats_remain_supported(payload: dict[str, Any], names: bool) -> None:
    dataset = {
        "categories": payload["categories"],
        "examples": payload["confirmed_examples"],
        "cases": [
            {
                "id": "purchase",
                "description": "Market",
                "amount_cents": -1250,
                "booking_date": "2026-01-15",
                "expected_category_id": "food-id",
            }
        ],
        "hash": "synthetic",
    }
    config = {"model": "mock", "category_names": names, "short_refs": False, "grouped": False}
    client = CapturingClient()
    result = evaluate(client, dataset, config)
    assert result["metrics"]["accuracy"] == 1
    wire, schema, _ = client.calls[0]
    assert wire["categories"][0]["id"] == ("Food & Café" if names else "food-id")
    assert "category_id" in schema["properties"]["assignments"]["items"]["properties"]
