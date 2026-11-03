"""Local Dash dashboard; manual workflows work without AI access."""

import base64
import binascii
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .accounts import ACCOUNT_KINDS, TREATMENTS, account_rows, review_repayment, update_account
from .ai import AIClient, ResponsesClient
from .ai_ui import AIWork, build_ai_panel, register_ai
from .analysis_ui import filter_data, read_filters, register_report, report_layout
from .auth import ChatGPTSession
from .dash_ui import callback, columns, dropdown, grid, money, text_input, transaction_grid
from .date_ui import iso_date_input
from .importer import import_content
from .manual import (
    assignment_history,
    categories,
    correct_transaction,
    delete_example,
    remove_category,
    save_category,
    undo_assignment,
)
from .models import Import
from .overview_ui import build_overview, register_overview
from .queries import example_rows, transaction_rows

logger = logging.getLogger(__name__)
TABS = ["Overview", "Analysis", "Transactions", "Categories", "Manual examples", "Import history"]


def filter_controls(prefix: str) -> html.Div:
    return html.Div(
        [
            iso_date_input(f"{prefix}-from", "Booking date from"),
            iso_date_input(f"{prefix}-through", "Through"),
            dropdown(f"{prefix}-category", "Category", []),
            text_input(f"{prefix}-search", "Description contains"),
            html.Button("Apply filters", id=f"{prefix}-apply"),
            html.Button("Clear", id=f"{prefix}-clear"),
            html.Div(id=f"{prefix}-error", role="alert", className="error"),
            dcc.Store(id=f"{prefix}-filters", data={}),
        ],
        className="controls",
    )


def category_editor() -> html.Section:
    return html.Section(
        [
            html.H2("Categories"),
            dropdown("category-edit", "Edit category (clear to create)", []),
            text_input("category-name", "Name"),
            html.Label("Definition", htmlFor="category-definition"),
            dcc.Textarea(id="category-definition", value="", style={"width": "100%"}),
            dropdown(
                "category-kind",
                "Financial type",
                ["spending", "income", "transfer"],
                "spending",
                clearable=False,
            ),
            html.Button("Save category", id="category-save"),
            html.P(
                "Merge or delete moves current assignments and confirmed examples to the selected "
                "destination. Uncategorized is permanent. "
                "History and manual protection are retained."
            ),
            dropdown("category-destination", "Destination", [], "uncategorized", clearable=False),
            html.Div(
                [
                    dcc.ConfirmDialogProvider(
                        html.Button("Merge category", id="category-merge"),
                        id="category-merge-confirm",
                        message="Merge this category into the selected destination? "
                        "Assignments and examples will move; this cannot be undone.",
                    ),
                    dcc.ConfirmDialogProvider(
                        html.Button("Delete category", id="category-delete"),
                        id="category-delete-confirm",
                        message="Delete this category and move its assignments and examples "
                        "to the selected destination? This cannot be undone.",
                    ),
                ],
                className="actions",
            ),
            html.Div(id="category-status", role="status", className="status"),
            dcc.Store(id="category-snapshot"),
            grid("categories-table", [], columns("name", "definition", "kind")),
        ]
    )


def import_panel() -> html.Div:
    return html.Div(
        [
            html.H2("Import CSV"),
            html.P(
                "Headerless CP1252 CSV; semicolon separators and German dates/amounts. "
                "Repeated exports reuse existing rows. Accepts bank and credit-card exports."
            ),
            dropdown(
                "import-kind",
                "Import account type",
                {"auto": "Detect new accounts", **ACCOUNT_KINDS},
                "auto",
                clearable=False,
            ),
            dcc.Upload(
                id="csv-upload",
                children=html.Div("Choose or drop a bank export (.csv)"),
                accept=".csv",
                multiple=False,
                className="upload",
            ),
            html.Div(id="import-status", role="status", className="status"),
            html.H3("Account settings"),
            dropdown("account-edit", "Account", []),
            text_input("account-label", "Account label"),
            dropdown("account-kind", "Account type", ACCOUNT_KINDS, "bank", clearable=False),
            html.P("Changing the type rechecks repayment matching, not purchase categories."),
            html.Button("Save account", id="account-save"),
            html.Div(id="account-status", role="status", className="status"),
        ]
    )


def transaction_editor() -> html.Dialog:
    return html.Dialog(
        [
            html.H2("Transaction details"),
            html.Div(id="transaction-description"),
            dropdown("transaction-assignment", "Category", [], "uncategorized", clearable=False),
            html.P(
                "Saving confirms only this transaction and replaces its previous confirmed example."
            ),
            html.Div(
                [
                    html.Button("Confirm individual category", id="transaction-save"),
                    html.Button("Undo latest assignment change", id="transaction-undo"),
                    html.Button("Close", id="transaction-close"),
                ],
                className="actions",
            ),
            html.Div(
                [
                    dropdown(
                        "transaction-treatment",
                        "Repayment treatment",
                        TREATMENTS,
                        "auto",
                        clearable=False,
                    ),
                    html.P(
                        "Automatic treatment excludes card credits and uniquely matched "
                        "bank withdrawals. "
                        "Unmatched bank withdrawals stay in totals. "
                        "Internal transfer excludes this "
                        "entry only; review the opposite entry separately. Include in totals still "
                        "respects an Internal transfer category."
                    ),
                    html.Button("Save repayment treatment", id="transaction-repayment"),
                ],
                id="transaction-repayment-controls",
            ),
            html.Div(id="transaction-status", role="alert", className="status"),
            html.H3("Assignment history"),
            grid("assignment-history", [], columns("created_at", "source", "category", "reason")),
        ],
        id="transaction-dialog",
        **{"aria-label": "Transaction details"},
    )


def build_page(engine: Engine, auth: ChatGPTSession) -> html.Main:
    panels = [
        build_overview(),
        html.Div([filter_controls("analysis"), report_layout("analysis")]),
        html.Section(
            [
                filter_controls("transaction"),
                html.P(id="transaction-count"),
                transaction_grid("main", []),
            ]
        ),
        category_editor(),
        html.Section(
            [
                html.H2("Manual examples"),
                html.P(
                    "Only human-confirmed decisions teach AI. Select a row to remove its teaching "
                    "example; the transaction, assignment and history are retained."
                ),
                grid(
                    "examples-table",
                    [],
                    columns(
                        "booking_date",
                        "description",
                        "category",
                        "amount",
                        "currency",
                        money_columns=("amount",),
                    ),
                ),
                dcc.ConfirmDialogProvider(
                    html.Button("Delete selected example", id="example-delete"),
                    id="example-delete-confirm",
                    message="Delete this teaching example? Its transaction category, "
                    "manual protection, and assignment history will be retained.",
                ),
                html.Div(id="example-status", role="status", className="status"),
            ]
        ),
        html.Section(
            [
                html.H2("Import history"),
                grid(
                    "imports-table",
                    [],
                    columns(
                        "filename", "created_at", "row_count", "inserted_count", "duplicate_count"
                    ),
                ),
            ]
        ),
    ]
    return html.Main(
        [
            dcc.Store(id="revision", data=str(uuid4())),
            dcc.Store(id="selected-transaction"),
            dcc.Store(id="tools-open"),
            dcc.Store(id="dialog-sync"),
            dcc.Store(id="chart-resize"),
            html.Header(
                [
                    html.H1("fyntok"),
                    html.Div(
                        [
                            html.Button("Import CSV", id="open-import"),
                            html.Button("Categorize", id="open-categorization"),
                        ],
                        className="actions",
                    ),
                ]
            ),
            html.P("Explore your history and refine one transaction at a time.", className="muted"),
            dcc.Tabs(
                id="tabs",
                value="Overview",
                children=[dcc.Tab(label=tab, value=tab) for tab in TABS],
            ),
            *[
                html.Div(
                    panel, id=f"panel-{index}", style={} if index == 0 else {"display": "none"}
                )
                for index, panel in enumerate(panels)
            ],
            html.Dialog(
                [
                    html.Div(
                        [html.H2("Data tools"), html.Button("Close data tools", id="tools-close")],
                        className="actions",
                    ),
                    dcc.Tabs(
                        id="tools-tabs",
                        value="import",
                        children=[
                            dcc.Tab(label="Import", value="import"),
                            dcc.Tab(label="Categorization", value="categorization"),
                        ],
                    ),
                    html.Div(import_panel(), id="tools-import"),
                    html.Div(
                        build_ai_panel(engine, auth),
                        id="tools-categorization",
                        style={"display": "none"},
                    ),
                ],
                id="tools-dialog",
                **{"aria-label": "Data tools"},
            ),
            transaction_editor(),
        ]
    )


def register_filters(app: Dash, engine: Engine, prefix: str) -> None:
    @callback(
        app,
        Output(f"{prefix}-filters", "data"),
        Output(f"{prefix}-error", "children"),
        Output(f"{prefix}-from", "date"),
        Output(f"{prefix}-through", "date"),
        Output(f"{prefix}-category", "value"),
        Output(f"{prefix}-search", "value"),
        Input(f"{prefix}-apply", "n_clicks"),
        Input(f"{prefix}-clear", "n_clicks"),
        Input("revision", "data"),
        State(f"{prefix}-from", "date"),
        State(f"{prefix}-through", "date"),
        State(f"{prefix}-category", "value"),
        State(f"{prefix}-search", "value"),
        State(f"{prefix}-filters", "data"),
        prevent_initial_call=True,
    )
    def apply(
        _apply: int,
        _clear: int,
        _revision: str,
        start: str | None,
        end: str | None,
        category: str | None,
        search: str | None,
        applied: dict[str, Any],
    ) -> tuple[Any, ...]:
        if ctx.triggered_id == f"{prefix}-clear":
            return {}, "", None, None, None, ""
        if ctx.triggered_id == "revision":
            active = {row["id"] for row in categories(engine)}
            filters = read_filters(applied)
            updated: Any = no_update
            if filters.category_id is not None and filters.category_id not in active:
                updated = filter_data(replace(filters, category_id=None))
            selection: Any = no_update if category in active else None
            return updated, no_update, no_update, no_update, selection, no_update
        try:
            filters = read_filters(
                {"start": start, "end": end, "category_id": category, "search": search or ""}
            )
        except ValueError as exc:
            return no_update, str(exc), *([no_update] * 4)
        logger.debug("Applied %s filters", prefix)
        return filter_data(filters), "", *([no_update] * 4)


def register_manual(app: Dash, engine: Engine) -> None:
    @callback(
        app,
        Output("categories-table", "rowData"),
        Output("examples-table", "rowData"),
        Output("imports-table", "rowData"),
        Output("account-edit", "options"),
        *[
            Output(identity, "options")
            for identity in [
                "transaction-category",
                "analysis-category",
                "transaction-assignment",
                "category-edit",
                "category-destination",
            ]
        ],
        Input("revision", "data"),
    )
    def refresh(_revision: str) -> tuple[Any, ...]:
        records = categories(engine)
        options = [{"label": row["name"], "value": row["id"]} for row in records]
        with Session(engine) as session:
            imports = [
                {
                    "id": item.id,
                    "filename": item.filename,
                    "created_at": item.created_at.isoformat(),
                    "row_count": item.row_count,
                    "inserted_count": item.inserted_count,
                    "duplicate_count": item.duplicate_count,
                }
                for item in session.scalars(select(Import).order_by(Import.created_at.desc()))
            ]
        from .dash_ui import json_rows

        accounts = [{"label": row["label"], "value": row["id"]} for row in account_rows(engine)]
        return records, json_rows(example_rows(engine)), imports, accounts, *([options] * 5)

    @callback(
        app,
        Output({"type": "transactions", "index": "main"}, "rowData"),
        Output("transaction-count", "children"),
        Input("revision", "data"),
        Input("transaction-filters", "data"),
    )
    def transactions(_revision: str, data: dict[str, Any]) -> tuple[Any, str]:
        from .dash_ui import json_rows

        filters = read_filters(data)
        if filters.category_id not in {row["id"] for row in categories(engine)}:
            filters = replace(filters, category_id=None)
        rows = json_rows(transaction_rows(engine, filters))
        return rows, f"{len(rows):,} matching transactions. Click a row to inspect or correct it."

    @callback(
        app,
        Output("import-status", "children"),
        Output("revision", "data", allow_duplicate=True),
        Input("csv-upload", "contents"),
        State("csv-upload", "filename"),
        State("import-kind", "value"),
        prevent_initial_call=True,
    )
    def upload(content: str | None, filename: str, kind: str) -> tuple[Any, Any]:
        if not bool(content):
            raise PreventUpdate
        try:
            payload = base64.b64decode(content.split(",", 1)[1], validate=True)
            result = import_content(
                engine, payload, filename, account_kind=None if kind == "auto" else kind
            )
            logger.info(
                "CSV import completed: %d new, %d duplicates",
                result.inserted_count,
                result.duplicate_count,
            )
            return (
                f"Imported {result.inserted_count:,} new transactions; "
                f"{result.duplicate_count:,} already present.",
                str(uuid4()),
            )
        except (ValueError, SQLAlchemyError, UnicodeError, binascii.Error, IndexError) as exc:
            logger.exception("CSV import failed")
            return str(exc), no_update

    @callback(
        app,
        Output("account-label", "value"),
        Output("account-kind", "value"),
        Input("account-edit", "value"),
    )
    def account(identity: str | None) -> tuple[str, str]:
        row = next((row for row in account_rows(engine) if row["id"] == identity), None)
        return (row["label"], row["kind"]) if row is not None else ("", "bank")

    @callback(
        app,
        Output("account-status", "children"),
        Output("revision", "data", allow_duplicate=True),
        Input("account-save", "n_clicks"),
        State("account-edit", "value"),
        State("account-label", "value"),
        State("account-kind", "value"),
        prevent_initial_call=True,
    )
    def save_account(_click: int, identity: str | None, label: str, kind: str) -> tuple[Any, Any]:
        try:
            if not bool(identity):
                raise ValueError("Select an account first.")
            update_account(engine, identity, label, kind)
            return "Account saved.", str(uuid4())
        except (ValueError, SQLAlchemyError) as exc:
            logger.exception("Edit account failed")
            return str(exc), no_update

    @callback(
        app,
        Output("category-name", "value"),
        Output("category-definition", "value"),
        Output("category-kind", "value"),
        Output("category-snapshot", "data"),
        Input("category-edit", "value"),
    )
    def edit_category(identity: str | None) -> tuple[Any, ...]:
        row = next((row for row in categories(engine) if row["id"] == identity), None)
        return (
            (row["name"], row["definition"], row["kind"], row)
            if row is not None
            else ("", "", "spending", None)
        )

    @callback(
        app,
        Output("category-status", "children"),
        Output("revision", "data", allow_duplicate=True),
        Output("category-edit", "value"),
        Input("category-save", "n_clicks"),
        Input("category-delete-confirm", "submit_n_clicks"),
        Input("category-merge-confirm", "submit_n_clicks"),
        State("category-snapshot", "data"),
        State("category-name", "value"),
        State("category-definition", "value"),
        State("category-kind", "value"),
        State("category-destination", "value"),
        prevent_initial_call=True,
    )
    def save_category_action(
        _save: int,
        _delete: int,
        _merge: int,
        row: dict[str, Any] | None,
        name: str,
        definition: str,
        kind: str,
        destination: str,
    ) -> tuple[Any, ...]:
        try:
            if ctx.triggered_id == "category-save":
                save_category(
                    engine,
                    name or "",
                    definition or "",
                    kind,
                    category_id=row["id"] if row is not None else None,
                    expected_revision=row["revision"] if row is not None else None,
                )
            else:
                if row is None:
                    raise ValueError("Select a category first.")
                remove_category(engine, row["id"], destination, expected_revision=row["revision"])
            logger.debug("Category action completed: %s", ctx.triggered_id)
            return "Category changes saved.", str(uuid4()), None
        except (ValueError, SQLAlchemyError) as exc:
            logger.exception("Category action failed")
            return str(exc), no_update, no_update

    @callback(
        app,
        Output("example-status", "children"),
        Output("revision", "data", allow_duplicate=True),
        Input("example-delete-confirm", "submit_n_clicks"),
        State("examples-table", "selectedRows"),
        prevent_initial_call=True,
    )
    def remove_example(_click: int, selected: list[dict[str, Any]] | None) -> tuple[Any, Any]:
        try:
            if not bool(selected):
                raise ValueError("Select an example first.")
            delete_example(
                engine, selected[0]["id"], expected_assignment_id=selected[0]["assignment_id"]
            )
            return "Example deleted. The assignment and history are retained.", str(uuid4())
        except (ValueError, SQLAlchemyError) as exc:
            logger.exception("Delete example failed")
            return str(exc), no_update


def register_transactions(app: Dash, engine: Engine) -> None:
    @callback(
        app,
        Output("selected-transaction", "data", allow_duplicate=True),
        Input({"type": "transactions", "index": ALL}, "cellClicked"),
        State({"type": "transactions", "index": ALL}, "rowData"),
        State({"type": "transactions", "index": ALL}, "id"),
        prevent_initial_call=True,
    )
    def select_transaction(clicks: list[Any], rows: list[Any], identities: list[Any]) -> Any:
        if not isinstance(ctx.triggered_id, dict):
            raise PreventUpdate
        index = identities.index(ctx.triggered_id)
        click = clicks[index]
        if not click:
            raise PreventUpdate
        row = next((row for row in rows[index] if row["id"] == click["rowId"]), None)
        if row is None:
            raise PreventUpdate
        return row

    @callback(
        app,
        Output("transaction-description", "children"),
        Output("transaction-assignment", "value"),
        Output("transaction-treatment", "value"),
        Output("transaction-repayment-controls", "style"),
        Output("assignment-history", "rowData"),
        Output("transaction-status", "children", allow_duplicate=True),
        Input("selected-transaction", "data"),
        prevent_initial_call=True,
    )
    def details(row: dict[str, Any] | None) -> tuple[Any, ...]:
        if row is None:
            raise PreventUpdate
        protected = (
            "protected manual decision"
            if row["protected"]
            else "eligible for future AI recalculation"
        )
        description = [
            html.H3("Cleaned description"),
            html.Pre(row["cleaned_description"]),
            html.H3("Full description"),
            html.Pre(row["description"]),
            html.P(
                f"Booking {row['booking_date']} · Value {row['value_date']} · "
                f"{money(row['amount_cents'])} {row['currency']}"
            ),
            html.P(f"Account: {row['account_label']}"),
            html.P(f"Assignment: {row['source']} · {protected}"),
            html.P(row["repayment_status"]),
        ]
        if row["repayment_partner"]:
            description.append(html.P("Matched to an opposite entry on the other account."))
        return (
            description,
            row["category_id"],
            row["repayment_treatment"] or "auto",
            {} if row["repayment_role"] else {"display": "none"},
            assignment_history(engine, row["id"]),
            "",
        )

    @callback(
        app,
        Output("transaction-status", "children", allow_duplicate=True),
        Output("revision", "data", allow_duplicate=True),
        Output("selected-transaction", "data", allow_duplicate=True),
        *[
            Input(f"transaction-{name}", "n_clicks")
            for name in ["save", "undo", "repayment", "close"]
        ],
        State("selected-transaction", "data"),
        State("transaction-assignment", "value"),
        State("transaction-treatment", "value"),
        prevent_initial_call=True,
    )
    def save_transaction(
        _save: int,
        _undo: int,
        _repayment: int,
        _close: int,
        row: dict[str, Any] | None,
        category: str,
        treatment: str,
    ) -> tuple[Any, ...]:
        if ctx.triggered_id == "transaction-close":
            return "", no_update, None
        if row is None:
            raise PreventUpdate
        try:
            match ctx.triggered_id:
                case "transaction-save":
                    correct_transaction(
                        engine, row["id"], category, expected_revision=row["revision"]
                    )
                case "transaction-undo":
                    undo_assignment(engine, row["id"], expected_revision=row["revision"])
                case _:
                    review_repayment(
                        engine, row["id"], treatment, expected_revision=row["revision"]
                    )
            logger.debug("Transaction action completed: %s", ctx.triggered_id)
            return "Saved.", str(uuid4()), None
        except (ValueError, SQLAlchemyError) as exc:
            logger.exception("Transaction action failed")
            return str(exc), no_update, no_update


def create_app(
    engine: Engine, ai_client: AIClient | None = None, auth: ChatGPTSession | None = None
) -> Dash:
    auth = auth or ChatGPTSession()
    work = AIWork(engine, ai_client or ResponsesClient(auth), auth)
    app = Dash(
        __name__,
        title="fyntok",
        suppress_callback_exceptions=True,
        assets_folder=str(Path(__file__).parent / "assets"),
    )
    app.layout = lambda: build_page(engine, auth)
    # Keep the worker lifecycle available to the CLI and embedded application callers.
    app.server.extensions["fyntok-ai"] = work

    @callback(
        app,
        *[Output(f"panel-{index}", "style") for index in range(len(TABS))],
        Input("tabs", "value"),
    )
    def tab(selected: str) -> list[dict[str, str]]:
        return [{} if name == selected else {"display": "none"} for name in TABS]

    @callback(
        app,
        Output("tools-open", "data"),
        Output("tools-tabs", "value"),
        Input("open-import", "n_clicks"),
        Input("open-categorization", "n_clicks"),
        Input("tools-close", "n_clicks"),
        prevent_initial_call=True,
    )
    def tools(_import: int, _categorize: int, _close: int) -> tuple[Any, Any]:
        if ctx.triggered_id == "tools-close":
            return None, no_update
        selected = "import" if ctx.triggered_id == "open-import" else "categorization"
        return selected, selected

    @callback(
        app,
        Output("tools-import", "style"),
        Output("tools-categorization", "style"),
        Input("tools-tabs", "value"),
    )
    def tool_tab(selected: str) -> tuple[dict[str, str], dict[str, str]]:
        return ({}, {"display": "none"}) if selected == "import" else ({"display": "none"}, {})

    app.clientside_callback(
        """function() {
            // Plotly's window resize handler runs after hidden report panels become visible.
            requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
            return window.dash_clientside.no_update;
        }""",
        Output("chart-resize", "data"),
        Input("panel-0", "style"),
        Input("panel-1", "style"),
    )
    app.clientside_callback(
        """
        function(tools, transaction) {
            for (const [id, value, store] of [
                ['tools-dialog', tools, 'tools-open'],
                ['transaction-dialog', transaction, 'selected-transaction']
            ]) {
                const dialog = document.getElementById(id);
                if (!dialog) continue;
                if (!dialog.dataset.bound) {
                    dialog.addEventListener('cancel', () => {
                        dash_clientside.set_props(store, {data: null});
                    });
                    dialog.dataset.bound = 'true';
                }
                if (value && !dialog.open) dialog.showModal();
                if (!value && dialog.open) dialog.close();
            }
            return window.dash_clientside.no_update;
        }
    """,
        Output("dialog-sync", "data"),
        Input("tools-open", "data"),
        Input("selected-transaction", "data"),
    )
    register_filters(app, engine, "transaction")
    register_filters(app, engine, "analysis")
    register_manual(app, engine)
    register_transactions(app, engine)
    register_overview(app, engine)
    register_report(app, engine, "analysis")
    register_ai(app, work)
    return app


def serve(
    engine: Engine,
    port: int = 8080,
    log_level: str = "info",
    reload: bool = False,
    trace_dir: Path | None = None,
    host: str = "127.0.0.1",
) -> None:
    auth = ChatGPTSession()
    app = create_app(engine, ResponsesClient(auth, trace_dir=trace_dir), auth)
    logging.getLogger("werkzeug").setLevel(log_level.upper())
    logger.info("Starting fyntok at http://%s:%d", host, port)
    try:
        app.run(
            host=host,
            port=port,
            debug=reload or log_level == "debug",
            use_reloader=reload,
            dev_tools_hot_reload=reload,
            dev_tools_silence_routes_logging=False,
            dev_tools_disable_version_check=True,
            # Keep exception handling in our callbacks; never expose an interactive debugger.
            use_debugger=False,
        )
    except Exception:
        logger.exception("fyntok server failed")
        raise
    finally:
        app.server.extensions["fyntok-ai"].close()
        logger.info("fyntok server stopped")
