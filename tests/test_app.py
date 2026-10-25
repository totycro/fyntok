"""Exercise real NiceGUI callbacks through its built-in user simulator."""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, override

import plotly.graph_objects as go
import pytest
from nicegui import events, ui
from nicegui.testing import User
from nicegui.testing.user_simulation import user_simulation

from fyntok.ai import AI_MODEL, AIRequestError
from fyntok.analysis_plot import AnalysisPlot
from fyntok.app import build_page
from fyntok.auth import PERMISSION, ChatGPTSession
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import categories, correct_transaction, save_category
from fyntok.queries import Filters, transaction_rows


@pytest.mark.parametrize(
    ("description", "cleaned"),
    [
        (r"Bezahlung Karte MC/000000001|EXAMPLE MARKET DANKT 9999\\WIEN\9999", "EXAMPLE MARKET / WIEN"),
        ("MC/000000002|private-account Café", "Café"),
        (" | ", "Unknown merchant"),
        ("Simple shop", "Simple shop"),
    ],
)
def test_cleaned_transaction_description_ui(tmp_path: Path, description: str, cleaned: str) -> None:
    engine = open_database(tmp_path / "descriptions-ui.sqlite3")
    import_content(
        engine,
        f"private-account;{description};01.01.2024;02.01.2024;-10,00;EUR\n".encode("cp1252"),
        "synthetic.csv",
    )

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            user.find(kind=ui.tab, content="Transactions").click()
            table = next(
                t
                for t in user.find(ui.table).elements
                if "value_date" in {c["name"] for c in t.columns}
            )
            column = next(c for c in table.columns if c["name"] == "description")
            assert column["label"] == "Description"
            assert column["field"] == "cleaned_description"
            assert column["sortable"]
            assert table.rows[0][column["field"]] == cleaned
            assert table.rows[0]["description"] == description
            assert "account" not in table.rows[0]
            await user.should_not_see("Full description")
            # Only declared columns are rendered; the original stays in row data for details.
            assert all(
                table.rows[0][c["field"]] != description
                for c in table.columns
                if description != cleaned
            )
            with user:
                assert user.client is not None
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
            await user.should_see("Full description")
            await user.should_see("Cleaned description")
            assert any(label.text == cleaned for label in user.find(ui.label).elements)
            assert any(label.text == description for label in user.find(ui.label).elements)

    try:
        row = transaction_rows(engine, Filters(search=description))[0]
        assert row["description"] == description
        assert row["cleaned_description"] == cleaned
        assert "account" not in row
        asyncio.run(scenario())
    finally:
        engine.dispose()


def poll_dashboard(user: User) -> None:
    """Run a dashboard polling cycle without waiting for the timer interval."""
    with user:
        for timer in user.find(ui.timer).elements:
            assert timer.callback is not None
            timer.callback()


def test_internal_transfers_remain_in_table_but_not_statistics(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "transfers-ui.sqlite3")
    import_content(
        engine,
        b"a;Own account;01.01.2024;02.01.2024;-10,00;EUR\n",
        "synthetic.csv",
    )
    transfer = save_category(engine, "Own accounts", "Between my accounts", "transfer")
    correct_transaction(engine, transaction_rows(engine)[0]["id"], transfer)

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            await user.should_see("1 matching transactions in the transaction list")
            for plot in user.find(ui.plotly).elements:
                assert isinstance(plot.figure, go.Figure)
                assert not plot.figure.data
            summaries = next(t for t in user.find(ui.table).elements if t.row_key == "key")
            assert summaries.rows == []
            user.find(kind=ui.tab, content="Transactions").click()
            table = next(
                t
                for t in user.find(ui.table).elements
                if "value_date" in {c["name"] for c in t.columns}
            )
            assert len(table.rows) == 1
            assert table.rows[0]["category_id"] == transfer

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_data_tools_preserve_proposal_edits_when_reopened(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "tools.sqlite3")

    class Advisor:
        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            return {"categories": [{"name": "Food", "definition": "Groceries", "kind": "spending"}]}

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise NotImplementedError("This scenario only proposes categories.")

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, Advisor(), ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            tools = user.find(marker="data-tools").elements.pop()
            assert isinstance(tools, ui.dialog)
            assert not tools.value
            await user.should_see("0 matching transactions")
            await user.should_not_see(marker="categorization-activity")
            user.find(kind=ui.button, content="Categorize").click()
            assert tools.value
            panels = next(e for e in tools.descendants() if isinstance(e, ui.tab_panels))
            assert panels.value == "categorization"
            user.find("Propose categories").click()
            await user.should_see("1 suggestions ready for review.")
            user.find("Suggested name").clear().type("Weekly groceries")
            user.find(marker="close-data-tools").click()
            assert not tools.value
            user.find(kind=ui.button, content="Import CSV").click()
            assert panels.value == "import"
            user.find(kind=ui.tab, content="Categorization").click()
            assert panels.value == "categorization"
            proposal = user.find("Suggested name").elements.pop()
            assert isinstance(proposal, ui.input)
            assert proposal.value == "Weekly groceries"
            user.find("Approve category").click()
            await user.should_see("Category approved and saved.")
            assert "Weekly groceries" in {c["name"] for c in categories(engine)}
            await user.should_not_see(marker="categorization-activity")

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_categorization_continues_with_tools_closed(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "background-tools.sqlite3")
    import_content(engine, b"a;Shop;01.01.2024;01.01.2024;-10,00;EUR\n", "bank.csv")
    release = threading.Event()

    class Advisor:
        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise NotImplementedError("This scenario only categorizes transactions.")

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model
            assert release.wait(timeout=10)
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
        async with user_simulation(
            lambda: build_page(engine, Advisor(), ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            user.find(kind=ui.button, content="Categorize").click()
            user.find("Recalculate categories — all transactions").click()
            await user.should_see(marker="categorization-activity")
            activity = user.find(marker="categorization-activity").elements.pop()
            assert activity.props["label"] in {"Pending: 0 / 1", "Running: 0 / 1"}
            user.find(marker="close-data-tools").click()
            tools = user.find(marker="data-tools").elements.pop()
            assert isinstance(tools, ui.dialog)
            assert not tools.value
            release.set()
            await user.should_see("Completed: 1 / 1", retries=30)
            assert not tools.value
            assert transaction_rows(engine)[0]["source"] == "ai"
            user.find(marker="categorization-activity").click()
            assert tools.value
            await user.should_see("Completed: 1 applied")

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        engine.dispose()


def test_numeric_table_columns(tmp_path: Path) -> None:
    """Amounts and counts reach the browser as numbers, with display-only formatting."""
    engine = open_database(tmp_path / "numeric-ui.sqlite3")
    amounts = ("-1000,01", "-10,00", "-2,00", "0,00", "2,00", "10,00", "1000,01")
    content = "".join(
        f"a;Merchant{i};01.01.2024;01.01.2024;{amount};EUR\n" for i, amount in enumerate(amounts)
    ).encode()
    import_content(engine, content, "numbers.csv")
    import_content(engine, content, "duplicates.csv")

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            tables = list(user.find(ui.table).elements)
            for table in tables:
                for column in table.columns:
                    name = column["name"]
                    if name in {
                        "incoming",
                        "outgoing",
                        "net",
                        "amount",
                        "spending",
                        "average",
                        "median",
                    }:
                        assert column["field"] == f"{name}_cents"
                        assert ":format" in column
                    elif name == "share":
                        assert ":format" in column
                        assert column["sortable"]
                        continue
                    elif name.endswith("_count"):
                        assert column["field"] == name
                        assert ":format" in column
                    else:
                        assert column.get("align", "left") == "left"
                        continue
                    assert column["sortable"]
                    assert column["align"] == "right"
                    assert column["classes"] == "tabular-nums"
                    assert all(type(row[column["field"]]) is int for row in table.rows)

            transactions = next(
                t for t in tables if t.row_key == "id" and t.rows and "amount_cents" in t.rows[0]
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
            # Uncategorized money is exposed separately, not ranked as categorized spending.
            assert summaries.rows == []
            await user.should_see("Uncategorized · outgoing 1,012.01 EUR · incoming 1,012.01 EUR")
            imports = next(t for t in tables if t.rows and "filename" in t.rows[0])
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
            user.find(kind=ui.tab, content="Transactions").click()
            table = next(
                t
                for t in user.find(ui.table).elements
                if "value_date" in {c["name"] for c in t.columns}
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
                if "value_date" in {c["name"] for c in t.columns}
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
            user.find(marker="clear-transaction-filters").click()
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
            assert model == AI_MODEL == "gpt-6.1-sol"
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
            await user.should_see("AI model: gpt-6.1-sol")
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
            assert calls == ["gpt-6.1-sol"]
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


@pytest.mark.parametrize("replace_before_delete", [False, True])
def test_manual_examples_ui(tmp_path: Path, replace_before_delete: bool) -> None:
    from fyntok.queries import example_rows

    engine = open_database(tmp_path / "examples-ui.sqlite3")
    import_content(engine, b"a;Shop;01.01.2024;02.01.2024;-10,00;EUR\n", "synthetic.csv")
    food = save_category(engine, "Food", "Groceries")
    tid = transaction_rows(engine)[0]["id"]
    correct_transaction(engine, tid, food)
    before = transaction_rows(engine)

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            user.find("Description contains").type("No matching transaction")
            if replace_before_delete:
                user.find("Booking date from").type("2025-01-01")
                user.find("Through").type("2024-01-01")
            user.find("Apply filters").click()
            user.find(kind=ui.tab, content="Manual examples").click()
            await user.should_see("1 manual examples")
            await user.should_see("0 matching transactions")
            if replace_before_delete:
                await user.should_see("Start date must be before the end date.")

            def open_deletion() -> None:
                table = user.find(marker="manual-examples-table").elements.pop()
                assert isinstance(table, ui.table)
                assert table.rows[0]["category"] == "Food"
                assert table.rows[0]["amount_cents"] == -1000
                assert table.rows[0]["cleaned_description"] == "Shop"
                with user:
                    assert user.client is not None
                    listener = next(
                        item
                        for item in table._event_listeners.values()
                        if item.type == "deleteExample"
                    )
                    events.handle_event(
                        listener.handler,
                        events.GenericEventArguments(
                            sender=table, client=user.client, args=table.rows[0]
                        ),
                    )

            open_deletion()
            await user.should_see("Delete manual example?")
            await user.should_see("Category: Food")
            user.find(kind=ui.button, content="Cancel").click()
            assert len(example_rows(engine)) == 1
            open_deletion()
            if replace_before_delete:
                correct_transaction(engine, tid, food)
            user.find(kind=ui.button, content="Delete example").click()
            if replace_before_delete:
                await user.should_see(
                    "Example changed or was deleted elsewhere. Refresh and try again."
                )
                assert len(example_rows(engine)) == 1
                user.find(kind=ui.button, content="Cancel").click()
                user.find(marker="manual-examples-table").elements.pop().mark("stale-examples")
                user.find("Apply filters").click()
                await user.should_not_see(marker="stale-examples")
                open_deletion()
                user.find(kind=ui.button, content="Delete example").click()
            await user.should_see("0 manual examples")
            await user.should_see("No manual examples yet.")
            panels = list(user.find(ui.tab_panels).elements)
            assert any(panel.value == "Manual examples" for panel in panels)
            assert example_rows(engine) == []

    try:
        asyncio.run(scenario())
        if not replace_before_delete:
            assert transaction_rows(engine) == before
    finally:
        engine.dispose()


def test_categorization_uses_applied_transaction_filters(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "local-filters.sqlite3")
    import_content(
        engine,
        b"a;Shop;01.03.2024;01.03.2024;-10,00;EUR\na;Other;02.03.2024;02.03.2024;-20,00;EUR\n",
        "synthetic.csv",
    )
    seen: list[str] = []

    class Advisor:
        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise NotImplementedError("This scenario only categorizes transactions.")

        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model
            seen.extend(t["description"] for t in payload["transactions"])
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
        async with user_simulation(
            lambda: build_page(engine, Advisor(), ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            user.find(kind=ui.tab, content="Transactions").click()
            user.find("Description contains").type("Shop")
            user.find("Apply filters").click()
            await user.should_see("1 matching transactions")
            # A draft edit must not change the categorization selection.
            user.find("Description contains").clear().type("Other")
            user.find(kind=ui.tab, content="Analysis").click()
            user.find("Analysis description contains").type("No matching description")
            user.find("Apply analysis filters").click()
            await user.should_see("No matching history.")
            user.find(kind=ui.button, content="Categorize").click()
            user.find("Recalculate categories — Transactions filters").click()
            await user.should_see("Completed: 1 applied")
            assert seen == ["Shop"]
            rows = {r["description"]: r for r in transaction_rows(engine)}
            assert rows["Shop"]["source"] == "ai"
            assert rows["Other"]["source"] == "unassigned"
            user.find(marker="close-data-tools").click()
            user.find(kind=ui.tab, content="Transactions").click()
            draft = user.find("Description contains").elements.pop()
            assert isinstance(draft, ui.input)
            assert draft.value == "Other"
            await user.should_see("1 matching transactions")

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_empty_overview_updates_after_import_and_correction(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "overview-refresh.sqlite3")

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(engine, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            await user.should_see("No transactions yet.")
            upload = user.find(ui.upload).elements.pop()
            with user:
                await upload.handle_uploads(
                    [
                        ui.upload.SmallFileUpload(
                            "synthetic.csv",
                            "text/csv",
                            b"a;Shop;01.03.2024;01.03.2024;-10,00;EUR\n",
                        )
                    ]
                )
            await user.should_see(marker="overview-spending_cents", content="0.00 EUR")
            food = save_category(engine, "Food", "Food spending")
            correct_transaction(engine, transaction_rows(engine)[0]["id"], food)
            # Another successful upload refreshes the dashboard without changing its month.
            with user:
                await upload.handle_uploads(
                    [
                        ui.upload.SmallFileUpload(
                            "repeat.csv",
                            "text/csv",
                            b"a;Shop;01.03.2024;01.03.2024;-10,00;EUR\n",
                        )
                    ]
                )
            await user.should_see(marker="overview-spending_cents", content="10.00 EUR")
            plot = user.find(marker="overview-category-chart").elements.pop()
            assert isinstance(plot, AnalysisPlot)
            assert isinstance(plot.figure, go.Figure)
            trace = plot.figure.data[0]
            assert isinstance(trace, go.Bar)
            assert list(zip(trace.y, trace.x, strict=True)) == [("Food", 10)]
            assert any(panel.value == "Overview" for panel in user.find(ui.tab_panels).elements)

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
