from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from fyntok.analysis import aggregate
from fyntok.database import open_database, require_record
from fyntok.importer import import_content
from fyntok.manual import (
    EditConflict,
    assignment_history,
    categories,
    correct_transaction,
    remove_category,
    save_category,
    undo_assignment,
)
from fyntok.models import Assignment, Category, ConfirmedExample, Transaction
from fyntok.queries import Filters, transaction_rows


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "manual.sqlite3")
    import_content(
        engine,
        b"a;Supermarket;01.01.2024;02.01.2024;-10,00;EUR\n"
        b"a;Supermarket;01.02.2024;02.02.2024;-20,00;EUR\n"
        b"a;Pay;01.02.2024;02.02.2024;50,00;EUR\n",
        "sample.csv",
    )
    yield engine
    engine.dispose()


def test_corrections_replace_examples_and_undo_preserves_manual_protection(engine: Engine) -> None:
    food = save_category(engine, "Food", "Groceries")
    shopping = save_category(engine, "Shopping", "Household")
    tid = transaction_rows(engine)[0]["id"]
    first = correct_transaction(engine, tid, food, expected_revision=0)
    second = correct_transaction(engine, tid, shopping, expected_revision=1)
    with Session(engine) as session:
        example = require_record(session.get(ConfirmedExample, tid))
        assert example.assignment_id == second and example.category_id == shopping
        assert require_record(session.get(Assignment, first)).active is False
        assert require_record(session.get(Assignment, second)).protected is True
        assert require_record(session.get(Transaction, tid)).revision == 2
        assert len(list(session.scalars(select(ConfirmedExample)))) == 1
    undo = undo_assignment(engine, tid, expected_revision=2)
    with Session(engine) as session:
        assert require_record(session.get(Assignment, undo)).category_id == food
        assert require_record(session.get(Assignment, undo)).protected is True
        assert require_record(session.get(ConfirmedExample, tid)).assignment_id == undo
        assert require_record(session.get(ConfirmedExample, tid)).category_id == food
    assert len(assignment_history(engine, tid)) == 3
    assert sum(r["protected"] for r in transaction_rows(engine)) == 1
    assert sum(r["category_id"] == "uncategorized" for r in transaction_rows(engine)) == 2
    with pytest.raises(EditConflict):
        correct_transaction(engine, tid, shopping, expected_revision=1)
    assert len(assignment_history(engine, tid)) == 3


def test_undo_to_ai_or_unassigned_removes_example(engine: Engine) -> None:
    food = save_category(engine, "Food", "")
    tid = transaction_rows(engine)[0]["id"]
    correct_transaction(engine, tid, food)
    undo_assignment(engine, tid)
    with Session(engine) as session:
        assert session.get(ConfirmedExample, tid) is None
    active = transaction_rows(engine)[0]
    assert active["category_id"] == "uncategorized" and not active["protected"]
    tid = transaction_rows(engine)[1]["id"]
    with Session(engine) as session, session.begin():
        session.add(Assignment(transaction_id=tid, category_id=food, source="ai"))
    correct_transaction(engine, tid, "uncategorized")
    aid = undo_assignment(engine, tid)
    with Session(engine) as session:
        assert require_record(session.get(Assignment, aid)).protected is False
        assert require_record(session.get(Assignment, aid)).category_id == food
        assert session.get(ConfirmedExample, tid) is None
    restored = next(r for r in transaction_rows(engine) if r["id"] == tid)
    assert restored["origin_source"] == "ai"


def test_category_lifecycle_preserves_history_moves_examples_and_totals(engine: Engine) -> None:
    food = save_category(engine, "Food", "Old definition")
    other = save_category(engine, "Other", "")
    ids = [r["id"] for r in transaction_rows(engine)]
    original = correct_transaction(engine, ids[0], food)
    with Session(engine) as session, session.begin():
        session.add(Assignment(transaction_id=ids[1], category_id=food, source="ai"))
    assert (
        save_category(engine, "Groceries", "New definition", category_id=food, expected_revision=0)
        == food
    )
    assert transaction_rows(engine)[0]["category"] == "Groceries"
    with pytest.raises(EditConflict):
        save_category(engine, "Stale", "", category_id=food, expected_revision=0)
    remove_category(engine, food, other, expected_revision=1)
    with Session(engine) as session:
        assert require_record(session.get(Category, food)).archived
        assert require_record(session.get(Assignment, original)).category_id == food
        assert require_record(session.get(ConfirmedExample, ids[0])).category_id == other
        assert session.get(ConfirmedExample, ids[1]) is None
    assert food not in {c["id"] for c in categories(engine)}
    moved = {r["id"]: r for r in transaction_rows(engine)}
    assert moved[ids[0]]["protected"] and not moved[ids[1]]["protected"]
    assert moved[ids[0]]["category_id"] == moved[ids[1]]["category_id"] == other
    before = assignment_history(engine, ids[0])
    with pytest.raises(ValueError, match="no longer exists"):
        undo_assignment(engine, ids[0])
    assert assignment_history(engine, ids[0]) == before
    remove_category(engine, other)  # Delete to Uncategorized, including confirmed examples.
    assert all(r["category_id"] == "uncategorized" for r in transaction_rows(engine))
    with Session(engine) as session:
        assert require_record(session.get(ConfirmedExample, ids[0])).category_id == "uncategorized"
    assert sum(r["net_cents"] for r in aggregate(engine)) == 2000
    assert all(r["bucket"] == "uncategorized" for r in aggregate(engine))
    with pytest.raises(ValueError):
        remove_category(engine, "uncategorized")


def test_invalid_category_changes_are_atomic(engine: Engine) -> None:
    food = save_category(engine, "Food", "")
    with pytest.raises(ValueError):
        save_category(engine, "food", "")
    with pytest.raises(ValueError):
        save_category(engine, " ", "")
    with pytest.raises(ValueError):
        save_category(engine, "Uncategorized", "")
    with pytest.raises(ValueError):
        save_category(engine, "Invalid", "", "uncategorized")
    tid = transaction_rows(engine)[0]["id"]
    correct_transaction(engine, tid, food)
    for dest in [food, "missing", None]:
        with pytest.raises(ValueError):
            remove_category(engine, food, dest)
    assert transaction_rows(engine)[0]["category_id"] == food
    with pytest.raises(ValueError):
        correct_transaction(engine, tid, "missing")
    assert len(assignment_history(engine, tid)) == 1


def test_table_and_aggregates_share_filters(engine: Engine) -> None:
    food = save_category(engine, "Food", "")
    transfer = save_category(engine, "Own accounts", "Between my accounts", "transfer")
    row = next(r for r in transaction_rows(engine) if r["booking_date"] == date(2024, 1, 1))
    correct_transaction(engine, row["id"], food)
    transfer_row = next(r for r in transaction_rows(engine) if r["description"] == "Pay")
    correct_transaction(engine, transfer_row["id"], transfer)
    for f in [
        Filters(start=date(2024, 2, 1)),
        Filters(end=date(2024, 1, 31)),
        Filters(category_id=food),
        Filters(category_id=transfer),
        Filters(merchant="Supermarket"),
        Filters(search="Pay"),
        Filters(source="manual"),
        Filters(source="unassigned"),
        Filters(currency="USD"),
        Filters(search="%"),
        Filters(
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            category_id=food,
            merchant="Supermarket",
            search="market",
            source="manual",
            currency="EUR",
        ),
    ]:
        rows = transaction_rows(engine, f)
        statistical_rows = [r for r in rows if r["category_id"] != transfer]
        for grouping in ["monthly", "trends", "merchant"]:
            totals = aggregate(engine, by=grouping, filters=f)
            assert sum(r["transaction_count"] for r in totals) == len(statistical_rows)
            assert sum(r["net_cents"] for r in totals) == sum(
                r["amount_cents"] for r in statistical_rows
            )
    assert len(transaction_rows(engine, Filters(category_id=transfer))) == 1
    with pytest.raises(ValueError):
        transaction_rows(engine, Filters(start=date(2025, 1, 1), end=date(2024, 1, 1)))


def test_schema_v1_migration_preserves_data_and_protects_manual_assignments(tmp_path: Path) -> None:
    # Recreate the prior schema using the retained metadata minus the new columns.
    from sqlalchemy import create_engine

    from fyntok.models import Base

    path = tmp_path / "v1.sqlite3"
    legacy = create_engine(f"sqlite:///{path}")
    with legacy.begin() as connection:
        for table in Base.metadata.sorted_tables:
            from sqlalchemy.schema import CreateTable

            ddl = str(CreateTable(table).compile(legacy))
            ddl = (
                ddl.replace("\tarchived BOOLEAN NOT NULL, \n", "")
                .replace("\tprotected BOOLEAN NOT NULL, \n", "")
                .replace("\trestored_assignment_id VARCHAR, \n", "")
            )
            ddl = ddl.replace(
                (
                    "\tFOREIGN KEY(restored_assignment_id, transaction_id) REFERENCES "
                    "assignments (id, transaction_id), \n"
                ),
                "",
            )
            connection.exec_driver_sql(ddl)
        connection.exec_driver_sql("INSERT INTO schema_version VALUES (1)")
        connection.exec_driver_sql(
            "INSERT INTO categories "
            "(id,name,definition,kind,revision,created_at) VALUES "
            "('food','Food','','spending',0,CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO transactions "
            "(id,row_sha256,occurrence,original_fields,account,description,mer"
            "chant,booking_date,value_date,amount_cents,currency,revision) "
            "VALUES "
            "('t','hash',1,'[]','a','Shop','Shop','2024-01-01','2024-01-01',-1"
            "00,'EUR',0)"
        )
        connection.exec_driver_sql(
            "INSERT INTO assignments "
            "(id,transaction_id,category_id,source,active,reason,created_at) "
            "VALUES ('a','t','food','manual',1,'',CURRENT_TIMESTAMP)"
        )
    legacy.dispose()
    engine = open_database(path)
    try:
        assert transaction_rows(engine)[0]["protected"]
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT version FROM schema_version").scalar() == 2
        engine.dispose()
        reopened = open_database(path)
        assert transaction_rows(reopened)[0]["category"] == "Food"
        reopened.dispose()
    finally:
        engine.dispose()


def test_repeated_undo_walks_back_history_and_survives_reload(engine: Engine) -> None:
    food = save_category(engine, "Food", "")
    other = save_category(engine, "Other", "")
    tid = transaction_rows(engine)[0]["id"]
    correct_transaction(engine, tid, food)
    correct_transaction(engine, tid, other)
    undo_assignment(engine, tid)
    undo_assignment(engine, tid)
    assert transaction_rows(engine)[0]["category_id"] == "uncategorized"
    with pytest.raises(ValueError, match="No earlier"):
        undo_assignment(engine, tid)
    correct_transaction(engine, tid, other)
    undo_assignment(engine, tid)
    assert transaction_rows(engine)[0]["category_id"] == "uncategorized"
    # Undo to a previous correction restores its durable example and protection.
    correct_transaction(engine, tid, food)
    correct_transaction(engine, tid, other)
    undo_assignment(engine, tid)
    path = engine.url.database
    assert path is not None
    engine.dispose()
    reopened = open_database(path)
    try:
        restored = next(r for r in transaction_rows(reopened) if r["id"] == tid)
        assert restored["category_id"] == food and restored["protected"]
        with Session(reopened) as session:
            assert require_record(session.get(ConfirmedExample, tid)).category_id == food
            assert require_record(session.get(Transaction, tid)).revision == 9
    finally:
        reopened.dispose()


def test_example_listing_and_deletion_preserve_assignments(engine: Engine) -> None:
    from fyntok.categorization import latest_examples
    from fyntok.manual import delete_example
    from fyntok.queries import example_rows

    food = save_category(engine, "Food", "Groceries")
    ids = [row["id"] for row in transaction_rows(engine)]
    first = correct_transaction(engine, ids[0], food)
    second = correct_transaction(engine, ids[1], food)
    examples = example_rows(engine)
    assert [row["id"] for row in examples] == [ids[1], ids[0]]
    row = examples[0]
    assert row["assignment_id"] == second
    assert row["category"] == "Food" and row["category_id"] == food
    transaction = next(row for row in transaction_rows(engine) if row["id"] == ids[1])
    for field in ("description", "cleaned_description", "booking_date", "amount_cents", "currency"):
        assert row[field] == transaction[field]
    assert row["confirmed_at"] is not None
    assert "account" not in row
    before_rows = transaction_rows(engine)
    before_history = assignment_history(engine, ids[1])
    before_totals = aggregate(engine)
    delete_example(engine, ids[1], expected_assignment_id=second)
    assert [row["id"] for row in example_rows(engine)] == [ids[0]]
    assert transaction_rows(engine) == before_rows
    assert assignment_history(engine, ids[1]) == before_history
    assert aggregate(engine) == before_totals
    with Session(engine) as session:
        assert [row["example_id"] for row in latest_examples(session)] == [ids[0]]
    with pytest.raises(EditConflict, match="Refresh"):
        delete_example(engine, ids[1], expected_assignment_id=second)
    replacement = correct_transaction(engine, ids[0], food)
    with pytest.raises(EditConflict, match="Refresh"):
        delete_example(engine, ids[0], expected_assignment_id=first)
    assert example_rows(engine)[0]["assignment_id"] == replacement
    delete_example(engine, ids[0], expected_assignment_id=replacement)
    assert example_rows(engine) == []
    correct_transaction(engine, ids[1], food)
    assert [row["id"] for row in example_rows(engine)] == [ids[1]]
