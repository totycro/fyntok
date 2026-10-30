"""A compact, unfiltered snapshot of one booking month."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from nicegui import events, ui
from sqlalchemy.engine import Engine

from .accounts import repayment_warning
from .analysis_plot import AnalysisPlot
from .charts import breakdown_figure
from .insights import (
    Period,
    build_report,
    currency_options,
    month_period,
    months_in,
    observed_history,
)
from .queries import Filters, transaction_rows


@dataclass
class OverviewState:
    currency: str | None = None
    month: str | None = None


def build_overview(
    engine: Engine,
    state: OverviewState,
    open_transaction: Callable[[dict[str, Any]], None],
    money: Callable[[int], str],
    columns: Callable[..., list[dict[str, Any]]],
) -> None:
    def update(attribute: str, value: str | None) -> None:
        setattr(state, attribute, value)
        overview.refresh()

    @ui.refreshable
    def overview() -> None:
        currencies = currency_options(engine, Filters())
        if not currencies:
            ui.label("No transactions yet. Import a bank export to see your overview.")
            return
        chosen = (
            state.currency
            if state.currency is not None and state.currency in currencies
            else currencies[0]
        )
        state.currency = chosen
        history = observed_history(engine, chosen)
        first = min(span[0] for span in history.values())
        last = max(span[1] for span in history.values())
        months = months_in(Period(first, last))
        month = state.month if state.month in months else months[-1]
        state.month = month
        period = month_period(date.fromisoformat(f"{month}-01"))
        with ui.row().classes("w-full items-end gap-4"):
            ui.select(
                list(reversed(months)),
                value=state.month,
                label="Month",
                on_change=lambda e: update("month", e.value),
            ).classes("w-48").mark("overview-month")
            ui.select(
                currencies,
                value=state.currency,
                label="Currency",
                on_change=lambda e: update("currency", e.value),
            ).classes("w-40").mark("overview-currency")
        ui.label(f"{period.start:%B %Y} · {state.currency}").classes("text-xl font-semibold")
        report = build_report(engine, Filters(currency=state.currency), period)
        if warning := repayment_warning(transaction_rows(engine, report.filters)):
            ui.label(warning).classes("text-sm text-amber-800")
        if state.month not in history:
            ui.label("No recorded activity this month. Import coverage is unknown.")
        elif period.start < first or period.end > min(last, date.today()):
            ui.label(
                "Partial month: recorded history does not span the full month. "
                "Transaction dates do not confirm complete import coverage."
            ).classes("text-sm text-amber-800")
        with ui.row().classes("w-full gap-4"):
            for title, field in (("Spending", "spending_cents"), ("Income", "income_cents")):
                with ui.card().classes("flex-1 min-w-48"):
                    ui.label(title).classes("text-gray-600")
                    ui.label(f"{money(report.totals[field])} {state.currency}").classes(
                        "text-2xl font-semibold"
                    ).mark(f"overview-{field}")
        ui.label(
            f"Uncategorized · outgoing "
            f"{money(report.totals['uncategorized_outgoing_cents'])} {state.currency} · "
            f"incoming {money(report.totals['uncategorized_incoming_cents'])} {state.currency}"
        ).classes("text-sm text-gray-600")
        ui.label("Where the money went").classes("text-xl font-semibold")

        def drill(category_id: str) -> None:
            category = next((row for row in report.categories if row["key"] == category_id), None)
            if category is None:
                return
            rows = [
                dict(
                    row,
                    booking_date=row["booking_date"].isoformat(),
                    value_date=row["value_date"].isoformat(),
                )
                for row in transaction_rows(
                    engine,
                    Filters(
                        start=period.start,
                        end=period.end,
                        currency=chosen,
                        category_id=category_id,
                    ),
                )
                if row["amount_cents"] < 0 and not row["repayment_excluded"]
            ]
            with ui.dialog() as dialog, ui.card().classes("w-full max-w-5xl"):
                ui.label("Transactions behind this report").classes("text-xl font-semibold")
                ui.label(f"Category: {category['name']}")
                ui.label(
                    f"{period.start:%B %Y} · {chosen} · "
                    f"{period.start} through {period.end} · {len(rows):,} transactions"
                )
                transactions = (
                    ui.table(
                        columns=columns(
                            "booking_date",
                            "description",
                            "category",
                            "amount",
                            "currency",
                            money_columns=("amount",),
                        ),
                        rows=rows,
                        row_key="id",
                        pagination=15,
                    )
                    .classes("w-full")
                    .mark("overview-transactions")
                )
                transactions.on("rowClick", lambda e: open_transaction(e.args[1]), [[], None, None])
                ui.button("Back to overview", on_click=dialog.close).props("flat")
            dialog.on("hide", dialog.delete)
            dialog.open()

        def chart_clicked(event: events.GenericEventArguments) -> None:
            points = event.args.get("points", [])
            if points:
                metadata = points[0].get("customdata")
                if isinstance(metadata, dict) and isinstance(metadata.get("category_id"), str):
                    drill(metadata["category_id"])

        if not report.categories:
            ui.label("No outgoing spending this month.")
        else:
            plot = AnalysisPlot(
                breakdown_figure(report.categories, chosen, show_amounts=True)
            ).classes("w-full")
            plot.mark("overview-category-chart")
            plot.on("plotly_click", chart_clicked, args=["points"])
            ui.label("Bars show the top 20 categories. Click a bar to inspect spending.").classes(
                "text-sm text-gray-600"
            )
        ui.label(
            "Spending includes all outgoing money, including uncategorized transactions. "
            "Uncategorized amounts are also shown separately for reference. "
            "Internal transfers excluded. "
            "Categorized credits, including refunds, count as income."
        ).classes("text-sm text-gray-600")

    overview()
