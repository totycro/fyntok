from pathlib import Path
from sqlite3 import Connection

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry

from .models import Base, Category, SchemaVersion

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

    # Refuse unknown versions before making schema changes.
    with engine.connect() as connection:
        exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).scalar()
        versions = (
            connection.exec_driver_sql("SELECT version FROM schema_version").all() if exists else []
        )
        if exists and versions not in [[(1,)], [(2,)]]:
            engine.dispose()
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
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        if session.get(SchemaVersion, 2) is None:
            session.add(SchemaVersion(version=2))
        if session.get(Category, UNCATEGORIZED_ID) is None:
            session.add(
                Category(
                    id=UNCATEGORIZED_ID,
                    name="Uncategorized",
                    kind="uncategorized",
                    definition="Transactions awaiting a category.",
                )
            )
    return engine


def require_record[T](record: T | None) -> T:
    """Reject missing records before accessing their persisted fields."""
    if record is None:
        raise ValueError("Database record does not exist.")
    return record
