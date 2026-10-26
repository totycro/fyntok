"""Plot only SQL-derived totals; currency series are always separate."""

from collections import defaultdict
from math import floor, log10
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def monthly_figure(rows: list[dict[str, Any]]) -> go.Figure:
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        subplot_titles=("Monthly totals by category and financial bucket", "Transaction counts"),
    )
    series = defaultdict(list)
    for row in rows:
        series[(row["currency"], row["category_name"], row["bucket"])].append(row)
    for (currency, category, bucket), values in series.items():
        name = f"{category} · {bucket.replace('_', ' ')} · {currency}"
        amounts = [
            r["outgoing_cents"]
            if bucket == "spending"
            else r["incoming_cents"]
            if bucket == "income"
            else r["net_cents"]
            for r in values
        ]
        figure.add_trace(
            go.Bar(x=[r["month"] for r in values], y=[v / 100 for v in amounts], name=name),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Scatter(
                x=[r["month"] for r in values],
                y=[r["transaction_count"] for r in values],
                name=name,
                showlegend=False,
                mode="lines+markers",
            ),
            row=2,
            col=1,
        )
    figure.update_layout(height=650, barmode="group", legend={"orientation": "h"})
    figure.update_yaxes(title_text="Amount (currency in legend)", row=1, col=1)
    figure.update_yaxes(title_text="Transactions", rangemode="tozero", row=2, col=1)
    return figure


def trend_figure(rows: list[dict[str, Any]]) -> go.Figure:
    series = defaultdict(list)
    for row in rows:
        series[(row["currency"], row["category_name"], row["bucket"])].append(row)
    months = sorted({r["month"] for r in rows})
    figure = go.Figure()
    for (currency, category, bucket), values in series.items():
        by_month = {r["month"]: r for r in values}
        field = (
            "outgoing_cents"
            if bucket == "spending"
            else "incoming_cents"
            if bucket == "income"
            else "net_cents"
        )
        figure.add_trace(
            go.Scatter(
                x=months,
                y=[by_month[m][field] / 100 if m in by_month else 0 for m in months],
                mode="lines",
                name=f"{category} · {bucket.replace('_', ' ')} · {currency}",
            )
        )
    figure.update_layout(
        title="Category trends", height=450, yaxis_title="Amount (currency in legend)"
    )
    return figure


# Dashboard figures use explicit gaps for months without observed history. Monetary
# values are converted to floating-point currency units only at this display boundary.
def category_color(key: str) -> str:
    from hashlib import sha256

    palette = (
        "#2563eb",
        "#9333ea",
        "#0891b2",
        "#ea580c",
        "#16a34a",
        "#db2777",
        "#4f46e5",
        "#a16207",
    )
    return palette[int.from_bytes(sha256(key.encode()).digest()[:2]) % len(palette)]


def breakdown_figure(
    rows: list[dict[str, Any]],
    currency: str,
    *,
    merchant: bool = False,
    show_amounts: bool = False,
) -> go.Figure:
    displayed = list(reversed(rows[:20]))
    figure = go.Figure()
    if displayed:
        figure.add_trace(
            go.Bar(
                x=[row["spending_cents"] / 100 for row in displayed],
                y=[row["name"] for row in displayed],
                orientation="h",
                marker_color=[category_color(row["key"]) for row in displayed],
                customdata=[
                    {"merchant" if merchant else "category_id": row["key"]} for row in displayed
                ],
                text=[f"{row['share']:.1f}%" for row in displayed],
                hovertext=[f"{row['share']:.1f}% of spending" for row in displayed],
                hovertemplate=(
                    "%{y}: %{x:,.2f} "
                    + currency
                    + "<br><span style='font-size:12px'>%{hovertext}</span><extra></extra>"
                ),
            )
        )
    figure.update_layout(
        title="Spending by merchant" if merchant else "Spending by category",
        height=max(300, len(displayed) * 28 + 110),
        margin={"l": 20, "r": 24, "t": 60, "b": 45},
        xaxis_title=currency,
        yaxis={"automargin": True},
    )
    if show_amounts and displayed:
        figure.update_traces(
            texttemplate="%{x:,.2f} " + currency,
            textposition="outside",
            textfont={"size": 14, "color": "#0f172a"},
            cliponaxis=False,
        )
        maximum = max(row["spending_cents"] for row in displayed) / 100
        figure.update_xaxes(range=[0, maximum * 1.3 if maximum else 1])
        figure.update_layout(margin={"r": 80})
    return figure


def cashflow_figure(
    series: list[dict[str, Any]],
    currency: str,
    *,
    yearly: bool = False,
    rolling: bool = False,
    spending_only: bool = False,
    categories: list[dict[str, Any]] | None = None,
    average_cents: int | None = None,
) -> go.Figure:
    from calendar import monthrange
    from datetime import date

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in series:
        grouped[row["month"][:4] if yearly else row["month"]].append(row)
    labels = list(grouped)
    metadata = []
    for label in labels:
        year = int(label[:4])
        month = 12 if yearly else int(label[5:])
        metadata.append(
            {
                "start": date(year, 1 if yearly else month, 1).isoformat(),
                "end": date(year, month, monthrange(year, month)[1]).isoformat(),
            }
        )
    figure = go.Figure()
    if average_cents is None and not any(
        row["incoming_cents"] or row["outgoing_cents"] for row in series
    ):
        return figure
    if bool(categories):
        for category in categories:
            amounts = [
                sum(category["monthly_cents"].get(row["month"]) or 0 for row in grouped[label])
                / 100
                if (
                    any(row["observed"] for row in grouped[label])
                    if yearly
                    else all(row["observed"] for row in grouped[label])
                )
                else None
                for label in labels
            ]
            figure.add_trace(
                go.Scatter(
                    x=labels,
                    y=amounts,
                    name=category["name"],
                    mode="lines+markers",
                    line_color=category_color(category["key"]),
                    connectgaps=False,
                    customdata=[dict(data, category_id=category["key"]) for data in metadata],
                )
            )
    else:
        fields = [("spending_cents", "Spending", "#2563eb")]
        if not spending_only:
            fields.extend(
                [
                    ("income_cents", "Income", "#16a34a"),
                    ("uncategorized_outgoing_cents", "Uncategorized outgoing", "#64748b"),
                    ("uncategorized_incoming_cents", "Uncategorized incoming", "#a16207"),
                    ("net_cents", "Net cash flow", "#9333ea"),
                ]
            )
        for field, name, color in fields:
            amounts = [
                sum(row[field] for row in grouped[label]) / 100
                if (
                    any(row["observed"] for row in grouped[label])
                    if yearly
                    else all(row["observed"] for row in grouped[label])
                )
                else None
                for label in labels
            ]
            if field == "net_cents":
                figure.add_trace(
                    go.Scatter(
                        x=labels,
                        y=amounts,
                        name=name,
                        mode="lines+markers",
                        line_color=color,
                        connectgaps=False,
                        customdata=[
                            dict(data, bucket=field.removesuffix("_cents")) for data in metadata
                        ],
                    )
                )
            else:
                figure.add_trace(
                    go.Bar(
                        x=labels,
                        y=amounts,
                        name=name,
                        marker_color=color,
                        customdata=[
                            dict(data, bucket=field.removesuffix("_cents")) for data in metadata
                        ],
                    )
                )
    if rolling and not yearly:
        averages: list[float | None] = []
        for index in range(len(series)):
            window = series[max(0, index - 2) : index + 1]
            averages.append(
                sum(row["spending_cents"] for row in window) / 300
                if len(window) == 3 and all(row["typical"] for row in window)
                else None
            )
        figure.add_trace(
            go.Scatter(
                x=labels,
                y=averages,
                name="3-month average spending",
                mode="lines",
                line={"color": "#ea580c", "dash": "dash"},
                connectgaps=False,
                customdata=metadata,
            )
        )
    if average_cents is not None:
        figure.add_hline(
            y=average_cents / 100,
            line_color="#ea580c",
            line_dash="dash",
            name="Average monthly spending",
            showlegend=True,
        )
    figure.update_layout(
        title="Annual trends" if yearly else "Monthly trends",
        height=440,
        barmode="group",
        yaxis_title=currency,
        legend={"orientation": "h", "x": 0, "xanchor": "left", "y": 1.02, "yanchor": "bottom"},
        xaxis={"type": "category"},
        margin={"l": 55, "r": 20, "t": 100, "b": 80},
        hovermode="x unified",
    )
    return figure


def spending_heatmap(rows: list[dict[str, Any]], months: list[str], currency: str) -> go.Figure:
    figure = go.Figure()
    displayed = rows[:30]
    if displayed:
        amounts = [
            [
                row["monthly_cents"][month] / 100
                if row["monthly_cents"][month] is not None
                else None
                for month in months
            ]
            for row in displayed
        ]
        maximum = max(
            (amount for row in amounts for amount in row if amount is not None), default=0
        )
        ticks = [0.0]
        if maximum >= 1:
            ticks.extend(10.0**exponent for exponent in range(floor(log10(maximum)) + 1))
        elif maximum > 0:
            ticks.append(maximum)
        figure.add_trace(
            go.Heatmap(
                x=months,
                y=[row["name"] for row in displayed],
                z=[
                    [log10(1 + amount) if amount is not None else None for amount in row]
                    for row in amounts
                ],
                zmin=0,
                zmax=log10(1 + maximum) if maximum > 0 else 1,
                customdata=[
                    [
                        {
                            "category_id": row["key"],
                            "start": f"{month}-01",
                            "month": month,
                            "amount": amount,
                        }
                        for month, amount in zip(months, monthly_amounts, strict=True)
                    ]
                    for row, monthly_amounts in zip(displayed, amounts, strict=True)
                ],
                colorscale="Blues",
                hoverongaps=False,
                hovertemplate=(
                    "%{y} · %{x}: %{customdata.amount:,.2f} " + currency + "<extra></extra>"
                ),
                colorbar={
                    "title": f"{currency} · log scale",
                    "tickvals": [log10(1 + amount) for amount in ticks],
                    "ticktext": [f"{amount:,.2f}" for amount in ticks],
                },
            )
        )
    figure.update_layout(
        title="Category spending by month (top 30)",
        height=max(300, len(displayed) * 26 + 120),
        yaxis={"autorange": "reversed", "automargin": True},
        margin={"l": 20, "r": 20, "t": 60, "b": 70},
        xaxis={"type": "category"},
    )
    return figure
