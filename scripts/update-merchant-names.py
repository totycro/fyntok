"""One-off recomputation of stored merchant names using the shared description parser."""

import argparse
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from fyntok.descriptions import parse_description


def update_merchants(database: Path, *, dry_run: bool = False) -> tuple[int, int, Path | None]:
    """Back up and atomically update merchant names; leave other transaction data intact."""
    path = database.resolve()
    mode = "ro" if dry_run else "rw"
    backup = None
    with closing(sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)) as db:
        versions = db.execute("SELECT version FROM schema_version").fetchall()
        if versions not in ([(1,)], [(2,)]):
            raise ValueError("Unsupported database schema version")
        if not dry_run:
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
            backup = path.with_name(f"{path.stem}.merchant-backup-{timestamp}.sqlite3")
            with backup.open("xb"):
                pass
            with closing(sqlite3.connect(backup)) as copy:
                db.backup(copy)
        with db:
            db.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id, description, account, merchant FROM transactions ORDER BY id"
            ).fetchall()
            changes = []
            for transaction_id, description, account, merchant in rows:
                name = parse_description(description, account).merchant_name
                if name != merchant:
                    changes.append((name, transaction_id))
            if not dry_run:
                db.executemany("UPDATE transactions SET merchant = ? WHERE id = ?", changes)
    return len(rows), len(changes), backup


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recompute stored merchant names with the current parser. Stop the app first.",
        allow_abbrev=False,
    )
    parser.add_argument("--database", type=Path, required=True, help="Existing SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Count changes without writing")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error(f"Database not found: {args.database}")
    try:
        total, changed, backup = update_merchants(args.database, dry_run=args.dry_run)
    except (sqlite3.Error, ValueError, OSError) as error:
        parser.exit(1, f"Update failed: {error}\n")
    if backup is not None:
        print(f"Backup: {backup}")
    action = "Would update" if args.dry_run else "Updated"
    print(f"{action} {changed} of {total} merchant names.")


if __name__ == "__main__":
    main()
