"""Bank/card integration uses synthetic statements only."""

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from fyntok.accounts import (
    account_rows,
    repayment_warning,
    review_repayment,
    update_account,
)
from fyntok.analysis import aggregate
from fyntok.cli import main
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.insights import Period, build_report
from fyntok.manual import assignment_history, correct_transaction, save_category
from fyntok.models import Account, Assignment, CardRepayment, ConfirmedExample, Import, Transaction
from fyntok.queries import Filters, transaction_rows

BANK = (
    b"bank;Supermarket;01.01.2024;01.01.2024;-100,00;EUR\n"
    b"bank;Beispielbank kreditkarte TEST | Beispielbank kreditkarte TEST 123Abrechnung Nr. 456;"
    b"10.01.2024;10.01.2024;-50,00;EUR\n"
)
CARD = (
    b"card;Bookshop;02.01.2024;01.01.2024;-49,00;EUR\n"
    b"card;MANIPULATIONSENTGELT USA;02.01.2024;01.01.2024;-1,00;EUR\n"
    b"card;EINZUGSBETRAG;10.01.2024;10.01.2024;+50,00;EUR\n"
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "accounts.sqlite3")
    yield engine
    engine.dispose()


def spending(engine: Engine) -> int:
    return sum(int(row["outgoing_cents"]) for row in aggregate(engine))


def repayment_rows(engine: Engine) -> list[dict[str, Any]]:
    return [row for row in transaction_rows(engine) if row["repayment_role"]]


@pytest.mark.parametrize("issuer", [b"Example Issuer", b"Sample Bank", b""])
def test_settlement_detection_is_independent_of_issuer(engine: Engine, issuer: bytes) -> None:
    import_content(engine, BANK.replace(b"Beispielbank", issuer), "bank.csv")
    import_content(engine, CARD, "card.csv")
    assert len(repayment_rows(engine)) == 2
    assert all(row["repayment_partner"] for row in repayment_rows(engine))


@pytest.mark.parametrize("card_first", [False, True])
def test_combined_spending_and_repeat_imports(engine: Engine, card_first: bool) -> None:
    statements = [BANK, CARD] if not card_first else [CARD, BANK]
    for statement in statements:
        import_content(engine, statement, "arbitrary.csv")
    assert spending(engine) == 15000
    assert sum(row["incoming_cents"] for row in aggregate(engine)) == 0
    rows = transaction_rows(engine)
    assert len(rows) == 5
    assert [row["booking_date"] for row in rows] == sorted(
        [row["booking_date"] for row in rows], reverse=True
    )
    assert {row["account_kind"] for row in rows} == {"bank", "credit_card"}
    assert all(row["repayment_excluded"] for row in repayment_rows(engine))
    assert all(row["repayment_partner"] for row in repayment_rows(engine))
    assert not repayment_warning(rows)
    for statement in statements:
        assert import_content(engine, statement, "renamed.csv").inserted_count == 0
    assert spending(engine) == 15000
    assert len(transaction_rows(engine)) == 5
    for by in ("monthly", "trends", "merchant"):
        assert sum(row["outgoing_cents"] for row in aggregate(engine, by=by)) == 15000
        assert sum(row["transaction_count"] for row in aggregate(engine, by=by)) == 3
    report = build_report(
        engine, Filters(currency="EUR"), Period(date(2024, 1, 1), date(2024, 1, 31))
    )
    assert report.totals["spending_cents"] == 15000
    with Session(engine) as session:
        assert not list(session.scalars(select(ConfirmedExample)))


def test_bank_only_history_is_preserved_and_warned(engine: Engine) -> None:
    import_content(engine, BANK, "bank.csv")
    assert spending(engine) == 15000
    bank = repayment_rows(engine)[0]
    assert not bank["repayment_excluded"]
    assert bank["repayment_status"] == "Review needed"
    assert "missing card detail" in repayment_warning(transaction_rows(engine))
    # A later card export does not retroactively hide an older unmatched bank bill.
    import_content(engine, CARD.replace(b"10.01.2024", b"10.02.2024"), "card.csv")
    assert spending(engine) == 20000
    assert "no matched bank withdrawal" in repayment_warning(transaction_rows(engine))


@pytest.mark.parametrize(
    ("replacement", "expected_match"),
    [(b"12.01.2024", True), (b"13.01.2024", True), (b"14.01.2024", False)],
)
def test_booking_date_window(engine: Engine, replacement: bytes, expected_match: bool) -> None:
    import_content(engine, BANK, "bank.csv")
    import_content(engine, CARD.replace(b"10.01.2024", replacement), "card.csv")
    bank = next(r for r in repayment_rows(engine) if r["repayment_role"] == "bank")
    assert bool(bank["repayment_partner"]) is expected_match
    assert spending(engine) == (15000 if expected_match else 20000)


def test_ambiguous_and_different_currency_matches_are_not_linked(engine: Engine) -> None:
    import_content(engine, BANK, "bank.csv")
    import_content(engine, CARD.replace(b"EUR", b"USD"), "foreign.csv")
    assert not any(r["repayment_partner"] for r in repayment_rows(engine))
    # Multiplicity is retained; two identical card credits must not be assigned one bank debit.
    credit = CARD.splitlines(keepends=True)[-1].replace(b"card;", b"other;")
    import_content(engine, credit + credit, "ambiguous.csv")
    assert not any(r["repayment_partner"] for r in repayment_rows(engine))
    assert len(repayment_rows(engine)) == 4


def test_amount_only_and_unrelated_descriptions_never_match(engine: Engine) -> None:
    import_content(
        engine,
        BANK.replace(
            b"Beispielbank kreditkarte TEST | Beispielbank kreditkarte TEST 123Abrechnung Nr. 456",
            b"Rent",
        ),
        "b.csv",
    )
    import_content(engine, CARD, "c.csv")
    assert len(repayment_rows(engine)) == 1
    assert not repayment_rows(engine)[0]["repayment_partner"]
    assert spending(engine) == 20000


def test_protected_categories_block_auto_exclusion_without_losing_history(engine: Engine) -> None:
    import_content(engine, BANK, "b.csv")
    category = save_category(engine, "Shopping", "Purchases")
    bank = repayment_rows(engine)[0]
    correct_transaction(engine, bank["id"], category)
    history = assignment_history(engine, bank["id"])
    import_content(engine, CARD, "c.csv")
    assert not any(r["repayment_excluded"] for r in repayment_rows(engine))
    assert all(r["repayment_status"] == "Review needed" for r in repayment_rows(engine))
    assert assignment_history(engine, bank["id"]) == history
    bank = next(r for r in repayment_rows(engine) if r["id"] == bank["id"])
    review_repayment(engine, bank["id"], "exclude", expected_revision=bank["revision"])
    assert assignment_history(engine, bank["id"]) == history
    assert next(r for r in repayment_rows(engine) if r["id"] == bank["id"])["repayment_excluded"]
    with Session(engine) as session:
        assert len(list(session.scalars(select(ConfirmedExample)))) == 1
    with pytest.raises(ValueError, match="changed"):
        review_repayment(engine, bank["id"], "auto", expected_revision=bank["revision"])


def test_transfer_category_is_not_a_protected_conflict(engine: Engine) -> None:
    import_content(engine, BANK, "b.csv")
    transfer = save_category(engine, "Transfers", "Own accounts", "transfer")
    correct_transaction(engine, repayment_rows(engine)[0]["id"], transfer)
    import_content(engine, CARD, "c.csv")
    assert all(r["repayment_excluded"] for r in repayment_rows(engine))
    assert spending(engine) == 15000


def test_ai_category_changes_do_not_turn_repayments_into_spending(engine: Engine) -> None:
    import_content(engine, BANK, "bank.csv")
    import_content(engine, CARD, "card.csv")
    category = save_category(engine, "Shopping", "Purchases")
    with Session(engine) as session, session.begin():
        for row in repayment_rows(engine):
            session.add(
                Assignment(
                    transaction_id=row["id"],
                    category_id=category,
                    source="ai",
                    active=True,
                    protected=False,
                    reason="Synthetic AI classification",
                )
            )
    assert spending(engine) == 15000
    assert all(row["repayment_excluded"] for row in repayment_rows(engine))
    with Session(engine) as session:
        assert not list(session.scalars(select(ConfirmedExample)))


def test_review_survives_import_and_account_labels_can_change(engine: Engine) -> None:
    import_content(engine, BANK, "b.csv")
    import_content(engine, CARD, "c.csv")
    bank = next(r for r in repayment_rows(engine) if r["repayment_role"] == "bank")
    review_repayment(engine, bank["id"], "include", expected_revision=bank["revision"])
    import_content(engine, CARD, "repeat.csv")
    assert spending(engine) == 20000
    bank = next(r for r in repayment_rows(engine) if r["id"] == bank["id"])
    assert bank["repayment_status"] == "Included by review"
    review_repayment(engine, bank["id"], "auto", expected_revision=bank["revision"])
    assert spending(engine) == 15000
    update_account(engine, "card", "Travel card", "credit_card")
    assert {a["label"] for a in account_rows(engine)} == {"Bank · bank", "Travel card"}
    assert any(r["account_label"] == "Travel card" for r in transaction_rows(engine))
    update_account(engine, "card", "Wrongly typed", "bank")
    assert not any(r["repayment_partner"] for r in repayment_rows(engine))
    update_account(engine, "card", "Travel card", "credit_card")
    assert spending(engine) == 15000


def test_explicit_account_kind_without_settlement_and_validation(engine: Engine) -> None:
    purchase = CARD.splitlines(keepends=True)[0]
    import_content(engine, purchase, "not-a-card-filename.csv", account_kind="credit_card")
    assert account_rows(engine)[0]["kind"] == "credit_card"
    with pytest.raises(ValueError, match="differs"):
        import_content(engine, CARD, "c.csv", account_kind="bank")
    with pytest.raises(ValueError, match="Unknown account"):
        import_content(engine, BANK, "b.csv", account_kind="cash")
    for identity, label, kind in [("card", "", "bank"), ("card", "Card", "cash")]:
        with pytest.raises(ValueError, match="Choose"):
            update_account(engine, identity, label, kind)
    with pytest.raises(ValueError, match="does not exist"):
        update_account(engine, "absent", "Missing", "bank")
    with pytest.raises(ValueError, match="Unknown repayment"):
        review_repayment(engine, "absent", "wrong", expected_revision=0)
    with pytest.raises(ValueError, match="does not exist"):
        review_repayment(engine, "absent", "exclude", expected_revision=0)
    with Session(engine) as session:
        assert len(list(session.scalars(select(Import)))) == 1


def test_v2_migration_backfills_accounts_and_repayments(tmp_path: Path) -> None:
    path = tmp_path / "migration.sqlite3"
    engine = open_database(path)
    import_content(engine, BANK, "b.csv")
    import_content(engine, CARD, "c.csv")
    ids = {r["id"] for r in transaction_rows(engine)}
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE card_repayments")
        connection.exec_driver_sql("DROP TABLE accounts")
        connection.exec_driver_sql("UPDATE schema_version SET version = 2")
    engine.dispose()
    engine = open_database(path)
    try:
        assert spending(engine) == 15000
        assert len(account_rows(engine)) == 2
        assert {r["id"] for r in transaction_rows(engine)} == ids
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT version FROM schema_version").scalar() == 3
        # New settlement foreign keys must not break the existing transaction clearing workflow.
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
            connection.exec_driver_sql("DELETE FROM import_rows")
            connection.exec_driver_sql("DELETE FROM transactions")
        with Session(engine) as session:
            assert not list(session.scalars(select(Transaction)))
            assert not list(session.scalars(select(CardRepayment)))
            assert len(list(session.scalars(select(Account)))) == 2
    finally:
        engine.dispose()


def test_migration_failure_rolls_back_schema_and_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-migration.sqlite3"
    engine = open_database(path)
    import_content(engine, BANK, "bank.csv")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE card_repayments")
        connection.exec_driver_sql("DROP TABLE accounts")
        connection.exec_driver_sql("UPDATE schema_version SET version = 2")

    def fail(_session: Session) -> None:
        raise ValueError("Synthetic backfill failure")

    with monkeypatch.context() as patch:
        patch.setattr("fyntok.database.reconcile_repayments", fail)
        with pytest.raises(ValueError, match="Synthetic"):
            open_database(path)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version FROM schema_version").scalar() == 2
        assert not connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name IN ('accounts','card_repayments')"
        ).all()
    engine.dispose()
    recovered = open_database(path)
    try:
        assert len(transaction_rows(recovered)) == 2
        assert len(account_rows(recovered)) == 1
    finally:
        recovered.dispose()


def test_explicit_reviews_survive_account_type_changes(engine: Engine) -> None:
    import_content(engine, CARD, "card.csv")
    card = repayment_rows(engine)[0]
    review_repayment(engine, card["id"], "include", expected_revision=card["revision"])
    update_account(engine, "card", "Card", "bank")
    assert repayment_rows(engine)[0]["repayment_treatment"] == "include"
    update_account(engine, "card", "Card", "credit_card")
    assert repayment_rows(engine)[0]["repayment_treatment"] == "include"


def test_cli_combines_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "cli.sqlite3"
    for filename, content, kind in [("bank.csv", BANK, "bank"), ("card.csv", CARD, "credit_card")]:
        path = tmp_path / filename
        path.write_bytes(content)
        monkeypatch.setattr(
            "sys.argv",
            ["fyntok", "--database", str(database), "import", str(path), "--account-kind", kind],
        )
        main()
        assert json.loads(capsys.readouterr().out)["inserted_count"] > 0
    monkeypatch.setattr("sys.argv", ["fyntok", "--database", str(database), "analyze"])
    main()
    rows = json.loads(capsys.readouterr().out)
    assert sum(row["outgoing_cents"] for row in rows) == 15000
