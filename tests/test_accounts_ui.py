"""Account editing and repayment review through real Dash callbacks."""

from pathlib import Path

from fyntok.accounts import account_rows
from fyntok.app import create_app
from fyntok.auth import ChatGPTSession
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.queries import transaction_rows
from tests.test_dash_driver import DashDriver


def test_combined_accounts_and_repayment_review(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "accounts.sqlite3")
    import_content(
        engine,
        b"bank;Groceries;01.01.2024;01.01.2024;-100,00;EUR\n"
        b"bank;Kreditkarte TEST Abrechnung;10.01.2024;10.01.2024;-50,00;EUR\n",
        "bank.csv",
    )
    import_content(
        engine,
        b"card;Books;02.01.2024;02.01.2024;-50,00;EUR\n"
        b"card;EINZUGSBETRAG;10.01.2024;10.01.2024;+50,00;EUR\n",
        "card.csv",
    )
    app = create_app(engine, auth=ChatGPTSession(tmp_path / "credentials"))
    try:
        browser = DashDriver(app)
        browser.refresh()
        assert "150.00 EUR" in str(browser.get("overview-report", "children"))
        rows = browser.get({"type": "transactions", "index": "main"}, "rowData")
        assert {row["account_label"] for row in rows} == {"Bank · bank", "Credit card · card"}
        bank = next(row for row in rows if row["repayment_role"] == "bank")
        browser.select(bank)
        assert "Matched to an opposite entry" in str(
            browser.get("transaction-description", "children")
        )
        browser.set("transaction-treatment", "value", "include")
        browser.call("transaction-status.children", "transaction-repayment")
        assert (
            next(row for row in transaction_rows(engine) if row["id"] == bank["id"])[
                "repayment_status"
            ]
            == "Included by review"
        )
        browser.refresh()
        assert "200.00 EUR" in str(browser.get("overview-report", "children"))
        browser.call("account-label.value", "account-edit", "value", "bank")
        browser.set("account-label", "value", "Daily account")
        browser.call("account-status.children", "account-save")
        assert any(row["label"] == "Daily account" for row in account_rows(engine))
        browser.set("account-edit", "value", None)
        browser.call("account-status.children", "account-save")
        assert "Select an account" in browser.get("account-status", "children")
    finally:
        app.server.extensions["fyntok-ai"].close()
        engine.dispose()
