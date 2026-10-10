import sqlite3
import subprocess
import sys
from pathlib import Path

from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import correct_transaction, save_category, undo_assignment
from fyntok.queries import transaction_rows

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clear-transactions.py"


def run_script(database: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — Fixed local script; arguments are passed without a shell.
        [sys.executable, str(SCRIPT), "--database", str(database), *flags],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clear_preserves_categories_and_backs_up_history(tmp_path: Path) -> None:
    path = tmp_path / "sample.sqlite3"
    engine = open_database(path)
    import_content(engine, b"a;Shop;01.01.2024;02.01.2024;-10,00;EUR\n", "test.csv")
    category = save_category(engine, "Food", "Groceries")
    tid = transaction_rows(engine)[0]["id"]
    correct_transaction(engine, tid, category)
    correct_transaction(engine, tid, "uncategorized")
    undo_assignment(engine, tid)
    engine.dispose()
    with sqlite3.connect(path) as db:
        categories = db.execute("SELECT * FROM categories ORDER BY id").fetchall()
        assignments = db.execute("SELECT * FROM assignments ORDER BY id").fetchall()

    result = run_script(path, "--dangerously-delete-all-transactions")

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM categories ORDER BY id").fetchall() == categories
        for table in (
            "transactions",
            "imports",
            "import_rows",
            "assignments",
            "confirmed_examples",
            "run_items",
            "categorization_runs",
        ):
            # Table names are fixed test constants, never user input.
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)  # noqa: S608
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    backups = list(tmp_path.glob("sample.backup-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT COUNT(*) FROM transactions").fetchone() == (1,)
        assert db.execute("SELECT * FROM assignments ORDER BY id").fetchall() == assignments


def test_full_danger_flag_is_required_before_database_access(tmp_path: Path) -> None:
    path = tmp_path / "sample.sqlite3"
    path.write_bytes(b"untouched")
    for flags in ((), ("--dangerously-delete-all",)):
        result = run_script(path, *flags)
        assert result.returncode == 2
        assert path.read_bytes() == b"untouched"
        assert list(tmp_path.glob("*.backup-*.sqlite3")) == []


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    result = run_script(path, "--dangerously-delete-all-transactions")
    assert result.returncode == 2
    assert "Database not found" in result.stderr
    assert not path.exists()
