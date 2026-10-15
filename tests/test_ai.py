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
    relevant_examples,
    start_run,
    validate_assignments,
    validate_proposals,
)
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.models import RunItem


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
@pytest.mark.parametrize("tracing", [False, True])
def test_subscription_transport_requires_a_completed_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    terminal: str,
    text_source: str,
    operation: str,
    tracing: bool,
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
    trace_dir = tmp_path / "traces"
    caplog.set_level("INFO", logger="fyntok.llm_traces")
    client = ResponsesClient(Login(), trace_dir=trace_dir if tracing else None)
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
    if not tracing:
        assert not trace_dir.exists()
        return
    directories = list(trace_dir.iterdir())
    assert len(directories) == 1
    directory = directories[0]
    assert json.loads((directory / "request.json").read_text()) == body
    # An interrupted stream with completion-only output never sends that output.
    expected = "" if terminal == "interrupted" and text_source == "completion" else output
    assert (directory / "response.txt").read_text() == expected
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["operation"] == operation
    assert metadata["model"] == "selected-model"
    assert metadata["http_status"] == 200
    assert metadata["status"] == ("completed" if terminal == "response.completed" else "failed")
    assert metadata["stream_status"] == terminal.removeprefix("response.")
    assert metadata["finished_at"] >= metadata["started_at"]
    assert metadata["duration_seconds"] >= 0
    assert metadata["run_id"] is None and metadata["batch_number"] is None
    assert str(directory.resolve()) in caplog.text
    assert output not in caplog.text
    assert all("test-token" not in file.read_text() for file in directory.iterdir())


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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: list[dict[str, str]],
    error: str,
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
        ResponsesClient(Login(), trace_dir=tmp_path).propose("model", {})
    assert "private" not in str(caught.value)
    directory = next(tmp_path.iterdir())
    assert (directory / "response.txt").read_text() == "".join(
        part.get("text", part.get("refusal", "")) for part in content
    )
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["status"] == "failed" and metadata["exception_type"] == "ValueError"


def test_completed_batches_survive_a_later_failure_and_reopening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fyntok import categorization

    monkeypatch.setattr(categorization, "BATCH_SIZE", 1)
    path = tmp_path / "batches.sqlite3"
    engine = open_database(path)
    import_content(
        engine,
        b"a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\na;EXAMPLE CARE;02.01.2024;02.01.2024;20,00;EUR\n",
        "bank.csv",
    )

    class Advisor:
        calls = 0

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model
            self.calls += 1
            target = payload["transactions"][0]
            assert target["amount_cents"] == {"EXAMPLE GROCERY": -1000, "EXAMPLE CARE": 2000}[target["description"]]
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
