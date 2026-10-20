"""Recovery contracts: real worker exclusion, durable cancellation, and no replay."""

import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, override

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from fyntok import categorization
from fyntok.categorization import cancel_run, execute_run, resume_run, run_progress, start_run
from fyntok.database import open_database, require_record
from fyntok.importer import import_content
from fyntok.manual import correct_transaction, save_category, undo_assignment
from fyntok.models import Assignment, RunItem
from fyntok.queries import transaction_rows


class Advisor:
    def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        del model
        return {
            "assignments": [
                {
                    "transaction_id": t["transaction_id"],
                    "category_id": "uncategorized",
                    "reason": "Unclear",
                }
                for t in payload["transactions"]
            ]
        }


@pytest.fixture
def bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    monkeypatch.setattr(categorization, "BATCH_SIZE", 1)
    # These tests isolate serial recovery contracts; concurrent cases have their own fixture.
    monkeypatch.setattr(categorization, "MAX_CONCURRENT_REQUESTS", 1)
    engine = open_database(tmp_path / "reliability.sqlite3")
    import_content(
        engine,
        b"a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\na;EXAMPLE CARE;02.01.2024;02.01.2024;-20,00;EUR\n",
        "bank.csv",
    )
    yield engine
    engine.dispose()


def assignment_count(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Assignment)) or 0


def test_cancel_inflight_discard_and_live_worker_cannot_be_recovered(
    bank: Engine,
) -> None:
    entered, release = threading.Event(), threading.Event()

    class Waiting(Advisor):
        @override
        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            entered.set()
            assert release.wait(5)
            return super().classify(model, payload)

    run_id = start_run(bank, "original-model")
    with ThreadPoolExecutor() as pool:
        future = pool.submit(execute_run, bank, Waiting(), run_id)
        try:
            assert entered.wait(5)
            cancel_run(bank, run_id)
            with pytest.raises(ValueError, match="still active"):
                resume_run(bank, Advisor(), run_id)
            # A separate process opening the same database also respects the lock.
            script = (
                "from fyntok.database import open_database, require_record; from "
                "fyntok.categorization import worker_lock; import sys; "
                "e=open_database(sys.argv[1]);\nwith worker_lock(e): pass"
            )
            child = subprocess.run(  # noqa: S603 - Fixed local script and test database arguments.
                [sys.executable, "-c", script, str(bank.url.database)],
                capture_output=True,
                text=True,
            )
            assert child.returncode != 0 and "still active" in child.stderr
        finally:
            release.set()
        assert future.result(timeout=5)["status"] == "cancelled"
    assert assignment_count(bank) == 0
    assert resume_run(bank, Advisor(), run_id)["applied"] == 2
    assert assignment_count(bank) == 2
    with pytest.raises(ValueError, match="unfinished"):
        resume_run(bank, Advisor(), run_id)


def test_cancel_between_batches_resume_preserves_undo_and_applied_ids(
    bank: Engine,
) -> None:
    run_id = start_run(bank, "model")
    result = execute_run(
        bank, Advisor(), run_id, lambda p: cancel_run(bank, run_id) if p["applied"] == 1 else None
    )
    assert result["status"] == "cancelled" and result["applied"] == 1
    with Session(bank) as session:
        item = require_record(
            session.scalar(
                select(RunItem).where(RunItem.run_id == run_id, RunItem.status == "applied")
            )
        )
        transaction_id, assignment_id = item.transaction_id, item.applied_assignment_id
    undo_assignment(bank, transaction_id)
    assert resume_run(bank, Advisor(), run_id)["applied"] == 2
    with Session(bank) as session:
        assert (
            require_record(session.get(RunItem, (run_id, transaction_id))).applied_assignment_id
            == assignment_id
        )
    assert next(r for r in transaction_rows(bank) if r["id"] == transaction_id)["source"] == "undo"
    assert assignment_count(bank) == 3  # two AI records, one undo; no replay


def test_process_death_recovers_only_uncommitted_batch(
    bank: Engine,
) -> None:
    run_id = start_run(bank, "model")
    script = """import os, sys
from fyntok.database import open_database, require_record
from fyntok import categorization as c
c.BATCH_SIZE = 1
c.MAX_CONCURRENT_REQUESTS = 1
class Crash:
    calls = 0
    def classify(self, model, payload):
        self.calls += 1
        if self.calls == 2: os._exit(17)
        return {'assignments': [
            dict(transaction_id=t['transaction_id'],
                 category_id='uncategorized', reason='Unclear')
            for t in payload['transactions']
        ]}
c.execute_run(open_database(sys.argv[1]), Crash(), sys.argv[2])
"""
    child = subprocess.run(  # noqa: S603 - Fixed local script and test database arguments.
        [sys.executable, "-c", script, str(bank.url.database), run_id]
    )
    assert child.returncode == 17
    assert run_progress(bank, run_id)["status"] == "running"
    assert assignment_count(bank) == 1
    assert resume_run(bank, Advisor(), run_id)["applied"] == 2
    assert assignment_count(bank) == 2
    with Session(bank) as session:
        assert sorted(i.attempts for i in session.scalars(select(RunItem))) == [1, 2]


def test_failed_resume_and_manual_edit_exclude_stale_results(
    bank: Engine,
) -> None:
    class Fail(Advisor):
        calls = 0

        @override
        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("private response body")
            return super().classify(model, payload)

    run_id = start_run(bank, "model")
    assert execute_run(bank, Fail(), run_id)["applied"] == 1
    pending = next(r for r in transaction_rows(bank) if r["source"] != "ai")
    correct_transaction(bank, pending["id"], "uncategorized")
    result = resume_run(bank, Advisor(), run_id)
    assert result["status"] == "completed" and result["applied"] == 1 and result["skipped"] == 1
    assert "private" not in (result["error"] or "")
    assert next(r for r in transaction_rows(bank) if r["id"] == pending["id"])["protected"]


def test_category_change_while_cancelled_and_pending_cancel(
    bank: Engine,
) -> None:
    run_id = start_run(bank, "model")
    cancel_run(bank, run_id)
    assert execute_run(bank, Advisor(), run_id)["status"] == "cancelled"
    save_category(bank, "Food", "Groceries", "spending")
    result = resume_run(bank, Advisor(), run_id)
    assert result["skipped"] == 2 and assignment_count(bank) == 0
    old_id = start_run(bank, "model")
    cancel_run(bank, old_id)
    new_id = start_run(bank, "model")
    with pytest.raises(ValueError, match="other unfinished"):
        resume_run(bank, Advisor(), old_id)
    cancel_run(bank, new_id)


@pytest.mark.parametrize("edit", ["rename", "merge", "delete", "undo"])
def test_edits_during_request_and_undo_afterwards(bank: Engine, edit: str) -> None:
    from fyntok.manual import remove_category

    food = save_category(bank, "Food", "Groceries")
    destination = (
        save_category(bank, "Shopping", "Other purchases") if edit == "merge" else "uncategorized"
    )
    if edit == "undo":
        execute_run(bank, Advisor(), start_run(bank, "model"))
    target_id = transaction_rows(bank)[0]["id"]

    class Editing(Advisor):
        edited = False

        @override
        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            if not self.edited:
                self.edited = True
                if edit == "rename":
                    save_category(bank, "Groceries", "Groceries", category_id=food)
                elif edit in {"merge", "delete"}:
                    remove_category(bank, food, destination)
                else:
                    undo_assignment(bank, target_id)
            return super().classify(model, payload)

    result = execute_run(bank, Editing(), start_run(bank, "model"))
    if edit == "undo":
        assert next(r for r in transaction_rows(bank) if r["id"] == target_id)["source"] == "undo"
        assert result["skipped"] == 1
    else:
        assert result["skipped"] == 2 and assignment_count(bank) == 0


def test_deleting_example_during_request_skips_stale_guidance(bank: Engine) -> None:
    from fyntok.manual import delete_example
    from fyntok.models import CategorizationRun

    food = save_category(bank, "Food", "Groceries")
    tid = transaction_rows(bank)[0]["id"]
    assignment_id = correct_transaction(bank, tid, food)
    run_id = start_run(bank, "model")

    class Deleting(Advisor):
        @override
        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert [e["example_id"] for e in payload["confirmed_examples"]] == [tid]
            delete_example(bank, tid, expected_assignment_id=assignment_id)
            return super().classify(model, payload)

    result = execute_run(bank, Deleting(), run_id)
    assert result["applied"] == 0 and result["skipped"] == 2
    assert assignment_count(bank) == 1
    fresh_id = start_run(bank, "model")
    with Session(bank) as session:
        assert require_record(session.get(CategorizationRun, run_id)).example_snapshot
        assert require_record(session.get(CategorizationRun, fresh_id)).example_snapshot == []
    assert execute_run(bank, Advisor(), fresh_id)["applied"] == 1
    manual = next(row for row in transaction_rows(bank) if row["id"] == tid)
    assert manual["protected"] and manual["category_id"] == food
