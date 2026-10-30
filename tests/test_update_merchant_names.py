"""One-off merchant updates preserve transaction data and manual decisions."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import correct_transaction, save_category
from fyntok.queries import transaction_rows

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update-merchant-names.py"


def run_script(database: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — Fixed local script; arguments passed without a shell.
        [sys.executable, str(SCRIPT), "--database", str(database), *flags],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "sample.sqlite3"
    engine = open_database(path)
    try:
        content = (
            r"a;Bank | EXAMPLE CARE-FIL. A112\\WIEN\1020 W;01.01.2024;01.01.2024;-1,00;EUR" + "\n"
            r"a;Bank | EXAMPLE24 GMBH\\DRESDEN\01237|SPESEN: 0,00;02.01.2024;02.01.2024;-2,00;EUR"
            "\n"
        ).encode()
        import_content(engine, content, "sample.csv")
        category = save_category(engine, "Shopping", "Purchases")
        correct_transaction(engine, transaction_rows(engine)[0]["id"], category)
    finally:
        engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE transactions SET merchant = description")
    return path


def test_update_preserves_other_fields_history_and_backup(database: Path) -> None:
    with sqlite3.connect(database) as db:
        original_dump = list(db.iterdump())
        db.row_factory = sqlite3.Row
        originals = [dict(row) for row in db.execute("SELECT * FROM transactions ORDER BY id")]
        tables = (
            "assignments",
            "confirmed_examples",
            "categories",
            "imports",
            "import_rows",
            "categorization_runs",
            "run_items",
            "schema_version",
            "accounts",
            "card_repayments",
        )
        preserved = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]  # noqa: S608 — Fixed table names.
            for table in tables
        }

    result = run_script(database)
    assert result.returncode == 0, result.stderr
    assert "Updated 2 of 2 merchant names." in result.stdout
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        updated = [dict(row) for row in db.execute("SELECT * FROM transactions ORDER BY id")]
        assert {row["merchant"] for row in updated} == {"EXAMPLE CARE", "EXAMPLE24 GMBH"}
        for original, current in zip(originals, updated, strict=True):
            del original["merchant"], current["merchant"]
            assert current == original
        for table, expected in preserved.items():
            # Table names are fixed test constants, never user input.
            assert [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] == expected  # noqa: S608
    backups = list(database.parent.glob("*.merchant-backup-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert list(db.iterdump()) == original_dump
    rerun = run_script(database)
    assert rerun.returncode == 0, rerun.stderr
    assert "Updated 0 of 2 merchant names." in rerun.stdout


def test_dry_run_does_not_write_or_create_a_backup(database: Path) -> None:
    with sqlite3.connect(database) as db:
        original = list(db.iterdump())
    result = run_script(database, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Would update 2 of 2 merchant names." in result.stdout
    with sqlite3.connect(database) as db:
        assert list(db.iterdump()) == original
    assert list(database.parent.glob("*.merchant-backup-*.sqlite3")) == []


def test_failed_update_rolls_back_every_change(database: Path) -> None:
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TRIGGER reject_update BEFORE UPDATE OF merchant ON transactions "
            "WHEN OLD.id = (SELECT MAX(id) FROM transactions) "
            "BEGIN SELECT RAISE(ABORT, 'test update failure'); END"
        )
        original = db.execute("SELECT id, merchant FROM transactions ORDER BY id").fetchall()
    result = run_script(database)
    assert result.returncode == 1
    assert "test update failure" in result.stderr
    with sqlite3.connect(database) as db:
        assert (
            db.execute("SELECT id, merchant FROM transactions ORDER BY id").fetchall() == original
        )


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    result = run_script(path)
    assert result.returncode == 2
    assert "Database not found" in result.stderr
    assert not path.exists()


def test_unsupported_schema_is_not_modified(database: Path) -> None:
    with sqlite3.connect(database) as db:
        db.execute("UPDATE schema_version SET version = 99")
        original = list(db.iterdump())
    result = run_script(database)
    assert result.returncode == 1
    assert "Unsupported database schema version" in result.stderr
    with sqlite3.connect(database) as db:
        assert list(db.iterdump()) == original
    assert list(database.parent.glob("*.merchant-backup-*.sqlite3")) == []
