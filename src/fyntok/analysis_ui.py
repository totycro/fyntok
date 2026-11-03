"""Dash financial reports and reconciled transaction drill-downs."""

from dataclasses import asdict, replace
from datetime import date
from typing import Any

from dash import ALL, Dash, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate
from sqlalchemy.engine import Engine

from .accounts import repayment_warning
from .charts import breakdown_figure, cashflow_figure, spending_heatmap
from .dash_ui import (
    MONEY_FORMAT,
    callback,
    columns,
    dropdown,
    grid,
    json_rows,
    money,
    transaction_grid,
)
from .date_ui import iso_date_input
from .insights import (
    Period,
    Report,
    build_report,
    currency_options,
    default_overview_month,
    month_period,
    month_start,
    months_in,
    observed_history,
    period_label,
)
from .manual import categories
from .queries import Filters, transaction_rows


def read_filters(data: dict[str, Any] | None) -> Filters:
    values = dict(data or {})
    for key in ("start", "end"):
        if values.get(key):
            values[key] = date.fromisoformat(values[key])
    filters = Filters(**values)
    filters.validate()
    return filters


def filter_data(filters: Filters) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, date) else value
        for key, value in asdict(filters).items()
    }


def report_layout(prefix: str, *, overview: bool = False) -> html.Section:
    controls: list[Any] = [
        dropdown(f"{prefix}-currency", "Currency", [], clearable=False),
        dropdown(f"{prefix}-month", "Month", [], clearable=False),
    ]
    if not overview:
        controls += [
            dropdown(
                "analysis-view",
                "View",
                ["Monthly overview", "Historical explorer"],
                "Monthly overview",
                clearable=False,
            ),
            dropdown(
                "analysis-period", "History period", ["All history"], "All history", clearable=False
            ),
            iso_date_input("analysis-start", "Analysis from"),
            iso_date_input("analysis-end", "Analysis through"),
            dropdown(
                "analysis-grouping",
                "Trend grouping",
                ["Monthly", "Yearly"],
                "Monthly",
                clearable=False,
            ),
            dropdown(
                "analysis-comparison",
                "Compare with",
                ["Previous period", "Same period last year"],
                "Previous period",
                clearable=False,
            ),
            dropdown(
                "analysis-breakdown",
                "Breakdown",
                ["Categories", "Merchants"],
                "Categories",
                clearable=False,
            ),
            dropdown("analysis-trends", "Category trends (empty = totals)", [], [], multi=True),
            dcc.Checklist(
                id="analysis-rolling",
                options=[{"label": "Show 3-month spending average", "value": "rolling"}],
                value=[],
            ),
        ]
    return html.Section(
        [
            html.Div(controls, className="controls"),
            dcc.Loading(html.Div(id=f"{prefix}-report")),
            dcc.Store(id=f"{prefix}-context"),
            html.Div(
                [
                    html.Button("View report transactions", id=f"{prefix}-all-transactions"),
                    html.Button("Close report transactions", id=f"{prefix}-close-drill"),
                ],
                className="actions",
            ),
            html.Div(id=f"{prefix}-drill"),
        ]
    )


def choose_report(
    engine: Engine,
    filters: Filters,
    currency: str,
    month: str | None,
    *,
    view: str = "Monthly overview",
    history_period: str = "All history",
    start: str | None = None,
    end: str | None = None,
    comparison: str = "Previous period",
) -> Report:
    history = observed_history(engine, currency)
    if not history:
        raise ValueError("No matching history. Import transactions or adjust the filters.")
    first = min(span[0] for span in history.values())
    last = max(span[1] for span in history.values())
    lower, upper = max(first, filters.start or first), min(last, filters.end or last)
    if lower > upper:
        raise ValueError("No history in the selected date range.")
    if view == "Monthly overview":
        selected = (
            month_period(date.fromisoformat(f"{month}-01")) if bool(month) else month_period(upper)
        )
    elif history_period == "Custom dates":
        if not bool(start) or not bool(end):
            raise ValueError("Enter a valid analysis date range.")
        selected = Period(date.fromisoformat(start), date.fromisoformat(end))
    elif history_period.isdigit():
        year = int(history_period)
        selected = Period(date(year, 1, 1), date(year, 12, 31))
    elif history_period == "Last 12 months":
        selected = Period(month_start(upper, -11), month_period(upper).end)
    else:
        selected = Period(month_start(lower), month_period(upper).end)
    selected = Period(
        max(selected.start, filters.start or selected.start),
        min(selected.end, filters.end or selected.end),
    )
    return build_report(
        engine,
        replace(filters, currency=currency),
        selected,
        year_ago=comparison == "Same period last year",
    )


def report_children(
    engine: Engine,
    prefix: str,
    report: Report,
    *,
    overview: bool = False,
    monthly: bool = True,
    merchant: bool = False,
    yearly: bool = False,
    rolling: bool = False,
    selected_categories: list[str] | None = None,
    base_filters: Filters | None = None,
) -> list[Any]:
    currency = report.filters.currency or ""
    first = min(span[0] for span in report.history.values())
    last = max(span[1] for span in report.history.values())
    children: list[Any] = [
        html.Div(
            f"{period_label(report.period)} · {currency}"
            + (
                "" if overview else f" · {'Monthly overview' if monthly else 'Historical explorer'}"
            ),
            className="context",
        ),
        html.Div(
            [
                html.Div(
                    [title, html.Strong(f"{money(report.totals[field])} {currency}")],
                    className="metric",
                    id=f"{prefix}-{field}",
                )
                for title, field in [
                    ("Spending", "spending_cents"),
                    ("Income", "income_cents"),
                    *([] if overview else [("Net cash flow", "net_cents")]),
                ]
            ],
            className="metrics",
        ),
        html.P(
            f"Uncategorized · outgoing {money(report.totals['uncategorized_outgoing_cents'])} "
            f"{currency} · incoming {money(report.totals['uncategorized_incoming_cents'])} "
            f"{currency}"
        ),
        html.P(
            "Spending includes all outgoing money, including uncategorized transactions. "
            "Categorized credits, including refunds, count as income. Internal transfers and "
            "excluded card repayments are excluded. Uncategorized amounts are also shown "
            "separately for reference.",
            className="muted",
        ),
    ]
    if warning := repayment_warning(transaction_rows(engine, report.filters)):
        children.append(html.P(warning, className="warning"))
    if (
        report.period.start < first
        or report.period.end > min(last, date.today())
        or report.period.start.day != 1
        or report.period.end != month_period(report.period.end).end
    ):
        children.append(
            html.P(
                "Partial period: recorded history does not span full calendar "
                "months. Transaction dates do not confirm complete import coverage.",
                className="warning",
            )
        )
    missing = [row["month"] for row in report.series if not row["observed"]]
    if missing:
        children.append(
            html.P(
                f"No recorded activity: {', '.join(missing)}. "
                "Import coverage is unknown; monthly charts show gaps.",
                className="warning",
            )
        )

    def chart(name: str, figure: Any) -> Any:
        return dcc.Graph(
            id={"type": f"{prefix}-chart", "index": name},
            figure=figure,
            responsive=True,
            config={"displaylogo": False},
        )

    def groups(name: str, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> Any:
        extra = []
        if name == "breakdown":
            extra = [
                {
                    "field": "share",
                    "headerName": "Share of spending (%)",
                    "type": "numericColumn",
                    "valueFormatter": {"function": "params.value.toFixed(1)"},
                }
            ]
        elif name == "changes":
            extra = [
                {
                    "field": "change_percent",
                    "headerName": "Change (%)",
                    "type": "numericColumn",
                    "valueFormatter": {
                        "function": "params.value == null ? 'No prior spending' : "
                        "params.value.toFixed(1) + '%'"
                    },
                }
            ]
        return grid(
            {"type": f"{prefix}-group", "index": name},
            rows,
            [*columns("name", *fields, money_columns=fields), *extra],
            key="key",
        )

    trend = report
    if monthly and not overview:
        trend = build_report(
            engine,
            replace(report.filters, start=None, end=None),
            Period(
                max(
                    month_start(report.period.start, -11),
                    month_start(first),
                    base_filters.start
                    if base_filters is not None and base_filters.start is not None
                    else date.min,
                ),
                report.period.end,
            ),
        )
    if not overview:
        children += [
            html.P(
                f"All non-transfer money · outgoing "
                f"{money(report.totals['outgoing_cents'])} {currency} · "
                f"incoming {money(report.totals['incoming_cents'])} {currency}"
            ),
            html.P(f"Observed history · {currency} · {first} through {last}"),
            html.P(
                "Typical-month statistics exclude partial boundary months and months "
                "without recorded activity. Averages round to the nearest cent.",
                className="muted",
            ),
        ]
        if trend.average_cents is not None and trend.median_cents is not None:
            children.append(
                html.P(
                    f"Typical month · average {money(trend.average_cents)} "
                    f"{currency} · median {money(trend.median_cents)} {currency} · "
                    f"{len(trend.eligible_months)} eligible months · "
                    f"{trend.period.start} through {trend.period.end}"
                )
            )
        else:
            children.append(
                html.P(
                    "Insufficient history for typical-month statistics: "
                    "no full interior months with recorded activity."
                )
            )
        children.append(
            chart(
                "trend",
                cashflow_figure(
                    trend.series,
                    currency,
                    yearly=yearly,
                    rolling=rolling,
                    categories=[
                        row for row in trend.categories if row["key"] in (selected_categories or [])
                    ],
                ),
            )
        )
    breakdown = report.merchants if merchant else report.categories
    if monthly and not merchant:
        typical = {row["key"]: row for row in trend.categories}
        breakdown = [
            dict(
                row,
                average_cents=typical.get(row["key"], {}).get("average_cents"),
                median_cents=typical.get(row["key"], {}).get("median_cents"),
            )
            for row in breakdown
        ]
    children += [
        html.H3("Where the money went"),
        chart(
            "breakdown",
            breakdown_figure(breakdown, currency, merchant=merchant, show_amounts=overview),
        ),
        html.P("Bars show the top 20. Click a bar or table row to inspect spending."),
    ]
    if not overview:
        children.append(groups("breakdown", breakdown, ("spending", "average", "median")))
        if not monthly:
            children += [
                html.H3("What changed?"),
                html.P(
                    f"Compared with {report.comparison.start} through "
                    f"{report.comparison.end}; the same non-date filters apply."
                ),
            ]
            if report.comparison_observed:
                delta = report.totals["spending_cents"] - report.previous["spending_cents"]
                before = report.previous["spending_cents"]
                percent = f"{100 * delta / before:+.1f}%" if before else "No prior spending"
                children.append(html.P(f"Spending change: {money(delta)} {currency} · {percent}"))
                comparison_missing = any(
                    month not in report.history for month in months_in(report.comparison)
                )
                if (
                    comparison_missing
                    or report.comparison.start < first
                    or report.comparison.end > min(last, date.today())
                ):
                    children.append(
                        html.P(
                            "Comparison history has gaps or partial boundaries; "
                            "totals may not cover equivalent activity.",
                            className="warning",
                        )
                    )
                changes = report.merchant_changes if merchant else report.category_changes
                nonzero = [row for row in changes if row["change_cents"]]
                if nonzero:
                    leader = nonzero[0]
                    children.append(
                        html.P(
                            f"Largest contributor: {leader['name']} "
                            f"({money(leader['change_cents'])} {currency})."
                        )
                    )
                else:
                    children.append(html.P("No spending change across these groups."))
                children.append(groups("changes", changes, ("spending", "previous", "change")))
            else:
                children.append(html.P("No comparison data in this period."))
            children.append(
                chart(
                    "heatmap",
                    spending_heatmap(report.categories, months_in(report.period), currency),
                )
            )
            children.append(
                grid(
                    "analysis-month-table",
                    [dict(row, **row["monthly_cents"]) for row in report.categories],
                    [
                        *columns("name"),
                        *[
                            {
                                "field": month,
                                "valueFormatter": {"function": MONEY_FORMAT},
                            }
                            for month in months_in(report.period)
                        ],
                    ],
                    key="key",
                )
            )
        else:
            children += [html.H3("Above recent monthly spending")]
            if len(report.baseline_months) < 3:
                children.append(
                    html.P(
                        "At least three full observed months in the preceding "
                        "six months are needed for a baseline."
                    )
                )
            else:
                children += [
                    html.P(
                        f"Baseline: {', '.join(report.baseline_months)}. "
                        "Ranked by extra spending, not a prediction or fraud alert."
                    ),
                    groups(
                        "baseline", report.above_baseline[:10], ("spending", "baseline", "above")
                    ),
                ]
            expenses = drill_rows(engine, filter_data(report.filters), {}, spending_only=True)
            children += [
                html.H3("Largest expenses"),
                transaction_grid(
                    "expenses", sorted(expenses, key=lambda row: row["amount_cents"])[:10]
                ),
            ]
    return children


def drill_rows(
    engine: Engine, data: dict[str, Any], metadata: dict[str, Any], *, spending_only: bool = False
) -> list[dict[str, Any]]:
    filters = read_filters(data)
    start = (
        max(date.fromisoformat(metadata["start"]), filters.start or date.min)
        if metadata.get("start")
        else filters.start
    )
    end = (
        min(date.fromisoformat(metadata["end"]), filters.end or date.max)
        if metadata.get("end")
        else filters.end
    )
    if metadata.get("month"):
        month = month_period(date.fromisoformat(f"{metadata['month']}-01"))
        start = max(month.start, start or date.min)
        end = min(month.end, end or date.max)
    if start is not None and end is not None and start > end:
        return []
    selection = replace(
        filters,
        start=start,
        end=end,
        category_id=metadata.get("category_id", filters.category_id),
        merchant=metadata.get("merchant", filters.merchant),
    )
    kinds = {row["id"]: row["kind"] for row in categories(engine)}
    rows = [
        row
        for row in transaction_rows(engine, selection)
        if kinds.get(row["category_id"], "uncategorized") != "transfer"
        and not row["repayment_excluded"]
    ]
    if (
        spending_only
        or "category_id" in metadata
        or "merchant" in metadata
        or metadata.get("bucket") == "spending"
    ):
        rows = [row for row in rows if row["amount_cents"] < 0]
    bucket = metadata.get("bucket")
    if bucket == "income":
        rows = [
            row for row in rows if row["amount_cents"] > 0 and row["category_id"] != "uncategorized"
        ]
    if bucket in {"uncategorized_outgoing", "uncategorized_incoming"}:
        rows = [
            row
            for row in rows
            if row["category_id"] == "uncategorized"
            and (
                row["amount_cents"] < 0
                if bucket == "uncategorized_outgoing"
                else row["amount_cents"] > 0
            )
        ]
    return rows


def register_report(app: Dash, engine: Engine, prefix: str, *, overview: bool = False) -> None:
    @callback(
        app,
        Output(f"{prefix}-currency", "options"),
        Output(f"{prefix}-currency", "value"),
        Output(f"{prefix}-month", "options"),
        Output(f"{prefix}-month", "value"),
        Input("revision", "data"),
        Input(f"{prefix}-currency", "value"),
        *([] if overview else [Input("analysis-filters", "data")]),
        State(f"{prefix}-month", "value"),
    )
    def options(_revision: str, currency: str | None, *settings: Any) -> tuple[Any, ...]:
        month = settings[-1]
        filters = Filters() if overview else read_filters(settings[0])
        if filters.category_id not in {row["id"] for row in categories(engine)}:
            filters = replace(filters, category_id=None)
        currencies = currency_options(engine, filters)
        currency = currency if currency in currencies else next(iter(currencies), None)
        if currency is None:
            return [], None, [], None
        history = observed_history(engine, currency)
        lower = max(min(span[0] for span in history.values()), filters.start or date.min)
        upper = min(max(span[1] for span in history.values()), filters.end or date.max)
        if lower > upper:
            return currencies, currency, [], None
        period = Period(lower, upper)
        months = months_in(period)
        default = months[-1] if overview else default_overview_month(period, date.today())
        return currencies, currency, list(reversed(months)), month if month in months else default

    if not overview:

        @callback(
            app,
            Output("analysis-period", "options"),
            Output("analysis-trends", "options"),
            Input("revision", "data"),
            Input("analysis-currency", "value"),
        )
        def analysis_options(_revision: str, currency: str | None) -> tuple[Any, ...]:
            history = observed_history(engine, currency) if bool(currency) else {}
            return (
                [
                    "All history",
                    "Last 12 months",
                    *sorted({m[:4] for m in history}, reverse=True),
                    "Custom dates",
                ],
                [{"label": row["name"], "value": row["id"]} for row in categories(engine)],
            )

    inputs = (
        []
        if overview
        else [
            Input("analysis-filters", "data"),
            *[
                Input(f"analysis-{name}", prop)
                for name, prop in [
                    ("view", "value"),
                    ("period", "value"),
                    ("start", "date"),
                    ("end", "date"),
                    ("comparison", "value"),
                    ("grouping", "value"),
                    ("breakdown", "value"),
                    ("trends", "value"),
                    ("rolling", "value"),
                ]
            ],
        ]
    )

    @callback(
        app,
        Output(f"{prefix}-report", "children"),
        Output(f"{prefix}-context", "data"),
        Input("revision", "data"),
        Input(f"{prefix}-currency", "value"),
        Input(f"{prefix}-month", "value"),
        *inputs,
    )
    def render(
        _revision: str, currency: str | None, month: str | None, *settings: Any
    ) -> tuple[Any, Any]:
        if not bool(currency):
            message = (
                "No transactions yet. Import a bank export to see your overview."
                if overview
                else "No matching history. Import transactions or adjust the filters."
            )
            return html.P(message), None
        values = settings or (
            {},
            "Monthly overview",
            "All history",
            None,
            None,
            "Previous period",
            "Monthly",
            "Categories",
            [],
            [],
        )
        (
            data,
            view,
            history_period,
            start,
            end,
            comparison,
            grouping,
            breakdown,
            trends,
            rolling,
        ) = values
        try:
            filters = read_filters(data)
            # Removed category filters must not leave a report permanently empty.
            if filters.category_id not in {row["id"] for row in categories(engine)}:
                filters = replace(filters, category_id=None)
            report = choose_report(
                engine,
                filters,
                currency,
                month,
                view=view,
                history_period=history_period,
                start=start,
                end=end,
                comparison=comparison,
            )
        except ValueError as exc:
            return html.P(str(exc), className="error"), None
        context = {
            "filters": filter_data(report.filters),
            "base": filter_data(filters),
            "merchant": breakdown == "Merchants",
            "monthly": view == "Monthly overview",
        }
        return report_children(
            engine,
            prefix,
            report,
            overview=overview,
            monthly=context["monthly"],
            merchant=context["merchant"],
            yearly=view != "Monthly overview" and grouping == "Yearly",
            rolling=bool(rolling),
            selected_categories=trends,
            base_filters=filters,
        ), context

    @callback(
        app,
        Output(f"{prefix}-drill", "children"),
        Input({"type": f"{prefix}-chart", "index": ALL}, "clickData"),
        Input({"type": f"{prefix}-group", "index": ALL}, "cellClicked"),
        Input(f"{prefix}-all-transactions", "n_clicks"),
        Input(f"{prefix}-close-drill", "n_clicks"),
        Input(f"{prefix}-context", "data"),
        State({"type": f"{prefix}-group", "index": ALL}, "rowData"),
        State({"type": f"{prefix}-group", "index": ALL}, "id"),
        prevent_initial_call=True,
    )
    def drill(
        clicks: list[Any],
        cells: list[Any],
        _all: int,
        _close: int,
        context: dict[str, Any] | None,
        rows: list[Any],
        identities: list[Any],
    ) -> Any:
        if context is None or ctx.triggered_id in (f"{prefix}-context", f"{prefix}-close-drill"):
            return []
        identity = ctx.triggered_id
        metadata: dict[str, Any] = {}
        spending_only = True
        if identity == f"{prefix}-all-transactions":
            identity = {"index": "all"}
            spending_only = False
        elif isinstance(identity, dict) and identity["type"] == f"{prefix}-chart":
            chart_ids = ctx.inputs_list[0]
            click = next(
                (clicks[i] for i, item in enumerate(chart_ids) if item["id"] == identity), None
            )
            if not click or not click.get("points"):
                raise PreventUpdate
            metadata = click["points"][0].get("customdata") or {}
            if not isinstance(metadata, dict):
                raise PreventUpdate
            spending_only = identity["index"] != "trend"
        elif isinstance(identity, dict):
            i = identities.index(identity)
            if not cells[i]:
                raise PreventUpdate
            row = next((row for row in rows[i] if row["key"] == cells[i]["rowId"]), None)
            if row is None:
                raise PreventUpdate
            metadata = {
                "merchant"
                if context["merchant"] and identity["index"] != "baseline"
                else "category_id": row["key"]
            }
        else:
            raise PreventUpdate
        data = dict(context["filters"])
        if identity["index"] == "trend" and context["monthly"]:
            data["start"], data["end"] = metadata.get("start"), metadata.get("end")
        base = read_filters(context["base"])
        if metadata.get("start") and base.start:
            metadata["start"] = max(metadata["start"], base.start.isoformat())
        if metadata.get("end") and base.end:
            metadata["end"] = min(metadata["end"], base.end.isoformat())
        selected = drill_rows(engine, data, metadata, spending_only=spending_only)
        children: list[Any] = [
            html.H3("Transactions behind this report"),
            html.P(
                f"{len(selected):,} transactions · "
                f"{data.get('currency')} · "
                f"{metadata.get('start') or data.get('start')} through "
                f"{metadata.get('end') or data.get('end')}"
            ),
        ]
        if "category_id" in metadata or "merchant" in metadata:
            category_name = next(
                (
                    row["name"]
                    for row in categories(engine)
                    if row["id"] == metadata.get("category_id")
                ),
                None,
            )
            children.append(
                html.P(
                    f"Category: {category_name}"
                    if category_name is not None
                    else f"Merchant: {metadata.get('merchant')}"
                )
            )
            selection = replace(
                read_filters(data),
                category_id=metadata.get("category_id", data.get("category_id")),
                merchant=metadata.get("merchant", data.get("merchant")),
            )
            end = selection.end or date.today()
            history = observed_history(engine, selection.currency or "")
            first = min(span[0] for span in history.values())
            start = max(
                month_start(selection.start or end, -11), month_start(first), base.start or date.min
            )
            trend = build_report(engine, selection, Period(start, end))
            average = (
                f"Average monthly spending · {money(trend.average_cents)} "
                f"{selection.currency} · {len(trend.eligible_months)} eligible months"
                if trend.average_cents is not None
                else "Insufficient history for average monthly spending."
            )
            children += [
                html.P(average),
                dcc.Graph(
                    figure=cashflow_figure(
                        trend.series,
                        selection.currency or "",
                        spending_only=True,
                        average_cents=trend.average_cents,
                    )
                ),
            ]
        children.append(transaction_grid(f"{prefix}-drill", json_rows(selected)))
        return children
