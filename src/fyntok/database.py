from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import Session

from .models import Base, Category, SchemaVersion


UNCATEGORIZED_ID = "uncategorized"


def open_database(path: str | Path = "data/fyntok.sqlite3") -> Engine:
    """Open or initialize SQLite with foreign keys and durable writes enabled."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite", database=str(path)))

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")

    # Refuse unknown versions before making schema changes.
    with engine.connect() as connection:
        exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).scalar()
        if exists and connection.exec_driver_sql("SELECT version FROM schema_version").all() != [(1,)]:
            engine.dispose()
            raise ValueError("Unsupported database schema version; migration required")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        if session.get(SchemaVersion, 1) is None:
            session.add(SchemaVersion(version=1))
        if session.get(Category, UNCATEGORIZED_ID) is None:
            session.add(Category(id=UNCATEGORIZED_ID, name="Uncategorized", kind="uncategorized",
                                 definition="Transactions awaiting a category."))
    return engine
