"""Exercise real NiceGUI callbacks through its built-in user simulator."""

import asyncio
import logging
from pathlib import Path
from typing import Any, override

import pytest
from nicegui import ui
from nicegui.testing import User
from nicegui.testing.user_simulation import user_simulation

from fyntok.ai import AI_MODEL, AIRequestError
from fyntok.app import build_page
from fyntok.auth import PERMISSION, ChatGPTSession
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import categories
from fyntok.queries import transaction_rows


def poll_dashboard(user: User) -> None:
    """Run a dashboard polling cycle without waiting for the timer interval."""
    with user:
        for timer in user.find(ui.timer).elements:
            assert timer.callback is not None
            timer.callback()


def test_numeric_table_columns(tmp_path: Path) -> None:
    """Amounts and counts reach the browser as numbers, with display-only formatting."""
    engine = open_database(tmp_path / "numeric-ui.sqlite3")
    amounts = ("-1000,01", "-10,00", "-2,00", "0,00", "2,00", "10,00", "1000,01")
    content = "".join(
        f"a;Merchant {i};01.01.2024;01.01.2024;{amount};EUR\n" for i, amount in enumerate(amounts)
    ).encode()
    import_content(engine, content, "numbers.csv")
    import_content(engine, content, "duplicates.csv")

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            tables = list(user.find(ui.table).elements)
            assert len(tables) == 3
            for table in tables:
                for column in table.columns:
                    name = column["name"]
                    if name in {"incoming", "outgoing", "net", "amount"}:
                        assert column["field"] == f"{name}_cents"
                        assert ":format" in column
                    elif name.endswith("_count"):
                        assert column["field"] == name
                        assert ":format" in column
                    else:
                        assert column["align"] == "left"
                        continue
                    assert column["sortable"]
                    assert column["align"] == "right"
                    assert column["classes"] == "tabular-nums"
                    assert all(type(row[column["field"]]) is int for row in table.rows)

            transactions = next(
                t for t in tables if t.row_key == "id" and "amount_cents" in t.rows[0]
            )
            assert sorted(row["amount_cents"] for row in transactions.rows) == [
                -100001,
                -1000,
                -200,
                0,
                200,
                1000,
                100001,
            ]
            summaries = next(t for t in tables if t.row_key == "key")
            assert sorted(row["net_cents"] for row in summaries.rows) == sorted(
                row["amount_cents"] for row in transactions.rows
            )
            imports = next(t for t in tables if "filename" in t.rows[0])
            assert {row["inserted_count"] for row in imports.rows} == {0, 7}
            assert {row["duplicate_count"] for row in imports.rows} == {0, 7}

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_manual_ui_workflow(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="fyntok")
    engine = open_database(tmp_path / "ui.sqlite3")

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            assert user.client is not None
            await user.should_see("0 matching transactions")
            # Upload exercises asynchronous bytes import as used by the browser.
            upload = user.find(ui.upload).elements.pop()
            with user:
                await upload.handle_uploads(
                    [
                        ui.upload.SmallFileUpload(
                            "bank.csv",
                            "text/csv",
                            b"a;Supermarket;01.01.2024;02.01.2024;-10,00;EUR\n",
                        )
                    ]
                )
            await user.should_see("1 matching transactions")
            user.find("Create category").click()
            user.find("Name").type("Food")
            user.find("Definition").type("Groceries and supermarket purchases")
            user.find("Save").click()
            await user.should_see("Food")
            assert any(c["name"] == "Food" for c in categories(engine))
            user.find("Transactions").click()
            table = next(
                t
                for t in user.find(ui.table).elements
                if "description" in {c["name"] for c in t.columns}
            )
            table.mark("before-confirmation")
            from nicegui import events

            with user:
                listener = next(
                    listener
                    for listener in table._event_listeners.values()
                    if listener.type == "rowClick"
                )
                events.handle_event(
                    listener.handler,
                    events.GenericEventArguments(
                        sender=table, client=user.client, args=[{}, table.rows[0], 0]
                    ),
                )
            await user.should_see("Transaction details")
            with user:
                selection = next(
                    s
                    for s in user.find(ui.select).elements
                    if s.props.get("label") == "Category"
                    and s.value == "uncategorized"
                    and s.visible
                )
                selection.value = next(c["id"] for c in categories(engine) if c["name"] == "Food")
            user.find("Confirm individual category").click()
            await user.should_not_see(marker="before-confirmation")
            await user.should_see("1 matching transactions")
            await user.should_not_see("Transaction details")
            row = transaction_rows(engine)[0]
            assert row["category"] == "Food" and row["protected"]
            await user.should_see("1 matching transactions")
            table = next(
                t
                for t in user.find(ui.table).elements
                if "description" in {c["name"] for c in t.columns}
            )
            table.mark("before-undo")
            with user:
                listener = next(
                    listener
                    for listener in table._event_listeners.values()
                    if listener.type == "rowClick"
                )
                events.handle_event(
                    listener.handler,
                    events.GenericEventArguments(
                        sender=table, client=user.client, args=[{}, table.rows[0], 0]
                    ),
                )
            user.find("Undo latest assignment change").click()
            await user.should_not_see(marker="before-undo")
            await user.should_see("1 matching transactions")
            await user.should_not_see("Transaction details")
            assert transaction_rows(engine)[0]["category"] == "Uncategorized"
            assert not transaction_rows(engine)[0]["protected"]
            user.find("Description contains").type("missing")
            user.find("Apply filters").click()
            await user.should_see("0 matching transactions")
            user.find("Clear").click()
            await user.should_see("1 matching transactions")
            user.find("Categories").click()
            user.find("Edit").click()
            user.find("Name").clear().type("Groceries")
            user.find("Save").click()
            await user.should_see("Groceries · spending")
            assert any(c["name"] == "Groceries" for c in categories(engine))
            user.find("Create category").click()
            user.find("Name").type("Shopping")
            user.find("Save").click()
            await user.should_see("Shopping · spending")
            user.find("Merge").click()
            with user:
                destination = next(
                    s
                    for s in user.find(ui.select).elements
                    if s.props.get("label") == "Destination"
                )
                destination.value = next(
                    c["id"] for c in categories(engine) if c["name"] == "Shopping"
                )
            user.find("Merge category").click()
            await user.should_see("Shopping · spending")
            await user.should_not_see("Groceries · spending")
            assert {c["name"] for c in categories(engine)} == {"Shopping", "Uncategorized"}
            user.find("Delete").click()
            user.find("Delete category").click()
            await user.should_see("Uncategorized · uncategorized")
            await user.should_not_see("Shopping · spending")
            assert [c["name"] for c in categories(engine)] == ["Uncategorized"]

    try:
        asyncio.run(scenario())
        for action in (
            "Create category",
            "Edit category",
            "Merge category",
            "Delete category",
            "Confirm individual category",
            "Undo latest assignment change",
        ):
            assert f"{action}: completed" in caplog.text
        assert "Apply filters: refreshing" in caplog.text
        assert "Clear filters: resetting" in caplog.text
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
    finally:
        engine.dispose()


def test_ai_ui_proposals_fixed_model_and_recalculation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person connects their plan, edits and approves a suggestion, and reviews AI assignments."""
    from fyntok.auth import ChatGPTSession

    caplog.set_level(logging.DEBUG, logger="fyntok")
    engine = open_database(tmp_path / "ai-ui.sqlite3")
    import_content(engine, b"a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\n", "bank.csv")
    auth = ChatGPTSession(tmp_path / "credentials")
    with auth.locked():
        data = auth.load()
        data["accounts"]["client"] = {"subject": "person", "email": "person@example.com"}
        data["active"] = "client"
        auth.save(data)

    monkeypatch.setattr(
        auth,
        "start_login",
        lambda _client_id=None: "https://auth.openai.com/api/accounts/authorize?state=test",
    )

    class Advisor:
        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del payload
            assert (
                "Propose categories: preparing descriptions and requesting AI suggestions"
                in caplog.text
            )
            assert model == AI_MODEL == "gpt-6-luna"
            return {
                "categories": [
                    {"name": "Groceries", "definition": "Food at supermarkets", "kind": "spending"}
                ]
            }

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert model == AI_MODEL
            category = next(c for c in payload["categories"] if c["name"] == "Food")
            return {
                "assignments": [
                    {
                        "transaction_id": t["transaction_id"],
                        "category_id": category["id"],
                        "reason": "Supermarket",
                    }
                    for t in payload["transactions"]
                ]
            }

    async def scenario() -> None:
        async with user_simulation(lambda: build_page(engine, Advisor(), auth)) as user:
            await user.open("/")
            assert user.client is not None
            await user.should_see("AI model: gpt-6-luna")
            await user.should_not_see(kind=ui.button, content="Load models")
            await user.should_not_see(kind=ui.select, content="AI model")
            user.find("Propose categories").click()
            await user.should_see("1 suggestions ready for review.")
            assert len(categories(engine)) == 1
            user.find("Suggested name").clear().type("Food")
            user.find("Approve category").click()
            await user.should_see("Category approved and saved.")
            assert {c["name"] for c in categories(engine)} == {"Food", "Uncategorized"}
            user.find("Recalculate categories — all transactions").click()
            await user.should_see("Completed: 1 applied, 0 protected or changed; 1 / 1 processed.")
            assert transaction_rows(engine)[0]["category"] == "Food"
            assert transaction_rows(engine)[0]["source"] == "ai"
            user.find("Sign out").click()
            await user.should_see("Signed out.")

    try:
        asyncio.run(scenario())
        assert "Propose categories: completed with 1 suggestions" in caplog.text
        assert "Approve category: completed" in caplog.text
        assert "Recalculate categories — all transactions: saved run" in caplog.text
        assert "Categorization run progress: status=completed" in caplog.text
        assert "Sign out: completed" in caplog.text
        assert "person@example.com" not in caplog.text
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_proposal_failure_logs_traceback_at_default_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error_type: type[Exception]
) -> None:
    caplog.set_level(logging.INFO, logger="fyntok")
    engine = open_database(tmp_path / "failed-proposal.sqlite3")

    class Advisor:
        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise NotImplementedError("This scenario only requests proposals.")

        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise error_type("proposal request failed")

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, Advisor(), ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            assert user.client is not None
            user.find("Propose categories").click()
            await user.should_see(f"Category suggestions unavailable ({error_type.__name__}).")

    try:
        asyncio.run(scenario())
        record = next(
            r for r in caplog.records if r.getMessage().startswith("Propose categories failed")
        )
        assert record.levelno == logging.ERROR
        assert record.exc_info and record.exc_info[0] is error_type
        assert "Traceback (most recent call last):" in caplog.text
        assert f"{error_type.__name__}: proposal request failed" in caplog.text
        assert "in propose" in caplog.text
        assert "Propose categories: completed" not in caplog.text
    finally:
        engine.dispose()


def test_ui_cancel_and_recover_after_reload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from fyntok.categorization import cancel_run, start_run

    engine = open_database(tmp_path / "recover-ui.sqlite3")
    import_content(engine, b"a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\n", "bank.csv")
    auth = ChatGPTSession(tmp_path / "credentials")
    run_id = start_run(engine, "saved-model")

    class Advisor:
        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise NotImplementedError("This scenario only resumes categorization.")

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert model == "saved-model"
            cancel_run(engine, run_id)
            return {
                "assignments": [
                    {
                        "transaction_id": t["transaction_id"],
                        "category_id": "uncategorized",
                        "reason": "Unclear",
                    }
                    for t in payload["transactions"]
                ]
            }

    async def scenario() -> None:
        async with user_simulation(lambda: build_page(engine, Advisor(), auth)) as user:
            await user.open("/")
            assert user.client is not None
            await user.should_see("Pending: 0 applied")
            user.find("Cancel selected run").click()
            await user.should_see("Cancelled: 0 applied")

        class Recovered(Advisor):
            @override
            def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
                assert model == "saved-model"
                return {
                    "assignments": [
                        {
                            "transaction_id": t["transaction_id"],
                            "category_id": "uncategorized",
                            "reason": "Unclear",
                        }
                        for t in payload["transactions"]
                    ]
                }

        async with user_simulation(lambda: build_page(engine, Recovered(), auth)) as user:
            await user.open("/")
            assert user.client is not None
            await user.should_see("Cancelled: 0 applied")
            user.find("Resume / recover selected run").click()
            await user.should_see("Completed: 1 applied")
            # Polling completed history must leave sign-out status visible.
            user.find("Sign out").click()
            await user.should_see("Signed out.")
            poll_dashboard(user)
            await user.should_see("Signed out.")

    try:
        asyncio.run(scenario())
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "action", ["Propose categories", "Recalculate categories — all transactions"]
)
def test_ui_unavailable_model_has_no_fallback(tmp_path: Path, action: str) -> None:
    engine = open_database(tmp_path / "unavailable.sqlite3")
    import_content(engine, b"a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\n", "bank.csv")
    calls: list[str] = []

    class Unavailable:
        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del payload
            calls.append(model)
            raise AIRequestError(400, "subscription_sharing_unsupported_capability")

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            return self.propose(model, payload)

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, Unavailable(), ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            user.find(action).click()
            await user.should_see("Check the configured model.")
            assert calls == ["gpt-6-luna"]
            assert transaction_rows(engine)[0]["source"] == "unassigned"
            await user.should_see("Create category")

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_ui_refreshes_signed_in_account_and_selects_account_for_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = open_database(tmp_path / "accounts.sqlite3")
    auth = ChatGPTSession(tmp_path / "credentials")
    with auth.locked():
        data = auth.load()
        data["accounts"]["first"] = {"subject": "first", "email": "first@example.com"}
        data["active"] = "first"
        auth.save(data)

    monkeypatch.setattr(auth, "start_login", lambda _client_id=None: "https://auth.openai.com/")
    requested_accounts: list[str | None] = []
    signed_out: list[str | None] = []

    def sign_out() -> bool:
        signed_out.append(auth.active_account())
        with auth.locked():
            data = auth.load()
            data["accounts"][data["active"]].pop("access_token", None)
            auth.save(data)
        return True

    monkeypatch.setattr(auth, "sign_out", sign_out)

    class Advisor:
        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del payload
            assert model == AI_MODEL
            requested_accounts.append(auth.active_account())
            return {
                "categories": (
                    [{"name": "Food", "definition": "Groceries", "kind": "spending"}]
                    if auth.active_account() == "second"
                    else []
                )
            }

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise NotImplementedError("This scenario only proposes categories.")

    async def scenario() -> None:
        async with user_simulation(lambda: build_page(engine, Advisor(), auth)) as user:
            await user.open("/")
            user.find("Continue with ChatGPT").click()
            await user.should_see("Open ChatGPT sign-in, then return here to use AI.")
            # Simulate the callback completing while the dashboard remains open.
            with auth.locked():
                data = auth.load()
                data["accounts"]["second"] = {
                    "subject": "second",
                    "email": "second@example.com",
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                    "scopes": [PERMISSION],
                    "expires_at": 0,
                }
                data["active"] = "second"
                auth.save(data)
            poll_dashboard(user)
            await user.should_see("Connected to ChatGPT. Ready to use AI.")
            account = next(
                s
                for s in user.find(ui.select).elements
                if s.props.get("label") == "ChatGPT account"
            )
            assert account.value == "second"
            assert "second" in account.options
            await user.should_see(kind=ui.button, content="Reconnect")
            await user.should_not_see("Open ChatGPT sign-in, then return here to use AI.")
            user.find("Propose categories").click()
            await user.should_see("1 suggestions ready for review.")
            assert requested_accounts == ["second"]
            with user:
                account.value = "first"
            user.find("Propose categories").click()
            await user.should_see("0 suggestions ready for review.")
            assert requested_accounts == ["second", "first"]
            await user.should_see(kind=ui.button, content="Continue with ChatGPT")
            with user:
                account.value = "second"
            user.find("Sign out").click()
            await user.should_see("Signed out.")
            assert signed_out == ["second"]
            await user.should_see(kind=ui.button, content="Continue with ChatGPT")

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
