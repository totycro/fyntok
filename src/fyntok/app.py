"""Local NiceGUI application; all manual workflows work without AI access."""

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from nicegui import events, run, ui
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .ai import AIClient, ResponsesClient
from .analysis import aggregate
from .auth import ChatGPTSession
from .charts import monthly_figure, trend_figure
from .importer import import_content
from .manual import (
    assignment_history,
    categories,
    correct_transaction,
    remove_category,
    save_category,
    undo_assignment,
)
from .models import Import, Transaction
from .queries import Filters, transaction_rows

logger = logging.getLogger(__name__)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def columns(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "name": n,
            "label": n.replace("_", " ").title(),
            "field": n,
            "sortable": True,
            "align": "left",
        }
        for n in names
    ]


def build_page(
    engine: Engine, ai_client: AIClient | None = None, auth: ChatGPTSession | None = None
) -> None:
    ui.label("fyntok").classes("text-3xl font-bold")
    ui.label("Explore your history and refine one transaction at a time.").classes("text-gray-600")
    with ui.expansion("Import CSV", icon="upload").classes("w-full"):
        ui.label(
            "Headerless CP1252 CSV; semicolon separators and German "
            "dates/amounts. Repeated exports reuse existing rows."
        )

        async def upload(event: events.UploadEventArguments) -> None:
            logger.debug("Import CSV: reading upload and importing transactions")
            try:
                result = await run.io_bound(
                    import_content, engine, await event.file.read(), event.file.name
                )
                if result is None:
                    raise RuntimeError("Background operation was cancelled.")
                logger.info(
                    "CSV import completed: %d new transactions, %d duplicates",
                    result.inserted_count,
                    result.duplicate_count,
                )
                ui.notify(
                    (
                        f"Imported {result.inserted_count} new transactions; "
                        f"{result.duplicate_count} already present."
                    ),
                    type="positive",
                )
                refresh()
            except (ValueError, SQLAlchemyError, UnicodeError) as exc:
                logger.exception("CSV import failed (%s)", type(exc).__name__)
                ui.notify(str(exc), type="negative")

        ui.upload(on_upload=upload, auto_upload=True, label="Choose bank export").props(
            "accept=.csv"
        ).classes("w-full")
    with ui.row().classes("items-end w-full"):
        start = ui.input("Booking date from", placeholder="YYYY-MM-DD").props("type=date")
        end = ui.input("Through", placeholder="YYYY-MM-DD").props("type=date")
        category_filter = ui.select({}, label="Category", clearable=True).classes("w-48")
        source = ui.select(
            ["unassigned", "manual", "ai", "system", "undo"],
            label="Assignment source",
            clearable=True,
        ).classes("w-44")
        currency = ui.select([], label="Currency", clearable=True).classes("w-28")
        merchant = ui.select([], label="Merchant", clearable=True, with_input=True).classes("w-64")
        search = ui.input("Description contains").classes("w-64")

        def apply_filters() -> None:
            logger.debug("Apply filters: refreshing analysis and transactions")
            content.refresh()

        ui.button("Apply filters", on_click=apply_filters)

        def clear() -> None:
            logger.debug("Clear filters: resetting filters and refreshing results")
            for control in (start, end, category_filter, source, currency, merchant, search):
                control.value = None
            content.refresh()

        ui.button("Clear", on_click=clear).props("flat")

    def filters() -> Filters:
        f = Filters(
            start=date.fromisoformat(start.value) if start.value else None,
            end=date.fromisoformat(end.value) if end.value else None,
            category_id=category_filter.value,
            source=source.value,
            currency=currency.value,
            merchant=merchant.value,
            search=search.value or "",
        )
        f.validate()
        return f

    from .ai_ui import build_ai_panel

    auth = auth or ChatGPTSession()
    ai_client = ai_client or ResponsesClient(auth)

    def refresh() -> None:
        category_filter.options = {c["id"]: c["name"] for c in categories(engine)}
        if category_filter.value not in category_filter.options:
            category_filter.value = None
        category_filter.update()
        with Session(engine) as session:
            merchant.options = list(
                session.scalars(
                    select(Transaction.merchant).distinct().order_by(Transaction.merchant)
                )
            )
            currency.options = list(
                session.scalars(
                    select(Transaction.currency).distinct().order_by(Transaction.currency)
                )
            )
        merchant.update()
        currency.update()
        content.refresh()

    def perform(action: Callable[[], object], dialog: ui.dialog, label: str) -> None:
        logger.debug("%s: saving changes", label)
        try:
            action()
        except (ValueError, SQLAlchemyError) as exc:
            logger.exception("%s failed (%s)", label, type(exc).__name__)
            ui.notify(str(exc), type="negative")
            return
        dialog.close()
        dialog.delete()
        logger.info("%s: completed", label)
        refresh()

    def close_dialog(dialog: ui.dialog, label: str) -> None:
        logger.debug("%s: closing dialog", label)
        dialog.close()

    def category_editor(category: dict[str, Any] | None = None) -> None:
        label = "Edit category" if category is not None else "Create category"
        logger.debug("%s: opening editor", label)
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
            ui.label("Edit category" if category is not None else "Create category").classes(
                "text-xl"
            )
            name = ui.input("Name", value=category["name"] if category is not None else "").classes(
                "w-full"
            )
            definition = ui.textarea(
                "Definition", value=category["definition"] if category is not None else ""
            ).classes("w-full")
            kind = ui.select(
                {"spending": "Spending", "income": "Income", "transfer": "Internal transfer"},
                label="Financial type",
                value=category["kind"] if category is not None else "spending",
            )
            if category is not None:
                ui.label(
                    "Definition changes take effect for future categorization. Use "
                    "Recalculate categories for all history or filter by this "
                    "category to revisit affected transactions."
                )
            ui.button(
                "Save",
                on_click=lambda: perform(
                    lambda: save_category(
                        engine,
                        name.value or "",
                        definition.value or "",
                        kind.value,
                        category_id=category["id"] if category is not None else None,
                        expected_revision=category["revision"] if category is not None else None,
                    ),
                    dialog,
                    label,
                ),
            )
            ui.button(
                "Cancel", on_click=lambda: close_dialog(dialog, f"Cancel {label.lower()}")
            ).props("flat")
        dialog.on("hide", dialog.delete)
        dialog.open()

    def category_removal(category: dict[str, Any], merging: bool) -> None:
        label = "Merge category" if merging else "Delete category"
        logger.debug("%s: opening destination selector", label)
        options = {c["id"]: c["name"] for c in categories(engine) if c["id"] != category["id"]}
        if merging:
            options.pop("uncategorized", None)
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
            ui.label(f"{'Merge' if merging else 'Delete'} {category['name']}").classes("text-xl")
            ui.label(
                "Current assignments and confirmed examples move to the "
                "destination. Previous assignments remain in history."
            )
            destination = ui.select(
                options, label="Destination", value=None if merging else "uncategorized"
            ).classes("w-full")
            ui.button(
                "Merge category" if merging else "Delete category",
                on_click=lambda: perform(
                    lambda: remove_category(
                        engine,
                        category["id"],
                        destination.value,
                        expected_revision=category["revision"],
                    ),
                    dialog,
                    label,
                ),
            )
            ui.button(
                "Cancel", on_click=lambda: close_dialog(dialog, f"Cancel {label.lower()}")
            ).props("flat")
        dialog.on("hide", dialog.delete)
        dialog.open()

    def transaction_editor(row: dict[str, Any]) -> None:
        logger.debug("Transaction details: opening editor and assignment history")
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
            ui.label("Transaction details").classes("text-xl")
            ui.label(row["description"]).classes("whitespace-pre-wrap")
            ui.label(
                f"Booking {row['booking_date']} · Value {row['value_date']} · "
                f"{money(row['amount_cents'])} {row['currency']}"
            )
            generated = " (AI-generated)" if row["origin_source"] == "ai" else ""
            protection = (
                "protected manual decision"
                if row["protected"]
                else "eligible for future AI recalculation"
            )
            ui.label(f"Assignment: {row['source']}{generated} · {protection}")
            selection = ui.select(
                {c["id"]: c["name"] for c in categories(engine)},
                label="Category",
                value=row["category_id"],
            ).classes("w-full")
            ui.label(
                "Saving confirms only this transaction and replaces its previous confirmed example."
            )
            with ui.row():
                ui.button(
                    "Confirm individual category",
                    on_click=lambda: perform(
                        lambda: correct_transaction(
                            engine, row["id"], selection.value, expected_revision=row["revision"]
                        ),
                        dialog,
                        "Confirm individual category",
                    ),
                )
                ui.button(
                    "Undo latest assignment change",
                    on_click=lambda: perform(
                        lambda: undo_assignment(
                            engine, row["id"], expected_revision=row["revision"]
                        ),
                        dialog,
                        "Undo latest assignment change",
                    ),
                ).props("outline")
                ui.button(
                    "Close", on_click=lambda: close_dialog(dialog, "Close transaction details")
                ).props("flat")
            ui.label("Assignment history").classes("text-lg")
            history = assignment_history(engine, row["id"])
            ui.table(
                columns=columns(
                    "created_at", "category", "source", "protected", "active", "reason"
                ),
                rows=history,
                row_key="id",
                pagination=10,
            ).classes("w-full")
        dialog.on("hide", dialog.delete)
        dialog.open()

    selected_tab = {"value": "Analysis"}

    def select_tab(event: events.ValueChangeEventArguments[Any]) -> None:
        logger.debug("Select tab: %s", event.value)
        selected_tab["value"] = event.value

    @ui.refreshable
    def content() -> None:
        try:
            f = filters()
            rows = transaction_rows(engine, f)
            monthly = aggregate(engine, filters=f)
            summaries = aggregate(engine, by="merchant", filters=f)
        except ValueError as exc:
            logger.exception("Refresh results failed: invalid filters (%s)", type(exc).__name__)
            ui.label(str(exc)).classes("text-red-700")
            return
        logger.debug("Refresh results: displaying %d matching transactions", len(rows))
        ui.label(f"{len(rows):,} matching transactions").classes("text-xl")
        ui.label(
            "Spending is positive; income, internal transfers and "
            "Uncategorized are separate. Transfers and Uncategorized show "
            "signed net values. Currencies are separate series."
        )
        with ui.tabs(on_change=select_tab).classes("w-full") as tabs:
            dashboard = ui.tab("Analysis")
            transactions = ui.tab("Transactions")
            category_tab = ui.tab("Categories")
            imports = ui.tab("Import history")
        with ui.tab_panels(tabs, value=selected_tab["value"]).classes("w-full"):
            with ui.tab_panel(dashboard):
                ui.plotly(monthly_figure(monthly)).classes("w-full")
                ui.plotly(trend_figure(monthly)).classes("w-full")
                ui.label("Merchant summaries").classes("text-xl")
                display = [
                    dict(
                        r,
                        outgoing=money(r["outgoing_cents"]),
                        incoming=money(r["incoming_cents"]),
                        net=money(r["net_cents"]),
                        key=i,
                    )
                    for i, r in enumerate(summaries)
                ]
                ui.table(
                    columns=columns(
                        "merchant",
                        "category_name",
                        "bucket",
                        "currency",
                        "transaction_count",
                        "outgoing",
                        "incoming",
                        "net",
                    ),
                    rows=display,
                    row_key="key",
                    pagination=20,
                ).classes("w-full")
            with ui.tab_panel(transactions):
                ui.label("Click a row to correct that transaction or view its history.")
                display = [
                    dict(
                        r,
                        booking_date=r["booking_date"].isoformat(),
                        value_date=r["value_date"].isoformat(),
                        amount=money(r["amount_cents"]),
                        source_label="AI-generated" if r["origin_source"] == "ai" else r["source"],
                    )
                    for r in rows
                ]
                table = ui.table(
                    columns=columns(
                        "booking_date",
                        "value_date",
                        "description",
                        "amount",
                        "currency",
                        "category",
                        "source_label",
                        "protected",
                    ),
                    rows=display,
                    row_key="id",
                    pagination=25,
                ).classes("w-full")
                table.on("rowClick", lambda e: transaction_editor(e.args[1]), [[], None, None])
            with ui.tab_panel(category_tab):
                ui.button("Create category", on_click=lambda: category_editor())
                for c in categories(engine):
                    with ui.card().classes("w-full"):
                        ui.label(f"{c['name']} · {c['kind']}").classes("text-lg")
                        ui.label(c["definition"]).classes("whitespace-pre-wrap")
                        if c["id"] != "uncategorized":
                            with ui.row():
                                ui.button("Edit", on_click=lambda c=c: category_editor(c)).props(
                                    "flat"
                                )
                                ui.button(
                                    "Merge", on_click=lambda c=c: category_removal(c, True)
                                ).props("flat")
                                ui.button(
                                    "Delete", on_click=lambda c=c: category_removal(c, False)
                                ).props("flat")
            with ui.tab_panel(imports):
                with Session(engine) as session:
                    records = [
                        {
                            "id": r.id,
                            "filename": r.filename,
                            "created_at": r.created_at.isoformat(),
                            "row_count": r.row_count,
                            "inserted_count": r.inserted_count,
                            "duplicate_count": r.duplicate_count,
                        }
                        for r in session.scalars(select(Import).order_by(Import.created_at.desc()))
                    ]
                ui.table(
                    columns=columns(
                        "filename", "created_at", "row_count", "inserted_count", "duplicate_count"
                    ),
                    rows=records,
                    row_key="id",
                    pagination=20,
                ).classes("w-full")

    build_ai_panel(engine, ai_client, auth, filters, refresh)
    content()
    refresh()


def serve(engine: Engine, port: int = 8080, log_level: str = "info") -> None:
    from .ai import ResponsesClient
    from .auth import ChatGPTSession

    auth = ChatGPTSession()
    ai_client = ResponsesClient(auth)

    @ui.page("/")
    def index() -> None:
        with ui.column().classes("w-full max-w-screen-2xl mx-auto p-6"):
            build_page(engine, ai_client, auth)

    logger.info("Starting fyntok at http://127.0.0.1:%d", port)
    try:
        ui.run(
            host="127.0.0.1",
            port=port,
            title="fyntok",
            reload=False,
            show=False,
            uvicorn_logging_level=log_level,
            access_log=True,
        )
    except Exception:
        logger.exception("fyntok server failed")
        raise
    finally:
        logger.info("fyntok server stopped")
