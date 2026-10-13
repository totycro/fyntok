"""Boundary checks for data minimization, validation, transport, and durable batches."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from fyntok.ai import AIRequestError, ResponsesClient
from fyntok.categorization import (
    execute_run,
    minimize_description,
    relevant_examples,
    start_run,
    validate_assignments,
    validate_proposals,
)
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.models import RunItem


def test_minimization_omits_bank_fields_and_common_identifiers() -> None:
    assert (
        minimize_description("account | card 123 01.01.2024 | EXAMPLE GROCERY 543 Berlin", "account")
        == "EXAMPLE GROCERY Berlin"
    )
    assert minimize_description("Pharmacy", "a") == "Pharmacy"
    text = minimize_description(
        "DE89370400440532013000 user@example.com account 123456 Reference: secret", "account"
    )
    assert all(
        secret not in text
        for secret in ("DE89370400440532013000", "user@example.com", "123456", "secret")
    )


# Patterns from the opening CSV rows, with synthetic merchants, identifiers, and dates.
@pytest.mark.parametrize(
    ("description", "expected"),
    [
        pytest.param(
            r"Bezahlung Karte MC/000000001|BIOMARKT999 9999 K009 01.01. 12:34|"
            r"BEISPIEL BIOMARKT\\WIEN\9999",
            "BEISPIEL BIOMARKT / WIEN",
            id="card-supermarket-location",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000002|POS M9XX K009 01.01. 12:34|"
            r"SUMUP *BEISPIELPRAXIS\\WIEN\9",
            "SUMUP *BEISPIELPRAXIS / WIEN",
            id="card-payment-processor",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000003|E-COMM K009 01.01. 12:34|"
            r"UZR*BEISPIELTICKET*DEMO\\WIEN\9999",
            "UZR*BEISPIELTICKET*DEMO / WIEN",
            id="ecommerce-ticket-processor",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000004|POS 9999 K009 01.01. 12:34|"
            r"BEISPIEL CAFE\\WIEN\9999",
            "BEISPIEL CAFE / WIEN",
            id="card-cafe",
        ),
        pytest.param(
            "/CNR/999999/DOC/99999999/12.34/20240101|OG/000000005|"
            "TESTATWWXXX AT000000000000000000 Beispiel Lieferdienst GmbH",
            "TESTATWWXXX [ACCOUNT] Beispiel Lieferdienst GmbH",
            id="direct-debit-inline-iban",
        ),
        pytest.param(
            "Beispiel Mobil KdNr: 9999999 OG/000000006|"
            "TESTATWWXXX AT000000000000000000|Beispiel Firma GmbH",
            "Beispiel Firma GmbH",
            id="direct-debit-customer-number",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000007|MARKT 9999 K009 01.01. 12:34|"
            r"BEISPIEL MARKT DANKT 9999\\WIEN\9999",
            "BEISPIEL MARKT DANKT / WIEN",
            id="card-merchant-branch-number",
        ),
        pytest.param(
            "REF-EXAMPLE999999 OG/000000008|"
            "TESTFRP1XXX FR0000000000000000000000000|Beispiel Medien GmbH",
            "Beispiel Medien GmbH",
            id="transfer-reference-and-foreign-iban",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000009|DROGERIE-FIL. A999 9999 K009 01.01. 12:34|"
            r"DROGERIE-FIL. A999\\WIEN\9999      W",
            "DROGERIE-FIL. A / WIEN / W",
            id="card-alphanumeric-branch-and-suffix",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000010|POS 9999 K009 01.01. 12:34|"
            r"BEISPIEL PIZZERIA\\WIEN\9999",
            "BEISPIEL PIZZERIA / WIEN",
            id="card-restaurant",
        ),
        pytest.param(
            "MIETE 01/2024 OG/000000011|TESTATWWXXX AT000000000000000000|"
            "Beispiel gemeinnutzige Wohnungsakt",
            "Beispiel gemeinnutzige Wohnungsakt",
            id="rent-transfer",
        ),
        pytest.param(
            "SALDO BEISPIEL 999999999999 OG/000000012|"
            "TESTATWWXXX AT000000000000000000 Beispiel Betreiberfirma GmbH",
            "TESTATWWXXX [ACCOUNT] Beispiel Betreiberfirma GmbH",
            id="invoice-transfer-inline-iban",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000013|POS 9999 K009 01.01. 12:34|"
            r"BEISPIEL DROGERIE DANKT 9999\\WIEN\9999",
            "BEISPIEL DROGERIE DANKT / WIEN",
            id="card-drugstore",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000014|POS 9999 K009 01.01. 12:34|"
            r"APOTHEKE BEISPIELVIERT\\WIEN\9",
            "APOTHEKE BEISPIELVIERT / WIEN",
            id="card-pharmacy",
        ),
    ],
)
def test_minimization_of_csv_description_patterns(description: str, expected: str) -> None:
    assert minimize_description(description, "AT000000000000000000") == expected


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            "Beispielbank kreditkarte TEST 200001 MC/000000001|"
            "Beispielbank kreditkarte TEST 200001Abrechnung Nr. 000000001|10",
            "Beispielbank kreditkarte TEST",
        ),
        (
            "Musterbank kreditkarte DEMO 200002 MC/000000002|Abrechnung Nr. 000000002",
            "Musterbank kreditkarte DEMO",
        ),
        ("Bank | Shop 24 | 10 | 11 | ", "Shop"),
        (
            "Bank | AT000000000000000000 / Beispiel Sonnenschutz und | 0000000001",
            "[ACCOUNT] / Beispiel Sonnenschutz und",
        ),
        (
            "Bank | account user@example.com Shop Reference: secret | 10",
            "[ACCOUNT] [EMAIL] Shop",
        ),
        ("10 | 11 | ", "Unknown merchant"),
        ("", "Unknown merchant"),
    ],
)
def test_minimization_selects_merchant_before_redacting(description: str, expected: str) -> None:
    assert minimize_description(description, "account") == expected


def test_related_examples_are_ranked_before_recent_unrelated_examples() -> None:
    unrelated = [{"description": "Unrelated pharmacy", "category_id": "care"} for _ in range(50)]
    supermarket = {"description": "EXAMPLE GROCERY Berlin", "category_id": "food"}
    selected = relevant_examples([*unrelated, supermarket], [{"description": "EXAMPLE GROCERY Hamburg"}])
    assert selected[0] == supermarket and len(selected) == 40


@pytest.mark.parametrize(
    "assignments",
    [
        [],
        [{"transaction_id": "other", "category_id": "food", "reason": "x"}],
        [{"transaction_id": "one", "category_id": "unknown", "reason": "x"}],
        [{"transaction_id": "one", "category_id": "food", "reason": 4}],
        [{"transaction_id": "one", "category_id": "food", "reason": "x"}] * 2,
    ],
)
def test_invalid_assignments_are_rejected_as_a_whole(assignments: list[dict[str, str]]) -> None:
    with pytest.raises(ValueError):
        validate_assignments(
            {"assignments": assignments}, [{"transaction_id": "one"}], [{"id": "food"}]
        )


def test_malformed_proposals_are_rejected() -> None:
    with pytest.raises(ValueError):
        validate_proposals(
            {"categories": [{"name": "Food", "kind": "invented", "definition": "x"}]}
        )


@pytest.mark.parametrize(
    "terminal", ["response.completed", "response.failed", "response.incomplete", "interrupted"]
)
@pytest.mark.parametrize("text_source", ["completion", "delta", "done", "delta_done", "all"])
@pytest.mark.parametrize("operation", ["propose", "classify"])
def test_subscription_transport_requires_a_completed_stream(
    monkeypatch: pytest.MonkeyPatch, terminal: str, text_source: str, operation: str
) -> None:
    class Login:
        def active_account(self) -> str | None:
            return "test-account"

        def access_token(self) -> str:
            return "test-token"

    received = []
    result = {"categories": []} if operation == "propose" else {"assignments": []}
    output = json.dumps(result)

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-token"
        events = []
        if text_source in {"delta", "delta_done", "all"}:
            events.extend(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": chunk,
                }
                for chunk in (output[:5], output[5:])
            )
        if text_source in {"done", "delta_done", "all"}:
            events.append(
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "content_index": 0,
                    "text": output,
                }
            )
        event = {
            "type": terminal,
            "response": {
                "output": (
                    [{"content": [{"type": "output_text", "text": output}]}]
                    if text_source in {"completion", "all"}
                    else []
                )
            },
        }
        if terminal != "interrupted":
            events.append(event)
        return httpx.Response(
            200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events)
        )

    factory = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs)
    )
    client = ResponsesClient(Login())
    request_result = client.propose if operation == "propose" else client.classify
    if terminal == "response.completed":
        assert (
            request_result("selected-model", {"categories": [], "descriptions": ["EXAMPLE GROCERY"]}) == result
        )
    else:
        with pytest.raises(RuntimeError):
            request_result("selected-model", {"categories": [], "descriptions": ["EXAMPLE GROCERY"]})
    body = received[0]
    assert body["store"] is False and body["stream"] is True
    assert body["model"] == "selected-model" and isinstance(body["input"], list)
    assert body["text"]["format"]["strict"] is True


@pytest.mark.parametrize(
    "content,error",
    [
        ([], "without returning output"),
        ([{"type": "output_text", "text": " "}], "without returning output"),
        ([{"type": "output_text", "text": "private invalid JSON"}], "malformed output"),
        ([{"type": "output_text", "text": "[]"}], "malformed output"),
        ([{"type": "refusal", "refusal": "private refusal text"}], "declined"),
    ],
)
def test_completed_stream_reports_safe_output_errors(
    monkeypatch: pytest.MonkeyPatch, content: list[dict[str, str]], error: str
) -> None:
    class Login:
        def active_account(self) -> str | None:
            return "test-account"

        def access_token(self) -> str:
            return "test-token"

    def respond(_request: httpx.Request) -> httpx.Response:
        event = {
            "type": "response.completed",
            "response": {"output": [{"content": content}]},
        }
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n")

    factory = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs)
    )
    with pytest.raises(ValueError, match=error) as caught:
        ResponsesClient(Login()).propose("model", {})
    assert "private" not in str(caught.value)


def test_completed_batches_survive_a_later_failure_and_reopening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fyntok import categorization

    monkeypatch.setattr(categorization, "BATCH_SIZE", 1)
    path = tmp_path / "batches.sqlite3"
    engine = open_database(path)
    import_content(
        engine,
        b"a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\na;EXAMPLE CARE;02.01.2024;02.01.2024;-20,00;EUR\n",
        "bank.csv",
    )

    class Advisor:
        calls = 0

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("Disconnected")
            return {
                "assignments": [
                    {
                        "transaction_id": payload["transactions"][0]["transaction_id"],
                        "category_id": "uncategorized",
                        "reason": "Unclear",
                    }
                ]
            }

    run_id = start_run(engine, "model")
    result = execute_run(engine, Advisor(), run_id)
    assert result["status"] == "failed" and result["applied"] == 1
    engine.dispose()
    engine = open_database(path)
    with Session(engine) as session:
        items = list(session.scalars(select(RunItem).where(RunItem.run_id == run_id)))
        applied = next(i for i in items if i.status == "applied")
        assert applied.applied_assignment_id is not None
        assert applied.result is not None
        assert applied.result["reason"] == "Unclear"
    engine.dispose()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, None),
        (403, "subscription_sharing_user_not_eligible"),
        (429, "subscription_sharing_usage_limit_exceeded"),
        (503, "subscription_sharing_usage_unavailable"),
    ],
)
@pytest.mark.parametrize("streamed", [False, True])
def test_session_and_plan_errors_stop_without_billing_fallback(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str | None, streamed: bool
) -> None:

    class Login:
        def active_account(self) -> str | None:
            return "account"

        def access_token(self) -> str:
            return "oauth-token"

    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if streamed:
            return httpx.Response(
                200,
                text="data: "
                + json.dumps(
                    {
                        "type": "response.failed",
                        "response": {
                            "error": {"code": code, "message": "private bank description"}
                        },
                    }
                )
                + "\n\n",
            )
        return httpx.Response(
            status, json={"error": {"code": code, "message": "private bank description"}}
        )

    factory = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs)
    )
    with pytest.raises(AIRequestError) as caught:
        ResponsesClient(Login()).propose("model", {"descriptions": ["Synthetic"]})
    assert "private" not in str(caught.value)
    assert caught.value.code == code
    assert len(requests) == 1 and requests[0].url == "https://api.openai.com/v1/responses"
    assert requests[0].headers["Authorization"] == "Bearer oauth-token"


def test_signout_during_stream_rejects_completed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class Login:
        signed_in = True

        def active_account(self) -> str | None:
            return "account"

        def access_token(self) -> str:
            if not self.signed_in:
                raise RuntimeError("Signed out")
            return "oauth-token"

    login = Login()

    def respond(_request: httpx.Request) -> httpx.Response:
        login.signed_in = False
        return httpx.Response(
            200,
            text="data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {"content": [{"type": "output_text", "text": '{"categories": []}'}]}
                        ]
                    },
                }
            )
            + "\n\n",
        )

    factory = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs)
    )
    with pytest.raises(RuntimeError, match="Signed out"):
        ResponsesClient(login).propose("model", {})
