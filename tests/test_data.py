import csv
from collections import defaultdict
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fyntok.analysis import aggregate
from fyntok.database import open_database, require_record
from fyntok.importer import import_csv, merchant_label, parse_csv
from fyntok.models import (
    Assignment,
    CategorizationRun,
    Category,
    ConfirmedExample,
    Import,
    ImportRow,
    RunItem,
    Transaction,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "test.sqlite3")
    yield engine
    engine.dispose()


def make_csv(tmp_path: Path, rows: list[list[str]]) -> Path:
    path = tmp_path / "sample.csv"
    with path.open("w", encoding="cp1252", newline="") as f:
        csv.writer(f, delimiter=";").writerows(rows)
    return path


def row(
    description: str = "SHOP | Café",
    amount: str = "-12,34",
    booking: str = "31.01.2024",
    value: str = "01.02.2024",
    currency: str = "EUR",
) -> list[str]:
    return ["account", description, booking, value, amount, currency]


def test_preservation_duplicates_and_overlap(engine: Engine, tmp_path: Path) -> None:
    fields = row()
    path = make_csv(tmp_path, [fields, fields])
    result = import_csv(engine, path)
    assert (result.row_count, result.inserted_count, result.duplicate_count) == (2, 2, 0)
    assert import_csv(engine, path).duplicate_count == 2
    path = make_csv(tmp_path, [fields, fields, fields, row("Other")])
    assert import_csv(engine, path).inserted_count == 2
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Transaction)) == 4
        assert session.scalar(select(func.count()).select_from(Import)) == 3
        assert session.scalar(select(func.count()).select_from(ImportRow)) == 8
        t = require_record(session.scalar(select(Transaction).where(Transaction.occurrence == 2)))
        assert t.original_fields == fields
        assert t.account == "account" and t.merchant == "Café"
        assert t.booking_date == date(2024, 1, 31)
        assert t.value_date == date(2024, 2, 1) and t.amount_cents == -1234
        assert (
            require_record(session.get(Import, result.import_id))
            .original_bytes.decode("cp1252")
            .count("Café")
            == 2
        )


@pytest.mark.parametrize(
    "bad",
    [
        row(amount="1,001"),
        row(amount="NaN"),
        row(amount="1.2,34"),
        row(booking="31.02.2024"),
        row(currency=""),
        ["wrong"],
    ],
)
def test_invalid_import_is_atomic(engine: Engine, tmp_path: Path, bad: list[str]) -> None:
    with pytest.raises(ValueError, match="row 2"):
        import_csv(engine, make_csv(tmp_path, [row(), bad]))
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Transaction)) == 0
        assert session.scalar(select(func.count()).select_from(Import)) == 0


def test_amounts_and_quoted_fields() -> None:
    fields = parse_csv('a;"shop; café";01.01.2024;02.01.2024;-1.234,56;EUR\r\n'.encode("cp1252"))[0]
    assert fields["amount_cents"] == -123456
    assert fields["description"] == "shop; café"
    with pytest.raises(ValueError, match="no transactions"):
        parse_csv(b"")


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            "Beispielbank kreditkarte TEST 200001 MC/000000001|"
            "Beispielbank kreditkarte TEST 200001Abrechnung Nr. 000000001|10",
            "Beispielbank kreditkarte TEST",
        ),
        (
            "Musterbank kreditkarte DEMO           200002       MC/000000002|"
            "Abrechnung Nr. 000000002",
            "Musterbank kreditkarte DEMO",
        ),
        (
            "Abbuchung Onlinebanking FE/000000001|Beispiel Sonnenschutz und|0000000001",
            "Beispiel Sonnenschutz und",
        ),
        ("Bank | Shop | 10 | 11 | 12", "Shop"),
        ("Bank |  Café   Beispiel  | 12 | ", "Café Beispiel"),
        ("Bank | Shop 24", "Shop 24"),
        ("Beispielbank kreditkarte TEST shop | Shop 24", "Shop 24"),
        ("Bank | 7-Shop", "7-Shop"),
        ("123", "Unknown"),
        (" 10 | 11 | ", "Unknown"),
        (" | ", "Unknown"),
        ("", "Unknown"),
    ],
)
def test_merchant_labels_use_meaningful_text(description: str, expected: str) -> None:
    assert merchant_label(description) == expected
    parsed = parse_csv(f"a;{description};01.01.2024;01.01.2024;-1,00;EUR\n".encode("cp1252"))
    assert parsed[0]["merchant"] == expected
    assert parsed[0]["description"] == description


def test_sql_aggregation_uses_active_assignments_and_booking_date(
    engine: Engine, tmp_path: Path
) -> None:
    import_csv(
        engine,
        make_csv(
            tmp_path,
            [
                row("Food", "-10,00"),
                row("Pay", "20,00"),
                row("Transfer", "-30,00"),
                row("Unknown", "-40,00"),
                row("Unknown credit", "50,00"),
                row("USD food", "-2,00", currency="USD"),
            ],
        ),
    )
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                Category(id="food", name="Food"),
                Category(id="salary", name="Salary", kind="income"),
                Category(id="transfer", name="Transfers", kind="transfer"),
            ]
        )
        session.flush()
        for t in session.scalars(select(Transaction)):
            if t.description in {"Food", "Pay", "Transfer", "USD food"}:
                category = {
                    "Food": "food",
                    "Pay": "salary",
                    "Transfer": "transfer",
                    "USD food": "food",
                }[t.description]
                session.add(Assignment(transaction_id=t.id, category_id=category, source="manual"))
        t = require_record(
            session.scalar(select(Transaction).where(Transaction.description == "Food"))
        )
        session.add(
            Assignment(transaction_id=t.id, category_id="transfer", source="ai", active=False)
        )
    totals = aggregate(engine)
    assert {x["month"] for x in totals} == {"2024-01"}
    eur = {x["bucket"]: x for x in totals if x["currency"] == "EUR"}
    assert eur["spending"]["outgoing_cents"] == 1000
    assert eur["income"]["incoming_cents"] == 2000
    assert eur["internal_transfer"]["net_cents"] == -3000
    assert eur["uncategorized"]["net_cents"] == 1000
    assert eur["uncategorized"]["transaction_count"] == 2
    assert aggregate(engine, by="trends") == totals
    assert sum(x["transaction_count"] for x in aggregate(engine, by="merchant")) == 6
    assert aggregate(engine, start=date(2024, 2, 1)) == []
    assert aggregate(engine, end=date(2024, 1, 30)) == []
    assert len(aggregate(engine, category_id="food", merchant="Food")) == 1


def test_history_examples_and_run_progress_survive_reload(engine: Engine, tmp_path: Path) -> None:
    import_csv(engine, make_csv(tmp_path, [row()]))
    with Session(engine) as session, session.begin():
        t = require_record(session.scalar(select(Transaction)))
        tid = t.id
        session.add(Category(id="food", name="Food"))
        run = CategorizationRun(
            model="test-model", scope={"all": True}, category_snapshot=[{"id": "food"}]
        )
        session.add(run)
        session.flush()
        rid = run.id
        first = Assignment(
            transaction_id=tid, category_id="food", source="ai", active=False, run_id=rid
        )
        session.add(first)
        session.flush()
        confirmed = Assignment(
            transaction_id=tid,
            category_id="uncategorized",
            source="manual",
            previous_assignment_id=first.id,
        )
        session.add(confirmed)
        session.flush()
        session.add(
            ConfirmedExample(
                transaction_id=tid, assignment_id=confirmed.id, category_id="uncategorized"
            )
        )
        session.add(
            RunItem(
                run_id=rid,
                transaction_id=tid,
                expected_revision=0,
                status="applied",
                attempts=1,
                result={"category_id": "food"},
                applied_assignment_id=first.id,
            )
        )
        t.revision = 1
    engine.dispose()
    reopened = open_database(tmp_path / "test.sqlite3")
    try:
        with Session(reopened) as session:
            assert require_record(session.get(Transaction, tid)).revision == 1
            assert require_record(session.get(RunItem, (rid, tid))).result == {
                "category_id": "food"
            }
            assert require_record(session.get(RunItem, (rid, tid))).status == "applied"
            assert require_record(session.get(CategorizationRun, rid)).category_snapshot == [
                {"id": "food"}
            ]
            assert require_record(session.get(ConfirmedExample, tid)).category_id == "uncategorized"
            assert len(list(session.scalars(select(Assignment)))) == 2
            assert (
                require_record(
                    session.scalar(select(Assignment).where(Assignment.active.is_(True)))
                ).source
                == "manual"
            )
        with pytest.raises(IntegrityError), Session(reopened) as session, session.begin():
            session.add(Assignment(transaction_id=tid, category_id="food", source="ai"))
        with pytest.raises(IntegrityError), Session(reopened) as session, session.begin():
            session.add(Assignment(transaction_id="missing", category_id="food", source="manual"))
        with pytest.raises(IntegrityError), Session(reopened) as session, session.begin():
            session.add(
                Assignment(transaction_id=tid, category_id="missing", source="ai", active=False)
            )
    finally:
        reopened.dispose()


def test_supplied_dataset_exact_totals_months_and_reload(engine: Engine, tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "data/data.csv"
    if not source.exists():
        pytest.skip("Private supplied dataset is not available")
    result = import_csv(engine, source)
    assert result.row_count == result.inserted_count
    totals = aggregate(engine)
    # Independent CSV oracle checks every month and both parsed date columns.
    expected = defaultdict(lambda: [0, 0, 0, 0])
    with source.open(encoding="cp1252", newline="") as f:
        fields = list(csv.reader(f, delimiter=";"))
    assert result.row_count == len(fields)
    for r in fields:
        month = datetime.strptime(r[2], "%d.%m.%Y").strftime("%Y-%m")
        amount = int(Decimal(r[4].replace(".", "").replace(",", ".")) * 100)
        values = expected[month]
        values[0] += 1
        values[1] += amount
        values[2] += max(-amount, 0)
        values[3] += max(amount, 0)
    assert {
        r["month"]: [
            r["transaction_count"],
            r["net_cents"],
            r["outgoing_cents"],
            r["incoming_cents"],
        ]
        for r in totals
    } == dict(expected)
    assert all(r["bucket"] == "uncategorized" for r in totals)
    with Session(engine) as session:
        stored = list(
            session.scalars(
                select(Transaction)
                .join(ImportRow, ImportRow.transaction_id == Transaction.id)
                .where(ImportRow.import_id == result.import_id)
                .order_by(ImportRow.row_number)
            )
        )
        assert [t.original_fields for t in stored] == fields
        assert all(not t.merchant.isdigit() for t in stored)
        card_statements = [
            t
            for t in stored
            if " kreditkarte " in t.description.split("|")[0]
            and " MC/" in t.description.split("|")[0]
        ]
        assert card_statements
        assert len({t.merchant for t in card_statements}) == 1
        assert all(t.description.startswith(t.merchant + " ") for t in card_statements)
        assert all(
            t.booking_date == datetime.strptime(r[2], "%d.%m.%Y").date()
            and t.value_date == datetime.strptime(r[3], "%d.%m.%Y").date()
            for t, r in zip(stored, fields, strict=False)
        )
    engine.dispose()
    reopened = open_database(tmp_path / "test.sqlite3")
    try:
        assert aggregate(reopened) == totals
        repeated = import_csv(reopened, source)
        assert repeated.inserted_count == 0
        assert repeated.duplicate_count == result.row_count
        assert aggregate(reopened) == totals
    finally:
        reopened.dispose()


def test_example_category_and_history_transaction_must_match(
    engine: Engine, tmp_path: Path
) -> None:
    import_csv(engine, make_csv(tmp_path, [row(), row("Another")]))
    with Session(engine) as session, session.begin():
        ids = list(session.scalars(select(Transaction.id)))
        session.add(Category(id="food", name="Food"))
        session.flush()
        assignment = Assignment(transaction_id=ids[0], category_id="food", source="manual")
        session.add(assignment)
        session.flush()
        aid = assignment.id
    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        session.add(
            ConfirmedExample(transaction_id=ids[0], assignment_id=aid, category_id="uncategorized")
        )
    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        session.add(
            Assignment(
                transaction_id=ids[1],
                category_id="food",
                source="manual",
                previous_assignment_id=aid,
            )
        )


def test_unknown_schema_is_refused(engine: Engine, tmp_path: Path) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE schema_version SET version = 99")
    with pytest.raises(ValueError, match="migration required"):
        open_database(tmp_path / "test.sqlite3")
