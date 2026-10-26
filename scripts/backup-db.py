"""Create and verify a consistent SQLite backup, including committed WAL data."""

import argparse
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def backup_database(database: Path, output_dir: Path | None = None) -> Path:
    """Back up an existing database without stopping the app or overwriting files."""
    source = database.resolve()
    directory = output_dir.resolve() if output_dir is not None else source.parent
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    backup = directory / f"{source.stem}.backup-{timestamp}.sqlite3"
    # Read-only mode refuses missing sources and includes the live WAL through SQLite.
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as db:
        directory.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(backup)) as copy:
                db.backup(copy, pages=256)
                results = copy.execute("PRAGMA integrity_check").fetchall()
                if results != [("ok",)]:
                    raise ValueError(f"Backup integrity check failed: {results}")
        except sqlite3.Error, OSError, ValueError:
            backup.unlink(missing_ok=True)
            raise
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a verified SQLite backup. The app can keep running.",
        allow_abbrev=False,
    )
    parser.add_argument("--database", type=Path, default=Path("data/fyntok.sqlite3"))
    parser.add_argument(
        "--output-dir", type=Path, help="Backup directory (default: beside the source database)"
    )
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error(f"Database not found: {args.database}")
    try:
        backup = backup_database(args.database, args.output_dir)
    except (sqlite3.Error, OSError, ValueError) as error:
        parser.exit(1, f"Backup failed: {error}\n")
    print(f"Backup: {backup}")
    print("Integrity check: ok")


if __name__ == "__main__":
    main()
