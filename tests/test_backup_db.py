import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup-db.py"


@pytest.mark.parametrize("custom_output", [False, True])
def test_backup_includes_live_wal_and_preserves_previous_backups(
    tmp_path: Path, custom_output: bool
) -> None:
    database = tmp_path / "live.sqlite3"
    output = tmp_path / "backups" if custom_output else tmp_path
    args = ["--output-dir", str(output)] if custom_output else []
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES ('committed in WAL')")
        db.commit()
        assert Path(f"{database}-wal").stat().st_size > 0
        for _ in range(2):
            result = subprocess.run(  # noqa: S603 - Fixed local script and temporary database.
                [sys.executable, str(SCRIPT), "--database", str(database), *args],
                capture_output=True,
                text=True,
                check=True,
            )
            assert "Integrity check: ok" in result.stdout
        backups = list(output.glob("live.backup-*.sqlite3"))
        assert len(backups) == 2
        for backup in backups:
            with closing(sqlite3.connect(backup)) as copy:
                assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                assert copy.execute("SELECT * FROM sample").fetchall() == [("committed in WAL",)]
        assert db.execute("SELECT * FROM sample").fetchall() == [("committed in WAL",)]


def test_missing_source_is_not_created(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    result = subprocess.run(  # noqa: S603 - Fixed local script and temporary database path.
        [sys.executable, str(SCRIPT), "--database", str(database)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Database not found" in result.stderr
    assert not database.exists()
    assert not list(tmp_path.glob("*.backup-*.sqlite3"))
