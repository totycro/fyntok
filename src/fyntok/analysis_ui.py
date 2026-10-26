"""Interactive financial analysis; drill-down dialogs preserve the report's controls."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import plotly.graph_objects as go
from nicegui import events, ui
from sqlalchemy.engine import Engine

from .analysis_plot import AnalysisPlot
from .charts import breakdown_figure, cashflow_figure, spending_heatmap
from .date_ui import iso_date_input
from .insights import (
    Period,
    Report,
    build_report,
    currency_options,
    month_period,
    month_start,
    months_in,
    observed_history,
)
from .queries import Filters, transaction_rows


@dataclass
class AnalysisState:
    view: str = "Monthly overview"
    currency: str | None = None
    month: str | None = None
    history_period: str = "All history"
    custom_start: str = ""
    custom_end: str = ""
    comparison: str = "Previous period"
    grouping: str = "Monthly"
    breakdown: str = "Categories"
    rolling: bool = False
    selected_categories: list[str] = field(default_factory=list)


def build_analysis(
    engine: Engine,
    filters: Filters,
    state: AnalysisState,
    open_transaction: Callable[[dict[str, Any]], None],
    money: Callable[[int], str],
    columns: Callable[..., list[dict[str, Any]]],
) -> None:
    # Quasar's panel wrappers otherwise become scroll containers and prevent the
    # report context from sticking to the viewport. Only relax the Analysis panel.
    ui.add_css("""
        .report-panels:has(.analysis-panel),
        .report-panels > .q-panel:has(> .analysis-panel) {
            overflow: visible;
        }
        .analysis-context {
            position: sticky;
            top: 0;
            z-index: 10;
            background: #eff6ff;
            border-left: 4px solid #2563eb;
            border-bottom: 1px solid #bfdbfe;
            box-shadow: 0 2px 6px rgb(30 64 175 / 12%);
        }
    """)

    def update(attribute: str, value: Any) -> None:
        setattr(state, attribute, value)
        dashboard.refresh()

    @ui.refreshable
    def dashboard() -> None:
        options = currency_options(engine, filters)
        chosen = filters.currency or (
            state.currency if state.currency in options else next(iter(options), None)
        )
        if chosen is not None and chosen not in options:
            options.append(chosen)
        state.currency = chosen
        with ui.row().classes("w-full items-center gap-4"):
            ui.toggle(
                ["Monthly overview", "Historical explorer"],
                value=state.view,
                on_change=lambda e: update("view", e.value),
            ).mark("analysis-view")
            ui.select(
                options,
                value=chosen,
                label="Analysis currency",
                on_change=lambda e: update("currency", e.value),
            ).classes("w-40").set_enabled(not bool(filters.currency))
        ui.label(
            "Reports reflect the active filters. Categorized credits count as income, "
            "including refunds. "
            "Spending includes all outgoing money, including uncategorized transactions "
            "and debits assigned to income categories. "
            "Uncategorized amounts are also shown separately for reference. "
            "Net cash flow includes uncategorized money; internal transfers are excluded."
        ).classes("text-sm text-gray-600")
        if chosen is None:
            ui.label("No matching history. Import transactions or adjust the filters.")
            ui.table(
                columns=columns("name", "spending", money_columns=("spending",)),
                rows=[],
                row_key="key",
            )
            return
        history = observed_history(engine, chosen)
        if not history:
            ui.label("No matching history. Import transactions or adjust the filters.")
            return
        first = min(span[0] for span in history.values())
        last = max(span[1] for span in history.values())
        ui.label(
            f"Observed history · {chosen} · {first.isoformat()} through {last.isoformat()}"
        ).classes("text-sm")
        ui.label(
            "Transaction dates do not establish complete import coverage. "
            "Gaps mean no recorded activity, "
            "not confirmed zero spending. Typical-month statistics use full interior months "
            "with activity; "
            "partial boundary months are excluded."
        ).classes("text-sm text-gray-600")
        lower, upper = max(first, filters.start or first), min(last, filters.end or last)
        if lower > upper:
            ui.label("No history in the selected date range.")
            return
        monthly = state.view == "Monthly overview"
        with ui.row().classes("w-full items-end gap-4"):
            if monthly:
                available = months_in(Period(lower, upper))
                if state.month not in available:
                    current_month = month_start(date.today()).isoformat()[:7]
                    completed = [month for month in available if month < current_month]
                    state.month = completed[-1] if completed else available[-1]
                ui.select(
                    list(reversed(available)),
                    value=state.month,
                    label="Overview month",
                    on_change=lambda e: update("month", e.value),
                ).classes("w-48")
                selected = month_period(date.fromisoformat(f"{state.month}-01"))
            else:
                presets = [
                    "All history",
                    "Last 12 months",
                    *sorted({m[:4] for m in history}, reverse=True),
                    "Custom dates",
                ]
                if state.history_period not in presets:
                    state.history_period = "All history"
                ui.select(
                    presets,
                    value=state.history_period,
                    label="History period",
                    on_change=lambda e: update("history_period", e.value),
                ).classes("w-48")
                if state.history_period == "Custom dates":
                    if not state.custom_start:
                        state.custom_start = lower.isoformat()
                    if not state.custom_end:
                        state.custom_end = upper.isoformat()
                    iso_date_input(
                        "Analysis from",
                        value=state.custom_start,
                        on_change=lambda e: update("custom_start", e.value),
                    )
                    iso_date_input(
                        "Analysis through",
                        value=state.custom_end,
                        on_change=lambda e: update("custom_end", e.value),
                    )
                    try:
                        selected = Period(
                            date.fromisoformat(state.custom_start),
                            date.fromisoformat(state.custom_end),
                        )
                    except ValueError:
                        ui.label("Enter a valid analysis date range.").classes("text-red-700")
                        return
                elif state.history_period.isdigit():
                    year = int(state.history_period)
                    selected = Period(date(year, 1, 1), date(year, 12, 31))
                elif state.history_period == "Last 12 months":
                    selected = Period(month_start(upper, -11), month_period(upper).end)
                else:
                    selected = Period(month_start(lower), month_period(upper).end)
                ui.select(
                    ["Monthly", "Yearly"],
                    value=state.grouping,
                    label="Trend grouping",
                    on_change=lambda e: update("grouping", e.value),
                ).classes("w-40")
            if not monthly:
                ui.select(
                    ["Previous period", "Same period last year"],
                    value=state.comparison,
                    label="Compare with",
                    on_change=lambda e: update("comparison", e.value),
                ).classes("w-56")
        # Global date filters constrain the period, but observed boundary dates do not
        # truncate it: an incomplete month must not look like a complete short period.
        selected_start = max(selected.start, filters.start or selected.start)
        selected_end = min(selected.end, filters.end or selected.end)
        if selected_start > selected_end:
            ui.label("This period does not overlap the active date filters.")
            return
        selected = Period(selected_start, selected_end)
        report = build_report(
            engine,
            replace(filters, currency=chosen),
            selected,
            year_ago=state.comparison == "Same period last year",
        )
        if not report.totals["outgoing_cents"] and not report.totals["incoming_cents"]:
            ui.label("No financial activity matches these filters in this period.")
        if selected == Period(date(selected.start.year, 1, 1), date(selected.start.year, 12, 31)):
            period_label = str(selected.start.year)
        elif selected == month_period(selected.start):
            period_label = selected.start.strftime("%B %Y")
        else:
            period_label = f"{selected.start} through {selected.end}"
        with (
            ui.row()
            .classes("analysis-context w-full items-center gap-x-4 gap-y-1 px-3 py-3")
            .props('role="group" aria-label="Selected analysis context"')
            .mark("analysis-context")
        ):
            (
                ui.label(period_label)
                .classes("text-lg font-semibold text-blue-900")
                .style("text-wrap: balance")
                .props(f'title="Selected period: {selected.start} through {selected.end}"')
            )
            ui.label(f"{state.view} · {chosen}").classes("text-sm text-gray-700")
        partial = selected.start < first or selected.end > last or selected.end > date.today()
        if partial or selected.start.day != 1 or selected.end != month_period(selected.end).end:
            ui.label(
                "Partial period: recorded history or date filters do not span "
                "full calendar months."
                + (" Comparison totals are not adjusted for missing days." if not monthly else "")
            ).classes("text-amber-800 text-sm")
        missing = [row["month"] for row in report.series if not row["observed"]]
        if missing:
            ui.label(
                f"No recorded activity: {', '.join(missing)}. "
                "Monthly charts show gaps; yearly totals sum recorded activity."
            ).classes("text-amber-800 text-sm")

        def drill(
            metadata: dict[str, Any] | None = None,
            *,
            spending_only: bool = False,
            within_period: bool = True,
        ) -> None:
            metadata = metadata or {}
            start = max(
                selected.start if within_period else filters.start or date.min,
                date.fromisoformat(metadata.get("start", selected.start.isoformat())),
            )
            end = min(
                selected.end if within_period else filters.end or date.max,
                date.fromisoformat(metadata.get("end", selected.end.isoformat())),
            )
            if metadata.get("month"):
                end = min(
                    selected.end, month_period(date.fromisoformat(f"{metadata['month']}-01")).end
                )
            if start > end:
                return
            selection = replace(
                report.filters,
                start=start,
                end=end,
                category_id=metadata.get("category_id", report.filters.category_id),
                merchant=metadata.get("merchant", report.filters.merchant),
            )
            # Determine transfer/income behavior using the same active category convention
            # as SQL aggregation. Drill-down must reconcile to the displayed spending.
            from .manual import categories

            kinds = {row["id"]: row["kind"] for row in categories(engine)}
            rows = [
                row
                for row in transaction_rows(engine, selection)
                if kinds.get(row["category_id"], "uncategorized") != "transfer"
            ]
            if spending_only or "category_id" in metadata or "merchant" in metadata:
                rows = [row for row in rows if row["amount_cents"] < 0]
            bucket = metadata.get("bucket")
            if bucket in {"uncategorized_outgoing", "uncategorized_incoming"}:
                rows = [
                    row
                    for row in rows
                    if kinds.get(row["category_id"], "uncategorized") == "uncategorized"
                    and (
                        row["amount_cents"] < 0
                        if bucket == "uncategorized_outgoing"
                        else row["amount_cents"] > 0
                    )
                ]
            with ui.dialog() as dialog, ui.card().classes("w-full max-w-5xl"):
                ui.label("Transactions behind this report").classes("text-xl font-semibold")
                ui.label(f"{chosen} · {start} through {end} · {len(rows):,} transactions")
                if selection.category_id is not None:
                    category_name = next(
                        (c["name"] for c in categories(engine) if c["id"] == selection.category_id),
                        selection.category_id,
                    )
                    ui.label(f"Category: {category_name}")
                if selection.merchant is not None:
                    ui.label(f"Merchant: {selection.merchant}")
                if "category_id" in metadata or "merchant" in metadata:
                    detail_start = max(
                        month_start(start, -11), month_start(first), filters.start or date.min
                    )
                    detail_report = build_report(engine, selection, Period(detail_start, end))
                    detail_series = detail_report.series
                    detail_average = detail_report.average_cents
                    ui.label(f"Spending trend · {detail_start} through {end}").classes("text-sm")
                    if detail_average is not None:
                        ui.label(
                            "Average monthly spending · "
                            f"{money(detail_average)} {chosen} · "
                            f"{len(detail_report.eligible_months)} eligible months"
                        ).classes("font-medium")
                    else:
                        ui.label(
                            "Insufficient history for average monthly spending: "
                            "no full interior months with recorded activity."
                        ).classes("text-sm text-gray-600")
                    AnalysisPlot(
                        cashflow_figure(
                            detail_series,
                            chosen,
                            spending_only=True,
                            average_cents=detail_average,
                        )
                    ).classes("w-full")
                table = (
                    ui.table(
                        columns=columns(
                            "booking_date",
                            "description",
                            "category",
                            "amount",
                            "currency",
                            money_columns=("amount",),
                        ),
                        rows=[
                            dict(
                                row,
                                booking_date=row["booking_date"].isoformat(),
                                value_date=row["value_date"].isoformat(),
                            )
                            for row in rows
                        ],
                        row_key="id",
                        pagination=15,
                    )
                    .classes("w-full")
                    .mark("analysis-transactions")
                )
                table.on("rowClick", lambda e: open_transaction(e.args[1]), [[], None, None])
                ui.button("Back to analysis", on_click=dialog.close).props("flat")
            dialog.on("hide", dialog.delete)
            dialog.open()

        def chart(
            figure: go.Figure, *, spending_only: bool = False, limit_to_selected: bool = True
        ) -> None:
            plot = AnalysisPlot(figure).classes("w-full")

            def clicked(event: events.GenericEventArguments) -> None:
                points = event.args.get("points", [])
                if points:
                    data = points[0].get("customdata")
                    if isinstance(data, dict):
                        # Overview trend periods can lie outside the selected month.
                        if not limit_to_selected and data.get("start"):
                            drill(data, spending_only=spending_only, within_period=False)
                        else:
                            drill(data, spending_only=spending_only)

            plot.on("plotly_click", clicked, args=["points"])

        with ui.row().classes("w-full gap-4"):
            for title, field_name in (
                ("Spending", "spending_cents"),
                ("Income", "income_cents"),
                ("Net cash flow", "net_cents"),
            ):
                with ui.card().classes("flex-1 min-w-48"):
                    ui.label(title).classes("text-gray-600")
                    ui.label(f"{money(report.totals[field_name])} {chosen}").classes(
                        "text-2xl font-semibold"
                    ).mark(f"analysis-{field_name}")
        ui.label(
            f"Uncategorized · outgoing "
            f"{money(report.totals['uncategorized_outgoing_cents'])} {chosen} · "
            f"incoming {money(report.totals['uncategorized_incoming_cents'])} {chosen}"
        ).classes("text-sm")
        ui.label(
            f"All non-transfer money · outgoing "
            f"{money(report.totals['outgoing_cents'])} {chosen} · "
            f"incoming {money(report.totals['incoming_cents'])} {chosen}"
        ).classes("text-sm")
        ui.button("View report transactions", on_click=lambda: drill()).props("outline").mark(
            "analysis-drill"
        )
        if not monthly:
            render_changes(report, chosen, money, columns, drill, state.breakdown == "Merchants")

        if monthly:
            trend_period = Period(
                max(
                    month_start(selected.start, -11), month_start(first), filters.start or date.min
                ),
                selected.end,
            )
            trend_report = build_report(engine, replace(filters, currency=chosen), trend_period)
        else:
            trend_report = report
        typical = trend_report
        if typical.average_cents is not None and typical.median_cents is not None:
            ui.label(
                f"Typical month · average {money(typical.average_cents)} {chosen} · "
                f"median {money(typical.median_cents)} {chosen} · "
                f"{len(typical.eligible_months)} eligible months"
            ).classes("text-lg font-medium")
            ui.label(
                f"Statistics window: {trend_report.period.start} "
                f"through {trend_report.period.end}. "
                "Averages are rounded to the nearest cent."
            ).classes("text-sm text-gray-600")
        else:
            ui.label(
                "Insufficient history for typical-month statistics: "
                "no full interior months with recorded activity."
            )
        with ui.row().classes("items-end w-full"):
            ui.checkbox(
                "Show 3-month spending average",
                value=state.rolling,
                on_change=lambda e: update("rolling", e.value),
            ).set_enabled(monthly or state.grouping == "Monthly")
            category_options = {row["key"]: row["name"] for row in trend_report.categories}
            state.selected_categories = [
                key for key in state.selected_categories if key in category_options
            ]
            ui.select(
                category_options,
                value=state.selected_categories,
                multiple=True,
                label="Category trends (empty = totals)",
                on_change=lambda e: update("selected_categories", e.value or []),
            ).props("use-chips clearable").classes("min-w-80")
        chart(
            cashflow_figure(
                trend_report.series,
                chosen,
                yearly=not monthly and state.grouping == "Yearly",
                rolling=state.rolling,
                categories=[
                    row
                    for row in trend_report.categories
                    if row["key"] in state.selected_categories
                ],
            ),
            limit_to_selected=not monthly,
        )
        with ui.row().classes("items-center"):
            ui.label("Where the money went").classes("text-xl font-semibold")
            ui.toggle(
                ["Categories", "Merchants"],
                value=state.breakdown,
                on_change=lambda e: update("breakdown", e.value),
            ).mark("analysis-breakdown")
        groups = report.merchants if state.breakdown == "Merchants" else report.categories
        if monthly and state.breakdown == "Categories":
            typical_categories = {row["key"]: row for row in trend_report.categories}
            groups = [
                dict(
                    row,
                    average_cents=typical_categories.get(row["key"], {}).get("average_cents"),
                    median_cents=typical_categories.get(row["key"], {}).get("median_cents"),
                )
                for row in groups
            ]
        chart(
            breakdown_figure(groups, chosen, merchant=state.breakdown == "Merchants"),
            spending_only=True,
        )
        ui.label(
            "Bars show the top 20; the table includes all groups. "
            "Click a bar or table row to inspect spending."
        ).classes("text-sm text-gray-600")
        if state.breakdown == "Categories":
            ui.label("Category averages and medians use the statistics window above.").classes(
                "text-sm text-gray-600"
            )
        table = (
            ui.table(
                columns=[
                    *columns("name", "spending", money_columns=("spending",)),
                    {
                        "name": "share",
                        "label": "Share of spending (%)",
                        "field": "share",
                        "sortable": True,
                        ":format": "v => v.toFixed(1)",
                    },
                    *(
                        []
                        if state.breakdown == "Merchants"
                        else columns("average", "median", money_columns=("average", "median"))
                    ),
                ],
                rows=groups,
                row_key="key",
                pagination=15,
            )
            .classes("w-full")
            .mark("analysis-breakdown-table")
        )
        table.on(
            "rowClick",
            lambda e: drill(
                {"merchant" if state.breakdown == "Merchants" else "category_id": e.args[1]["key"]},
                spending_only=True,
            ),
            [[], None, None],
        )
        if not monthly:
            chart(
                spending_heatmap(report.categories, months_in(selected), chosen), spending_only=True
            )
            month_columns = [
                {
                    "name": month,
                    "label": month,
                    "field": month,
                    "sortable": True,
                    ":format": (
                        "v => v === null ? '—' : (v / 100).toLocaleString('en-US', "
                        "{minimumFractionDigits: 2, maximumFractionDigits: 2})"
                    ),
                }
                for month in months_in(selected)
            ]
            ui.table(
                columns=[*columns("name"), *month_columns],
                rows=[dict(row, **row["monthly_cents"]) for row in report.categories],
                row_key="key",
                pagination=15,
            ).classes("w-full").mark("analysis-month-table")
        else:
            ui.label("Above recent monthly spending").classes("text-xl font-semibold")
            if len(report.baseline_months) < 3:
                ui.label(
                    "At least three full observed months in the preceding six months "
                    "are needed for a baseline."
                )
            else:
                ui.label(
                    f"Baseline: {', '.join(report.baseline_months)}. "
                    "Ranked by extra spending, not a prediction or fraud alert."
                ).classes("text-sm text-gray-600")
                baseline = ui.table(
                    columns=columns(
                        "name",
                        "spending",
                        "baseline",
                        "above",
                        money_columns=("spending", "baseline", "above"),
                    ),
                    rows=report.above_baseline[:10],
                    row_key="key",
                    pagination=10,
                ).classes("w-full")
                baseline.on(
                    "rowClick",
                    lambda e: drill({"category_id": e.args[1]["key"]}, spending_only=True),
                    [[], None, None],
                )
            ui.label("Largest expenses").classes("text-xl font-semibold")
            from .manual import categories

            kinds = {row["id"]: row["kind"] for row in categories(engine)}
            expenses = sorted(
                [
                    row
                    for row in transaction_rows(engine, report.filters)
                    if row["amount_cents"] < 0
                    and kinds.get(row["category_id"], "uncategorized") != "transfer"
                ],
                key=lambda row: row["amount_cents"],
            )[:10]
            expense_table = ui.table(
                columns=columns(
                    "booking_date", "description", "category", "amount", money_columns=("amount",)
                ),
                rows=[
                    dict(
                        row,
                        booking_date=row["booking_date"].isoformat(),
                        value_date=row["value_date"].isoformat(),
                    )
                    for row in expenses
                ],
                row_key="id",
                pagination=10,
            ).classes("w-full")
            expense_table.on("rowClick", lambda e: open_transaction(e.args[1]), [[], None, None])

    dashboard()


def render_changes(
    report: Report,
    currency: str,
    money: Callable[[int], str],
    columns: Callable[..., list[dict[str, Any]]],
    drill: Callable[..., None],
    merchant: bool,
) -> None:
    ui.label("What changed?").classes("text-xl font-semibold")
    ui.label(
        f"Compared with {report.comparison.start} through {report.comparison.end}; "
        "the same non-date filters apply."
    ).classes("text-sm text-gray-600")
    if not report.comparison_observed:
        ui.label("No comparison data in this period.")
        return
    before = report.previous["spending_cents"]
    delta = report.totals["spending_cents"] - before
    percent = f"{100 * delta / before:+.1f}%" if before else "No prior spending"
    ui.label(f"Spending change: {money(delta)} {currency} · {percent}").classes("text-lg")
    missing = [month for month in months_in(report.comparison) if month not in report.history]
    first = min(span[0] for span in report.history.values())
    last = max(span[1] for span in report.history.values())
    if (
        missing
        or report.comparison.start < first
        or report.comparison.end > min(last, date.today())
    ):
        ui.label(
            "Comparison history has gaps or partial boundaries; "
            "totals may not cover equivalent activity."
        ).classes("text-amber-800 text-sm")
    rows = report.merchant_changes if merchant else report.category_changes
    nonzero = [row for row in rows if row["change_cents"]]
    if not nonzero:
        ui.label("No spending change across these groups.")
        return
    leader = nonzero[0]
    ui.label(f"Largest contributor: {leader['name']} ({money(leader['change_cents'])} {currency}).")
    table = (
        ui.table(
            columns=[
                *columns(
                    "name",
                    "spending",
                    "previous",
                    "change",
                    money_columns=("spending", "previous", "change"),
                ),
                {
                    "name": "change_percent",
                    "label": "Change (%)",
                    "field": "change_percent",
                    "sortable": True,
                    ":format": "v => v === null ? 'No prior spending' : v.toFixed(1) + '%'",
                },
            ],
            rows=rows,
            row_key="key",
            pagination=10,
        )
        .classes("w-full")
        .mark("analysis-changes")
    )
    table.on(
        "rowClick",
        lambda e: drill(
            {"merchant" if merchant else "category_id": e.args[1]["key"]}, spending_only=True
        ),
        [[], None, None],
    )
