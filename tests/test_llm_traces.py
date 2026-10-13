"""Opt-in traces retain diagnostics without changing transport or assignment behavior."""

import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, override

import httpx
import pytest
from sqlalchemy.engine import Engine

from fyntok import cli, llm_traces
from fyntok.ai import AIRequestError, ResponsesClient
from fyntok.categorization import execute_run, propose_categories, start_run
from fyntok.database import open_database
from fyntok.importer import import_content


class Login:
    def active_account(self) -> str:
        return "private-account"

    def access_token(self) -> str:
        return "private-test-token"


def mock_transport(
    monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx.Request], httpx.Response]
) -> None:
    factory = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs)
    )


def stream_response(events: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events))


def complete(output: str = '{"categories": []}') -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {"output": [{"content": [{"type": "output_text", "text": output}]}]},
    }


def test_http_error_body_is_saved_only_in_trace_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    body = {"error": {"message": "private diagnostic detail"}}

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=body)

    mock_transport(monkeypatch, respond)
    caplog.set_level("INFO", logger="fyntok.llm_traces")
    with pytest.raises(AIRequestError) as caught:
        ResponsesClient(Login(), tmp_path).propose("model", {})
    assert "private diagnostic detail" not in str(caught.value)
    assert "private diagnostic detail" not in caplog.text
    directory = next(tmp_path.iterdir())
    assert json.loads((directory / "response.txt").read_text()) == body
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["http_status"] == 403 and metadata["status"] == "failed"
    assert metadata["exception_type"] == "AIRequestError"
    assert all("private-test-token" not in file.read_text() for file in directory.iterdir())


def test_streamed_refusal_is_retained_without_duplicate_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return stream_response(
            [
                {
                    "type": "response.refusal.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Private refusal",
                },
                {
                    "type": "response.refusal.done",
                    "output_index": 0,
                    "content_index": 0,
                    "refusal": "Private refusal text",
                },
                {"type": "response.completed", "response": {"output": []}},
            ]
        )

    mock_transport(monkeypatch, respond)
    with pytest.raises(ValueError, match="declined"):
        ResponsesClient(Login(), tmp_path).propose("model", {})
    assert (next(tmp_path.iterdir()) / "response.txt").read_text() == "Private refusal text"


def test_connection_failure_retains_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenStream(httpx.SyncByteStream):
        @override
        def __iter__(self) -> Iterator[bytes]:
            yield (
                b'data: {"type":"response.output_text.delta","output_index":0,'
                b'"content_index":0,"delta":"partial output"}\n\n'
            )
            raise httpx.ReadError("private network error")

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BrokenStream())

    mock_transport(monkeypatch, respond)
    with pytest.raises(httpx.ReadError):
        ResponsesClient(Login(), tmp_path).propose("model", {})
    directory = next(tmp_path.iterdir())
    assert (directory / "response.txt").read_text() == "partial output"
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["stream_status"] == "interrupted"
    assert metadata["exception_type"] == "ReadError"
    assert "private network error" not in (directory / "metadata.json").read_text()


@pytest.mark.parametrize("failure", ["directory", "request.json", "response.txt", "metadata.json"])
def test_trace_write_failures_do_not_change_successful_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    root = tmp_path / "traces"
    if failure == "directory":
        root.write_text("not a directory")
    else:
        original = Path.write_text

        def failing_write(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
            should_fail = path.parent.parent == root and path.name == failure
            if failure == "response.txt":
                should_fail = should_fail and bool(data)
            if failure == "metadata.json":
                should_fail = should_fail and '"status": "completed"' in data
            if should_fail:
                raise OSError("private disk diagnostic")
            return original(path, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_write)

    def respond(_request: httpx.Request) -> httpx.Response:
        return stream_response([complete()])

    mock_transport(monkeypatch, respond)
    assert ResponsesClient(Login(), root).propose("model", {}) == {"categories": []}
    assert "LLM trace write failed" in caplog.text
    assert "private disk diagnostic" not in caplog.text


def test_timestamped_directories_sort_and_do_not_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 10, 13, 14, 35, 2, 123456, tzinfo=UTC)
    times = iter(
        [
            start,
            start + timedelta(seconds=1),
            start + timedelta(seconds=2),
            start + timedelta(seconds=3),
            start + timedelta(seconds=2),
            start + timedelta(seconds=3),
        ]
    )

    class Clock:
        @staticmethod
        def now(zone: tzinfo) -> datetime:
            return next(times).astimezone(zone)

    monkeypatch.setattr(llm_traces, "datetime", Clock)

    def respond(_request: httpx.Request) -> httpx.Response:
        return stream_response([complete()])

    mock_transport(monkeypatch, respond)
    client = ResponsesClient(Login(), tmp_path)
    for _ in range(3):
        client.propose("model", {})
    directories = sorted(tmp_path.iterdir())
    assert len(directories) == 3
    assert directories[0].name.startswith("2026-10-13T14-35-02.123456Z_propose_")
    assert all(
        directory.name.startswith("2026-10-13T14-35-04.123456Z_propose_")
        for directory in directories[1:]
    )
    assert all(":" not in directory.name for directory in directories)
    assert (
        len({json.loads((d / "metadata.json").read_text())["request_id"] for d in directories}) == 3
    )


def test_classification_traces_include_run_batches_and_proposals_have_no_run_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fyntok import categorization

    monkeypatch.setattr(categorization, "BATCH_SIZE", 1)
    engine = open_database(tmp_path / "sample.sqlite3")
    root = tmp_path / "traces"

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = json.loads(body["input"][0]["content"])
        if "transactions" in payload:
            result = {
                "assignments": [
                    {
                        "transaction_id": t["transaction_id"],
                        "category_id": "uncategorized",
                        "reason": "Mock result",
                    }
                    for t in payload["transactions"]
                ]
            }
        else:
            result = {"categories": []}
        return stream_response([complete(json.dumps(result))])

    mock_transport(monkeypatch, respond)
    try:
        import_content(
            engine,
            b"a;Shop;01.01.2024;01.01.2024;-1,00;EUR\na;Cafe;02.01.2024;02.01.2024;-2,00;EUR\n",
            "sample.csv",
        )
        client = ResponsesClient(Login(), root)
        run_id = start_run(engine, "model")
        assert execute_run(engine, client, run_id)["applied"] == 2
        assert propose_categories(engine, client, "model") == []
    finally:
        engine.dispose()
    metadata = [json.loads((d / "metadata.json").read_text()) for d in sorted(root.iterdir())]
    assert [(m["run_id"], m["batch_number"], m["operation"]) for m in metadata] == [
        (run_id, 1, "classify"),
        (run_id, 2, "classify"),
        (None, None, "propose"),
    ]


@pytest.mark.parametrize("tracing", [False, True])
@pytest.mark.parametrize("log_level", ["info", "debug"])
def test_cli_passes_explicit_trace_option_to_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracing: bool, log_level: str
) -> None:
    from fyntok import app

    captured: dict[str, Any] = {}

    def serve(_engine: Engine, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(app, "serve", serve)
    # Restore the application logger's level after CLI configuration changes it.
    monkeypatch.setattr(logging.getLogger("fyntok"), "level", logging.getLogger("fyntok").level)
    argv = [
        "fyntok",
        "--database",
        str(tmp_path / "sample.sqlite3"),
        "serve",
        "--log-level",
        log_level,
    ]
    if tracing:
        argv.append("--trace-llm")
    monkeypatch.setattr("sys.argv", argv)
    cli.main()
    assert captured["trace_dir"] == (Path("data/llm-traces") if tracing else None)
    assert captured["log_level"] == log_level
