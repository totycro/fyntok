"""Exercise combined account labels, import controls, and repayment review callbacks."""

import asyncio
from pathlib import Path
from typing import Any

from nicegui import events, ui
from nicegui.testing import User
from nicegui.testing.user_simulation import user_simulation

from fyntok.accounts import account_rows
from fyntok.app import build_page
from fyntok.auth import ChatGPTSession
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.queries import transaction_rows


def open_row(user: User, table: ui.table, row: dict[str, Any]) -> None:
    with user:
        assert user.client is not None
        listener = next(
            listener for listener in table._event_listeners.values() if listener.type == "rowClick"
        )
        events.handle_event(
            listener.handler,
            events.GenericEventArguments(sender=table, client=user.client, args=[{}, row, 0]),
        )


def test_combined_accounts_and_repayment_review(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "ui.sqlite3")
    import_content(
        engine,
        b"bank;Groceries;01.01.2024;01.01.2024;-100,00;EUR\n"
        b"bank;Beispielbank kreditkarte TEST Abrechnung;10.01.2024;10.01.2024;-50,00;EUR\n",
        "bank.csv",
    )
    import_content(
        engine,
        b"card;Books;02.01.2024;02.01.2024;-50,00;EUR\n"
        b"card;EINZUGSBETRAG;10.01.2024;10.01.2024;+50,00;EUR\n",
        "card.csv",
    )

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            await user.should_see("150.00 EUR")
            user.find(kind=ui.tab, content="Transactions").click()
            table = next(
                t
                for t in user.find(ui.table).elements
                if "repayment_status" in {c["name"] for c in t.columns}
            )
            assert {r["account_label"] for r in table.rows} == {"Bank · bank", "Credit card · card"}
            bank = next(r for r in table.rows if r["repayment_role"] == "bank")
            open_row(user, table, bank)
            await user.should_see("Matched to an opposite entry")
            with user:
                treatment = next(
                    s
                    for s in user.find(ui.select).elements
                    if s.props.get("label") == "Repayment treatment"
                )
                treatment.value = "include"
            user.find("Save repayment treatment").click()
            await user.should_not_see("Transaction details")
            assert (
                next(r for r in transaction_rows(engine) if r["id"] == bank["id"])[
                    "repayment_status"
                ]
                == "Included by review"
            )
            user.find(kind=ui.tab, content="Overview").click()
            await user.should_see("200.00 EUR")
            # Account editing updates the labels throughout the page without recategorization.
            next(iter(user.find("Account settings").elements)).mark("account-to-edit")
            user.find(marker="account-to-edit").click()
            await user.should_see("Account label")
            user.find("Account label").clear().type("Daily account")
            user.find("Save account").click()
            await user.should_see("Daily account")
            assert any(a["label"] == "Daily account" for a in account_rows(engine))

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_import_card_purchase_only_and_show_missing_coverage(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "upload.sqlite3")
    import_content(
        engine,
        b"bank;Beispielbank kreditkarte TEST Abrechnung;10.01.2024;10.01.2024;-50,00;EUR\n",
        "bank.csv",
    )

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            await user.should_see("missing card detail")
            with user:
                kind = next(
                    s
                    for s in user.find(ui.select).elements
                    if s.props.get("label") == "Import account type"
                )
                kind.value = "credit_card"
                upload = next(iter(user.find(ui.upload).elements))
                await upload.handle_uploads(
                    [
                        ui.upload.SmallFileUpload(
                            "arbitrary.csv",
                            "text/csv",
                            b"card;Books;02.01.2024;02.01.2024;-50,00;EUR\n",
                        )
                    ]
                )
            await user.should_see("2 matching transactions")
            assert (
                next(a for a in account_rows(engine) if a["id"] == "card")["kind"] == "credit_card"
            )
            assert len(transaction_rows(engine)) == 2
            await user.should_see("missing card detail")

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
