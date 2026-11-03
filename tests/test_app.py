"""Dash HTTP workflows preserve imports, edits, examples, reports, and AI recovery."""

import base64
import logging
import threading
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any, override
from unittest.mock import Mock

import pytest
from dash import Dash
from sqlalchemy.engine import Engine

from fyntok.ai import AI_MODEL, AIClient, AIRequestError
from fyntok.ai_ui import AIWork
from fyntok.app import create_app, serve
from fyntok.auth import ChatGPTSession
from fyntok.categorization import cancel_run, run_progress, start_run
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import categories, correct_transaction, save_category
from fyntok.queries import example_rows, transaction_rows
from tests.test_dash_driver import DashDriver


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = open_database(tmp_path / "dash.sqlite3")
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def browser(engine: Engine, tmp_path: Path) -> Iterator[DashDriver]:
    app = create_app(engine, auth=ChatGPTSession(tmp_path / "credentials"))
    try:
        yield DashDriver(app)
    finally:
        app.server.extensions["fyntok-ai"].close()


def seed(engine: Engine) -> str:
    import_content(engine, b"a;Example Market;01.01.2024;02.01.2024;-10,00;EUR\n", "synthetic.csv")
    return save_category(engine, "Food", "Food purchases")


def test_dash_layout_dependencies_assets_and_empty_reports(browser: DashDriver) -> None:
    assert browser.client.get("/").status_code == 200
    dependencies = browser.client.get("/_dash-dependencies")
    assert dependencies.status_code == 200
    assert len(dependencies.get_json()) >= 20
    assert browser.client.get("/assets/dashboard.css").status_code == 200
    browser.refresh()
    assert "No transactions yet" in str(browser.get("overview-report", "children"))
    browser.call("panel-0.style", "tabs", "value", "Transactions")
    assert browser.get("panel-0", "style") == {"display": "none"}
    assert browser.get("panel-2", "style") == {}
    browser.call("tools-open.data", "open-import")
    assert browser.get("tools-tabs", "value") == "import"
    browser.call("tools-import.style", "tools-tabs", "value", "categorization")
    assert browser.get("tools-import", "style") == {"display": "none"}
    browser.call("tools-open.data", "open-categorization")
    browser.call("tools-open.data", "tools-close")
    assert browser.get("tools-open", "data") is None


def test_upload_deduplication_and_atomic_errors(browser: DashDriver, engine: Engine) -> None:
    payload = b"a;Example Market;01.01.2024;02.01.2024;-10,00;EUR\n"
    browser.set("csv-upload", "filename", "synthetic.csv")
    contents = "data:text/csv;base64," + base64.b64encode(payload).decode()
    for expected in ["1 new", "0 new"]:
        browser.call("import-status.children", "csv-upload", "contents", contents)
        assert expected in browser.get("import-status", "children")
    browser.refresh()
    assert len(transaction_rows(engine)) == 1
    assert len(browser.get("imports-table", "rowData")) == 2
    browser.call("import-status.children", "csv-upload", "contents", "invalid")
    assert len(transaction_rows(engine)) == 1
    browser.call("import-status.children", "csv-upload", "contents", "data:text/csv;base64,@@@")
    assert len(transaction_rows(engine)) == 1
    browser.call("import-status.children", "csv-upload", "contents", "")


def test_manual_correction_conflict_undo_and_example_deletion(
    browser: DashDriver,
    engine: Engine,
) -> None:
    category = seed(engine)
    browser.refresh()
    row = browser.get({"type": "transactions", "index": "main"}, "rowData")[0]
    browser.select(row)
    assert "Full description" in str(browser.get("transaction-description", "children"))
    browser.set("transaction-assignment", "value", category)
    browser.call("transaction-status.children", "transaction-save")
    assert browser.get("selected-transaction", "data") is None
    assert transaction_rows(engine)[0]["protected"]
    assert len(example_rows(engine)) == 1
    browser.refresh()
    browser.set("examples-table", "selectedRows", browser.get("examples-table", "rowData"))
    browser.call("example-status.children", "example-delete-confirm", "submit_n_clicks")
    assert not example_rows(engine)
    assert transaction_rows(engine)[0]["category_id"] == category
    # A stale details snapshot cannot overwrite a newer decision.
    browser.set("selected-transaction", "data", row)
    browser.call("transaction-status.children", "transaction-save")
    assert "changed elsewhere" in browser.get("transaction-status", "children")
    browser.select(browser.get({"type": "transactions", "index": "main"}, "rowData")[0])
    browser.call("transaction-status.children", "transaction-undo")
    assert transaction_rows(engine)[0]["category_id"] == "uncategorized"
    browser.refresh()
    browser.select(browser.get({"type": "transactions", "index": "main"}, "rowData")[0])
    browser.call("transaction-status.children", "transaction-close")
    browser.call("transaction-status.children", "transaction-save")  # no selected row


def test_category_lifecycle_and_stale_editor(browser: DashDriver, engine: Engine) -> None:
    category = seed(engine)
    browser.refresh()
    browser.call("category-name.value", "category-edit", "value", None)
    browser.set("category-name", "value", "Travel")
    browser.set("category-definition", "value", "Transport and accommodation")
    browser.call("category-status.children", "category-save")
    travel = next(row for row in categories(engine) if row["name"] == "Travel")
    browser.call("category-name.value", "category-edit", "value", category)
    browser.set("category-name", "value", "Groceries")
    browser.call("category-status.children", "category-save")
    assert next(row for row in categories(engine) if row["id"] == category)["name"] == "Groceries"
    # Reusing the captured revision is rejected.
    browser.call("category-status.children", "category-save")
    assert "changed elsewhere" in browser.get("category-status", "children")
    browser.call("category-name.value", "category-edit", "value", category)
    browser.set("category-destination", "value", travel["id"])
    correct_transaction(engine, transaction_rows(engine)[0]["id"], category)
    browser.call("category-status.children", "category-merge-confirm", "submit_n_clicks")
    assert transaction_rows(engine)[0]["category_id"] == travel["id"]
    browser.call("category-name.value", "category-edit", "value", travel["id"])
    browser.set("category-destination", "value", "uncategorized")
    browser.call("category-status.children", "category-delete-confirm", "submit_n_clicks")
    assert transaction_rows(engine)[0]["category_id"] == "uncategorized"
    browser.call("category-name.value", "category-edit", "value", "uncategorized")
    browser.call("category-status.children", "category-delete-confirm", "submit_n_clicks")
    assert "Uncategorized" in browser.get("category-status", "children")


def test_applied_filters_remain_independent_and_invalid_dates_keep_last_scope(
    browser: DashDriver,
    engine: Engine,
) -> None:
    seed(engine)
    browser.refresh()
    browser.set("transaction-search", "value", "Missing")
    browser.call("transaction-filters.data", "transaction-apply")
    browser.call(
        '"index":"main"', "transaction-filters", "data", browser.get("transaction-filters", "data")
    )
    assert not browser.get({"type": "transactions", "index": "main"}, "rowData")
    assert browser.get("analysis-filters", "data") == {}
    scope = browser.get("transaction-filters", "data")
    browser.set("transaction-from", "date", "2024-02-01")
    browser.set("transaction-through", "date", "2024-01-01")
    browser.call("transaction-filters.data", "transaction-apply")
    assert "Start date" in browser.get("transaction-error", "children")
    assert browser.get("transaction-filters", "data") == scope
    browser.call("transaction-filters.data", "transaction-clear")
    assert browser.get("transaction-from", "date") is None
    browser.set("analysis-search", "value", "Other")
    browser.call("analysis-filters.data", "analysis-apply")
    assert browser.get("transaction-filters", "data") == {}
    browser.call("analysis-filters.data", "analysis-clear")


class Advisor:
    def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert model == AI_MODEL
        assert payload["descriptions"]
        return {
            "categories": [{"name": "Food", "definition": "Food purchases", "kind": "spending"}]
        }

    def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert model == AI_MODEL
        assert not payload["confirmed_examples"]
        category = next(row["id"] for row in payload["categories"] if row["name"] == "Food")
        return {
            "assignments": [
                {
                    "transaction_id": row["transaction_id"],
                    "category_id": category,
                    "reason": "Scripted suggestion",
                }
                for row in payload["transactions"]
            ]
        }


def ai_browser(
    engine: Engine,
    tmp_path: Path,
    client: AIClient | None = None,
    auth: ChatGPTSession | None = None,
) -> DashDriver:
    app = create_app(engine, client or Advisor(), auth or ChatGPTSession(tmp_path / "credentials"))
    return DashDriver(app)


def wait_for_work(browser: DashDriver) -> None:
    work = browser.app.server.extensions["fyntok-ai"]
    assert isinstance(work, AIWork)
    assert work.future is not None
    with suppress(ValueError, AIRequestError):
        work.future.result(timeout=10)
    browser.call("ai-polled.data", "ai-poll", "n_intervals", 1)


def test_proposal_edits_survive_polling_and_tools_navigation(
    engine: Engine, tmp_path: Path
) -> None:
    seed(engine)
    browser = ai_browser(engine, tmp_path)
    try:
        browser.call("ai-task.data", "ai-propose")
        wait_for_work(browser)
        suggestions = browser.get("ai-proposals", "rowData")
        suggestions[0]["name"] = "Reviewed food"
        browser.set("ai-proposals", "rowData", suggestions)
        browser.call("ai-polled.data", "ai-poll", "n_intervals", 2)
        browser.call("tools-open.data", "tools-close")
        browser.call("tools-open.data", "open-categorization")
        assert browser.get("ai-proposals", "rowData")[0]["name"] == "Reviewed food"
        browser.set("ai-proposals", "selectedRows", suggestions)
        browser.call("ai-task.data", "ai-approve")
        assert any(row["name"] == "Reviewed food" for row in categories(engine))
        assert not browser.get("ai-proposals", "rowData")
        browser.call("ai-task.data", "ai-propose")
        wait_for_work(browser)
        browser.set("ai-proposals", "selectedRows", browser.get("ai-proposals", "rowData"))
        browser.call("ai-task.data", "ai-dismiss")
        assert not browser.get("ai-proposals", "rowData")
    finally:
        browser.app.server.extensions["fyntok-ai"].close()


def test_filtered_categorization_uses_applied_scope_and_recovery(
    engine: Engine, tmp_path: Path
) -> None:
    seed(engine)
    import_content(engine, b"a;Other;02.01.2024;02.01.2024;-5,00;EUR\n", "other.csv")
    browser = ai_browser(engine, tmp_path)
    try:
        browser.set("transaction-search", "value", "Example")
        browser.call("transaction-filters.data", "transaction-apply")
        browser.set("transaction-search", "value", "Other")
        browser.call(
            "ai-count.children",
            "transaction-filters",
            "data",
            browser.get("transaction-filters", "data"),
        )
        assert "1 in applied" in browser.get("ai-count", "children")
        browser.call("ai-task.data", "ai-run-filtered")
        wait_for_work(browser)
        run_id = browser.get("ai-run", "value")
        assert run_progress(engine, run_id)["total"] == 1
        assert "completed" in browser.get("ai-progress", "children")
        assert not example_rows(engine)
        pending = start_run(engine, AI_MODEL)
        cancel_run(engine, pending)
        browser.set("ai-run", "value", pending)
        browser.call("ai-task.data", "ai-resume")
        wait_for_work(browser)
        assert run_progress(engine, pending)["status"] == "completed"
        assert all(row["category"] == "Food" for row in transaction_rows(engine))
        browser.call("ai-task.data", "ai-run-all")
        wait_for_work(browser)
        browser.call("ai-task.data", "ai-cancel")
    finally:
        browser.app.server.extensions["fyntok-ai"].close()


@pytest.mark.parametrize("action", ["propose", "run-all"])
def test_ai_failure_logs_traceback_and_keeps_manual_workflows(
    engine: Engine,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    action: str,
) -> None:
    seed(engine)

    class Unavailable(Advisor):
        @override
        def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise AIRequestError(503)

        @override
        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            del model, payload
            raise AIRequestError(503)

    browser = ai_browser(engine, tmp_path, Unavailable())
    try:
        with caplog.at_level(logging.ERROR):
            browser.call("ai-task.data", f"ai-{action}")
            wait_for_work(browser)
        browser.refresh()
        assert browser.get({"type": "transactions", "index": "main"}, "rowData")
        if action == "propose":
            assert "temporarily unavailable" in browser.get("ai-status", "children")
        else:
            assert "failed" in browser.get("ai-progress", "children")
        assert any(record.exc_info for record in caplog.records)
    finally:
        browser.app.server.extensions["fyntok-ai"].close()


def test_background_work_keeps_manual_ui_and_cancel_responsive(
    engine: Engine, tmp_path: Path
) -> None:
    seed(engine)
    entered, release = threading.Event(), threading.Event()

    class Blocking(Advisor):
        @override
        def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
            entered.set()
            assert release.wait(10)
            return super().classify(model, payload)

    browser = ai_browser(engine, tmp_path, Blocking())
    try:
        browser.call("ai-task.data", "ai-run-all")
        assert entered.wait(10)
        browser.call("tools-open.data", "tools-close")
        browser.refresh()
        browser.call("ai-polled.data", "ai-poll", "n_intervals", 1)
        assert browser.get("ai-propose", "disabled")
        browser.call("ai-task.data", "ai-signout")
        assert "Wait" in browser.get("ai-status", "children")
        browser.call("ai-task.data", "ai-cancel")
        assert run_progress(engine, browser.get("ai-run", "value"))["status"] == "cancelled"
        release.set()
        wait_for_work(browser)
        assert transaction_rows(engine)[0]["category_id"] == "uncategorized"
    finally:
        release.set()
        browser.app.server.extensions["fyntok-ai"].close()


def test_auth_account_selection_login_and_signout(engine: Engine, tmp_path: Path) -> None:
    seed(engine)
    auth = ChatGPTSession(tmp_path / "credentials")
    auth.select = Mock()
    auth.accounts = Mock(return_value={"one": "Account one", "two": "Account two"})
    auth.active_account = Mock(return_value="one")
    auth.start_login = Mock(return_value="https://example.invalid/sign-in")
    auth.is_connected = Mock(return_value=False)
    auth.sign_out = Mock(return_value=False)
    browser = ai_browser(engine, tmp_path, auth=auth)
    try:
        browser.set("ai-account", "value", "two")
        browser.call("ai-task.data", "ai-login")
        wait_for_work(browser)
        assert browser.get("ai-login-link", "href") == "https://example.invalid/sign-in"
        auth.start_login.assert_called_once_with("two")
        browser.call("ai-task.data", "ai-signout")
        wait_for_work(browser)
        auth.select.assert_called_with("two")
        assert "Remote revocation was not confirmed" in browser.get("ai-status", "children")
        auth.is_connected.return_value = True
        browser.call("ai-polled.data", "ai-poll", "n_intervals", 3)
        assert "Connected" in browser.get("ai-connection", "children")
        assert browser.get("ai-login-link", "style") == {"display": "none"}
    finally:
        browser.app.server.extensions["fyntok-ai"].close()


def test_server_options_and_cleanup(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(Dash, "run", run)
    serve(engine, port=8081, host="127.0.0.2", reload=True, log_level="debug")
    assert run.call_args.kwargs["port"] == 8081
    assert run.call_args.kwargs["use_reloader"] is True
    assert run.call_args.kwargs["use_debugger"] is False
    run.side_effect = RuntimeError("Server failed")
    with pytest.raises(RuntimeError, match="Server failed"):
        serve(engine)


def test_removed_category_clears_applied_scope_but_preserves_drafts(
    browser: DashDriver,
    engine: Engine,
) -> None:
    category = seed(engine)
    browser.refresh()
    browser.set("transaction-category", "value", category)
    browser.set("transaction-search", "value", "Example")
    browser.call("transaction-filters.data", "transaction-apply")
    browser.set("transaction-search", "value", "Unapplied draft")
    browser.call("category-name.value", "category-edit", "value", category)
    browser.call("category-status.children", "category-delete-confirm", "submit_n_clicks")
    browser.refresh()
    scope = browser.get("transaction-filters", "data")
    assert scope["category_id"] is None
    assert scope["search"] == "Example"
    assert browser.get("transaction-category", "value") is None
    assert browser.get("transaction-search", "value") == "Unapplied draft"
    browser.call("ai-count.children", "transaction-filters", "data", scope)
    assert "1 in applied" in browser.get("ai-count", "children")
