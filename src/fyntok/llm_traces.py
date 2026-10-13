"""Opt-in, best-effort request traces, separate from normal application logs."""

import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)
_batch: ContextVar[tuple[str, int] | None] = ContextVar("llm_trace_batch", default=None)


@contextmanager
def trace_batch(run_id: str, batch_number: int) -> Generator[None]:
    """Attach run context without changing the replaceable AI client protocols."""
    token = _batch.set((run_id, batch_number))
    try:
        yield
    finally:
        _batch.reset(token)


class RequestTrace:
    def __init__(self, root: Path | None, operation: str, body: dict[str, Any]) -> None:
        self.directory: Path | None = None
        self.response_text = ""
        self.http_status: int | None = None
        self.stream_status: str | None = None
        self.started = monotonic()
        self.metadata: dict[str, Any] = {}
        if root is None:
            return
        now = datetime.now(UTC)
        request_id = uuid4().hex
        batch = _batch.get()
        self.metadata = {
            "request_id": request_id,
            "operation": operation,
            "model": body["model"],
            "started_at": now.isoformat(),
            "finished_at": None,
            "duration_seconds": None,
            "status": "running",
            "http_status": None,
            "stream_status": None,
            "exception_type": None,
            "run_id": batch[0] if batch else None,
            "batch_number": batch[1] if batch else None,
        }
        directory = root / f"{now:%Y-%m-%dT%H-%M-%S.%fZ}_{operation}_{request_id}"
        try:
            directory.mkdir(parents=True, exist_ok=False)
            self.directory = directory
            self._write("request.json", json.dumps(body, ensure_ascii=False, indent=2) + "\n")
            self._write("response.txt", "")
            self._write_metadata()
        except (OSError, ValueError, TypeError) as error:
            self._disable(directory, error)
        if self.directory is not None:
            logger.info(
                "LLM trace %s: operation=%s, run=%s, batch=%s; directory=%s",
                request_id,
                operation,
                self.metadata["run_id"],
                self.metadata["batch_number"],
                directory.resolve(),
            )

    def _disable(self, directory: Path, error: Exception) -> None:
        self.directory = None
        logger.warning("LLM trace write failed at %s (%s)", directory, type(error).__name__)

    def _write(self, filename: str, text: str) -> None:
        if self.directory is None:
            return
        try:
            (self.directory / filename).write_text(text, encoding="utf-8")
        except (OSError, UnicodeError) as error:
            self._disable(self.directory, error)

    def _write_metadata(self) -> None:
        self._write("metadata.json", json.dumps(self.metadata, ensure_ascii=False, indent=2) + "\n")

    def write_response(self) -> None:
        self._write("response.txt", self.response_text)

    def finish(self, error: BaseException | None) -> None:
        if self.directory is None:
            return
        self.metadata.update(
            finished_at=datetime.now(UTC).isoformat(),
            duration_seconds=monotonic() - self.started,
            status="completed" if error is None else "failed",
            http_status=self.http_status,
            stream_status=self.stream_status,
            exception_type=type(error).__name__ if error else None,
        )
        self.write_response()
        self._write_metadata()


@contextmanager
def trace_request(
    root: Path | None, operation: str, body: dict[str, Any]
) -> Generator[RequestTrace]:
    trace = RequestTrace(root, operation, body)
    error: BaseException | None = None
    try:
        yield trace
    except BaseException as caught:
        error = caught
        raise
    finally:
        trace.finish(error)
