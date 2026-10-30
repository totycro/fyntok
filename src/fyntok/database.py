from pathlib import Path
from sqlite3 import Connection

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry

from .accounts import reconcile_repayments, register_accounts
from .models import Base, Category, SchemaVersion, Transaction

UNCATEGORIZED_ID = "uncategorized"


def open_database(path: str | Path = "data/fyntok.sqlite3") -> Engine:
    """Open or initialize SQLite with foreign keys and durable writes enabled."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite", database=str(path)))

    @event.listens_for(engine, "connect")
    def configure(connection: Connection, _: ConnectionPoolEntry) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")

    try:
        _initialize_database(engine)
    except BaseException:
        engine.dispose()
        raise
    return engine


def _initialize_database(engine: Engine) -> None:
    # Refuse unknown versions before making schema changes.
    with engine.connect() as connection:
        exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).scalar()
        versions = (
            connection.exec_driver_sql("SELECT version FROM schema_version").all() if exists else []
        )
        if exists and versions not in [[(1,)], [(2,)], [(3,)]]:
            raise ValueError("Unsupported database schema version; migration required")
    if versions == [(1,)]:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            # Another process may have migrated while we waited for the write lock.
            if connection.exec_driver_sql("SELECT version FROM schema_version").scalar() == 1:
                connection.exec_driver_sql(
                    "ALTER TABLE categories ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0"
                )
                connection.exec_driver_sql(
                    "ALTER TABLE assignments ADD COLUMN protected BOOLEAN NOT NULL DEFAULT 0"
                )
                connection.exec_driver_sql(
                    "ALTER TABLE assignments ADD COLUMN restored_assignment_id "
                    "VARCHAR REFERENCES assignments(id)"
                )
                connection.exec_driver_sql(
                    "UPDATE assignments SET protected = 1 WHERE source = 'manual'"
                )
                connection.exec_driver_sql("UPDATE schema_version SET version = 2")
    with engine.begin() as connection:
        # Include SQLite DDL and backfill in one transaction. Recheck the version
        # under the write lock in case another process completed the migration.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(connection)
        with Session(connection) as session, session.begin():
            version = session.scalars(select(SchemaVersion)).first()
            if version is None or version.version != 3:
                register_accounts(
                    session,
                    [
                        {"account": t.account, "description": t.description}
                        for t in session.scalars(select(Transaction))
                    ],
                )
                reconcile_repayments(session)
                if version is None:
                    session.add(SchemaVersion(version=3))
                else:
                    version.version = 3
            if session.get(Category, UNCATEGORIZED_ID) is None:
                session.add(
                    Category(
                        id=UNCATEGORIZED_ID,
                        name="Uncategorized",
                        kind="uncategorized",
                        definition="Transactions awaiting a category.",
                    )
                )


def require_record[T](record: T | None) -> T:
    """Reject missing records before accessing their persisted fields."""
    if record is None:
        raise ValueError("Database record does not exist.")
    return record
