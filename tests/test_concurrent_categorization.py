"""Bounded concurrent calls with coordinator-owned commits and durable recovery."""

import queue
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, override

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from fyntok import categorization
from fyntok.categorization import cancel_run, execute_run, resume_run, start_run
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import correct_transaction, save_category
from fyntok.models import RunItem
from fyntok.queries import transaction_rows


@pytest.fixture
def bank(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "concurrent.sqlite3")
    # Six batches at the production size: five of 25 and a final batch of one.
    import_content(
        engine,
        "".join(f"account;Shop {i};01.01.2024;01.01.2024;-1,00;EUR\n" for i in range(126)).encode(),
        "synthetic.csv",
    )
    yield engine
    engine.dispose()


class ControlledAdvisor:
    def __init__(self, *, failure: str | None = None) -> None:
        self.started: queue.Queue[int] = queue.Queue()
        self.release = [threading.Event() for _ in range(6)]
        self.lock = threading.Lock()
        self.targets: list[list[str]] = []
        self.request_threads: set[int] = set()
        self.failure = failure

    def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert model == "saved-model"
        with self.lock:
            index = len(self.targets)
            self.targets.append([t["transaction_id"] for t in payload["transactions"]])
            self.request_threads.add(threading.get_ident())
        self.started.put(index)
        assert self.release[index].wait(10)
        if index == 1 and self.failure == "transport":
            raise RuntimeError("private transport details")
        return {
            "assignments": [
                {
                    "transaction_id": t["transaction_id"],
                    "category_id": "invented"
                    if index == 1 and self.failure == "validation"
                    else "uncategorized",
                    "reason": "Synthetic result",
                }
                for t in payload["transactions"]
            ]
        }

    def release_all(self) -> None:
        for release in self.release:
            release.set()

    def first_four(self) -> None:
        assert {self.started.get(timeout=5) for _ in range(4)} == {0, 1, 2, 3}
        # All four calls remain blocked, so no fifth request can have started.
        assert self.started.empty()
        assert len(self.targets) == 4


@pytest.fixture
def draining(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Observe shutdown before blocked calls finish, without timing assumptions."""
    entered = threading.Event()

    class ObservedPool(ThreadPoolExecutor):
        @override
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            entered.set()
            super().shutdown(wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(categorization, "ThreadPoolExecutor", ObservedPool)
    return entered


def test_four_calls_refill_out_of_order_and_commit_only_on_coordinator(bank: Engine) -> None:
    advisor = ControlledAdvisor()
    run_id = start_run(bank, "saved-model")
    callback_threads: list[int] = []
    database_threads: set[int] = set()
    coordinator_threads: list[int] = []
    first_commit = threading.Event()

    @event.listens_for(bank, "before_cursor_execute")
    def track_database(
        _connection: Any,
        _cursor: Any,
        _statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        database_threads.add(threading.get_ident())

    def progress(value: dict[str, Any]) -> None:
        callback_threads.append(threading.get_ident())
        if value["applied"] == 25:
            first_commit.set()

    def execute() -> dict[str, Any]:
        coordinator_threads.append(threading.get_ident())
        return execute_run(bank, advisor, run_id, progress)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        try:
            advisor.first_four()
            # Complete a later batch while the first and two peers stay blocked.
            advisor.release[2].set()
            assert first_commit.wait(5)
            assert advisor.started.get(timeout=5) == 4
            assert not future.done()
            assert advisor.started.empty()
            assert len(advisor.targets) == 5
        finally:
            advisor.release_all()
        result = future.result(timeout=10)
    assert result["status"] == "completed" and result["applied"] == 126
    assert set(callback_threads) == set(coordinator_threads)
    assert database_threads == set(coordinator_threads)
    assert len(advisor.request_threads) == 4
    assert not database_threads & advisor.request_threads
    assert sorted(map(len, advisor.targets)) == [1, 25, 25, 25, 25, 25]
    ids = [transaction_id for batch in advisor.targets for transaction_id in batch]
    assert len(ids) == len(set(ids)) == 126
    with Session(bank) as session:
        assert all(i.attempts == 1 and i.result for i in session.scalars(select(RunItem)))


def test_cancel_discards_four_calls_and_resume_waits_for_shutdown(
    bank: Engine, draining: threading.Event
) -> None:
    advisor = ControlledAdvisor()
    run_id = start_run(bank, "saved-model")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_run, bank, advisor, run_id)
        try:
            advisor.first_four()
            assert cancel_run(bank, run_id)["status"] == "cancelled"
            assert draining.wait(5)
            with pytest.raises(ValueError, match="still active"):
                resume_run(bank, advisor, run_id)
            assert not future.done()
            assert len(advisor.targets) == 4
        finally:
            advisor.release_all()
        result = future.result(timeout=10)
    assert result["status"] == "cancelled" and result["applied"] == 0
    with Session(bank) as session:
        assert {i.status for i in session.scalars(select(RunItem))} == {"pending"}
    resumed = ControlledAdvisor()
    resumed.release_all()
    assert resume_run(bank, resumed, run_id)["applied"] == 126
    with Session(bank) as session:
        assert sorted(i.attempts for i in session.scalars(select(RunItem))) == [1] * 26 + [2] * 100


@pytest.mark.parametrize("failure", ["transport", "validation"])
def test_failure_preserves_commit_discards_peers_and_resumes(
    bank: Engine, draining: threading.Event, failure: str
) -> None:
    advisor = ControlledAdvisor(failure=failure)
    run_id = start_run(bank, "saved-model")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_run, bank, advisor, run_id)
        try:
            advisor.first_four()
            advisor.release[0].set()
            assert advisor.started.get(timeout=5) == 4  # First batch is now committed.
            advisor.release[1].set()
            assert draining.wait(5)  # Coordinator observed the failure before peers finish.
            assert len(advisor.targets) == 5
            assert not future.done()
            with pytest.raises(ValueError, match="still active"):
                resume_run(bank, advisor, run_id)
        finally:
            advisor.release_all()
        result = future.result(timeout=10)
    assert result["status"] == "failed" and result["applied"] == 25
    assert "private" not in result["error"]
    with Session(bank) as session:
        committed = {
            i.transaction_id: i.applied_assignment_id
            for i in session.scalars(select(RunItem))
            if i.status == "applied"
        }
        assert sorted(i.status for i in session.scalars(select(RunItem))) == (
            ["applied"] * 25 + ["failed"] * 100 + ["pending"]
        )
    resumed = ControlledAdvisor()
    resumed.release_all()
    assert resume_run(bank, resumed, run_id)["applied"] == 126
    assert not set(committed) & {tid for batch in resumed.targets for tid in batch}
    with Session(bank) as session:
        assert all(
            i.applied_assignment_id == committed[i.transaction_id]
            for i in session.scalars(select(RunItem))
            if i.transaction_id in committed
        )


@pytest.mark.parametrize("edit", ["manual", "definition"])
def test_edits_during_four_calls_exclude_stale_results(bank: Engine, edit: str) -> None:
    advisor = ControlledAdvisor()
    run_id = start_run(bank, "saved-model")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_run, bank, advisor, run_id)
        try:
            advisor.first_four()
            if edit == "manual":
                correct_transaction(bank, advisor.targets[0][0], "uncategorized")
            else:
                save_category(bank, "Food", "Groceries")
        finally:
            advisor.release_all()
        result = future.result(timeout=10)
    assert result["skipped"] == 126 and result["applied"] == 0
    if edit == "manual":
        row = next(r for r in transaction_rows(bank) if r["id"] == advisor.targets[0][0])
        assert row["protected"]


def test_process_death_recovers_multiple_inflight_batches(bank: Engine) -> None:
    run_id = start_run(bank, "saved-model")
    script = """import os, sys, threading
from fyntok.database import open_database
from fyntok.categorization import execute_run
barrier = threading.Barrier(4)
lock = threading.Lock()
class Crash:
    calls = 0
    def classify(self, model, payload):
        with lock:
            index = self.calls
            self.calls += 1
        barrier.wait(timeout=10)
        if index != 0:
            threading.Event().wait(30)
        return {"assignments": [dict(transaction_id=t["transaction_id"],
            category_id="uncategorized", reason="Synthetic") for t in payload["transactions"]]}
def committed(progress):
    if progress["applied"] == 25:
        os._exit(17)
execute_run(open_database(sys.argv[1]), Crash(), sys.argv[2], committed)
"""
    child = subprocess.run(  # noqa: S603 - Fixed local script and synthetic test database.
        [sys.executable, "-c", script, str(bank.url.database), run_id], timeout=20, check=False
    )
    assert child.returncode == 17
    with Session(bank) as session:
        assert sorted(i.status for i in session.scalars(select(RunItem))) == (
            ["applied"] * 25 + ["pending"] * 26 + ["running"] * 75
        )
    advisor = ControlledAdvisor()
    advisor.release_all()
    assert resume_run(bank, advisor, run_id)["applied"] == 126
    with Session(bank) as session:
        assert sorted(i.attempts for i in session.scalars(select(RunItem))) == [1] * 51 + [2] * 75


@pytest.mark.parametrize("protected", [False, True])
def test_empty_or_fully_protected_run_sends_no_requests(tmp_path: Path, protected: bool) -> None:
    engine = open_database(tmp_path / "empty.sqlite3")
    try:
        if protected:
            import_content(engine, b"a;Shop;01.01.2024;01.01.2024;-1,00;EUR\n", "synthetic.csv")
            correct_transaction(engine, transaction_rows(engine)[0]["id"], "uncategorized")
        advisor = ControlledAdvisor()
        result = execute_run(engine, advisor, start_run(engine, "saved-model"))
        assert result["status"] == "completed" and result["applied"] == 0
        assert result["skipped"] == int(protected)
        assert advisor.targets == []
    finally:
        engine.dispose()
