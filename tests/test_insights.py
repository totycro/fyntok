"""Financial reconciliation, observed-history limits, and dashboard interactions."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import plotly.graph_objects as go
import pytest
from sqlalchemy.engine import Engine

from fyntok.analysis import aggregate
from fyntok.charts import breakdown_figure, cashflow_figure, spending_heatmap
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.insights import (
    Period,
    build_report,
    comparison_period,
    currency_options,
    default_overview_month,
    month_period,
    period_label,
)
from fyntok.manual import correct_transaction, save_category
from fyntok.queries import Filters, transaction_rows


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "insights.sqlite3")
    rows = [
        ("2024-01-15", "Food Jan", -1000, "Food", "EUR"),
        ("2024-02-15", "Food Feb", -3000, "Food", "EUR"),
        ("2024-02-16", "Travel Feb", -1000, "Travel", "EUR"),
        ("2024-03-15", "Food Mar", -5000, "Food", "EUR"),
        ("2024-03-16", "Travel Mar", -2000, "Travel", "EUR"),
        ("2024-03-17", "Food credit", 1000, "Food", "EUR"),
        ("2024-03-18", "Salary", 10000, "Salary", "EUR"),
        ("2024-03-19", "Income reversal", -500, "Salary", "EUR"),
        ("2024-03-20", "Unknown debit", -300, "", "EUR"),
        ("2024-03-21", "Unknown credit", 200, "", "EUR"),
        ("2024-03-22", "Own account", -99999, "Transfers", "EUR"),
        ("2024-03-23", "US shop", -10000, "Food", "USD"),
        ("2024-05-15", "Food May", -1000, "Food", "EUR"),
        ("2024-06-15", "Food Jun", -2000, "Food", "EUR"),
        ("2024-07-15", "Food Jul", -3000, "Food", "EUR"),
        ("2024-08-20", "Food Aug", -4000, "Food", "EUR"),
    ]
    definitions = {
        "Food": "spending",
        "Travel": "spending",
        "Salary": "income",
        "Transfers": "transfer",
    }
    ids = {name: save_category(engine, name, name, kind) for name, kind in definitions.items()}
    assignments = {description: category for _, description, _, category, _ in rows}
    content = ""
    for booking, description, cents, _, currency in rows:
        day = date.fromisoformat(booking).strftime("%d.%m.%Y")
        amount = f"{'-' if cents < 0 else ''}{abs(cents) // 100},{abs(cents) % 100:02d}"
        content += f"a;{description};{day};{day};{amount};{currency}\n"
    import_content(engine, content.encode(), "synthetic.csv")
    for row in transaction_rows(engine):
        category = assignments[row["description"]]
        if category:
            correct_transaction(engine, row["id"], ids[category])
    try:
        yield engine
    finally:
        engine.dispose()


def test_cash_flow_reconciles_and_preserves_credit_convention(ledger: Engine) -> None:
    period = month_period(date(2024, 3, 1))
    report = build_report(ledger, Filters(currency="EUR"), period)
    assert report.totals == {
        "spending_cents": 7800,
        "income_cents": 11000,
        "net_cents": 3400,
        "outgoing_cents": 7800,
        "incoming_cents": 11200,
        "uncategorized_outgoing_cents": 300,
        "uncategorized_incoming_cents": 200,
    }
    assert (
        report.totals["net_cents"]
        == report.totals["incoming_cents"] - report.totals["outgoing_cents"]
    )
    assert sum(row["spending_cents"] for row in report.categories) == 7800
    assert sum(row["spending_cents"] for row in report.merchants) == 7800
    assert report.totals["spending_cents"] == report.totals["outgoing_cents"]
    assert (
        sum(row["spending_cents"] for row in report.categories) == report.totals["spending_cents"]
    )
    unknown = next(row for row in report.categories if row["key"] == "uncategorized")
    assert unknown["spending_cents"] == 300
    assert unknown["monthly_cents"] == {"2024-03": 300}
    assert {row["name"] for row in report.merchants} == {
        "Food Mar",
        "Travel Mar",
        "Unknown debit",
        "Income reversal",
    }
    assert sum(row["share"] for row in report.categories) == pytest.approx(100)
    assert report.previous["spending_cents"] == 4000
    assert sum(row["change_cents"] for row in report.category_changes) == 3800
    assert sum(row["change_cents"] for row in report.merchant_changes) == 3800
    food = next(row for row in report.category_changes if row["name"] == "Food")
    assert food["change_cents"] == 2000
    assert food["change_percent"] == pytest.approx(200 / 3)
    assert currency_options(ledger, Filters()) == ["EUR", "USD"]
    usd = build_report(ledger, Filters(currency="USD"), period)
    assert usd.totals["spending_cents"] == 10000
    # Existing CLI aggregates still include categorized credits in the income bucket.
    credit = next(
        row
        for row in aggregate(ledger)
        if row["category_name"] == "Food" and row["bucket"] == "income"
    )
    assert credit["incoming_cents"] == 1000


def test_gaps_partial_months_and_category_averages(ledger: Engine) -> None:
    report = build_report(
        ledger, Filters(currency="EUR"), Period(date(2024, 1, 1), date(2024, 8, 31))
    )
    assert report.eligible_months == ["2024-02", "2024-03", "2024-05", "2024-06", "2024-07"]
    assert report.average_cents == 3560
    assert report.median_cents == 3000
    april = next(row for row in report.series if row["month"] == "2024-04")
    assert not april["observed"]
    food = next(row for row in report.categories if row["name"] == "Food")
    travel = next(row for row in report.categories if row["name"] == "Travel")
    assert food["average_cents"] == 2800
    assert food["monthly_cents"]["2024-04"] is None
    assert travel["monthly_cents"]["2024-05"] == 0
    assert travel["average_cents"] == 600
    unknown = next(row for row in report.categories if row["key"] == "uncategorized")
    assert unknown["average_cents"] == 60
    assert unknown["median_cents"] == 0
    assert unknown["monthly_cents"]["2024-03"] == 300
    assert unknown["monthly_cents"]["2024-04"] is None
    partial = build_report(
        ledger, Filters(currency="EUR"), Period(date(2024, 3, 16), date(2024, 3, 25))
    )
    assert partial.average_cents is None
    assert partial.median_cents is None
    assert partial.totals["spending_cents"] == 2800
    clipped = build_report(ledger, Filters(currency="EUR"), report.period, today=date(2024, 6, 10))
    assert clipped.eligible_months == ["2024-02", "2024-03", "2024-05"]


def test_baseline_needs_history_and_keeps_non_date_filters(ledger: Engine) -> None:
    august = build_report(
        ledger, Filters(currency="EUR", source="manual"), month_period(date(2024, 8, 1))
    )
    assert august.baseline_months == ["2024-02", "2024-03", "2024-05", "2024-06", "2024-07"]
    food = august.above_baseline[0]
    assert food["baseline_cents"] == 2800
    assert food["above_cents"] == 1200
    early = build_report(ledger, Filters(currency="EUR"), month_period(date(2024, 3, 1)))
    assert early.above_baseline == []
    selected = build_report(
        ledger,
        Filters(currency="EUR", merchant="Food Mar", search="Food", source="manual"),
        month_period(date(2024, 3, 1)),
    )
    assert selected.totals["spending_cents"] == 5000
    assert selected.previous["spending_cents"] == 0
    assert selected.comparison_observed
    assert selected.category_changes[0]["change_percent"] is None
    unassigned = build_report(
        ledger, Filters(currency="EUR", source="unassigned"), month_period(date(2024, 3, 1))
    )
    assert unassigned.totals["spending_cents"] == 300
    assert unassigned.totals["net_cents"] == -100
    absent = build_report(
        ledger, Filters(currency="EUR", merchant="Absent"), month_period(date(2024, 3, 1))
    )
    assert absent.categories == []
    assert absent.totals["net_cents"] == 0
    year_ago = build_report(
        ledger, Filters(currency="EUR"), month_period(date(2024, 3, 1)), year_ago=True
    )
    assert not year_ago.comparison_observed


@pytest.mark.parametrize(
    ("period", "previous", "year_ago"),
    [
        (
            Period(date(2024, 3, 1), date(2024, 3, 31)),
            Period(date(2024, 2, 1), date(2024, 2, 29)),
            False,
        ),
        (
            Period(date(2024, 1, 1), date(2024, 12, 31)),
            Period(date(2023, 1, 1), date(2023, 12, 31)),
            False,
        ),
        (
            Period(date(2024, 3, 10), date(2024, 3, 20)),
            Period(date(2024, 2, 28), date(2024, 3, 9)),
            False,
        ),
        (
            Period(date(2023, 9, 1), date(2024, 8, 31)),
            Period(date(2022, 9, 1), date(2023, 8, 31)),
            False,
        ),
        (
            Period(date(2024, 2, 1), date(2024, 2, 29)),
            Period(date(2023, 2, 1), date(2023, 2, 28)),
            True,
        ),
    ],
)
def test_comparison_boundaries(period: Period, previous: Period, year_ago: bool) -> None:
    assert comparison_period(period, year_ago=year_ago) == previous


def test_figures_keep_gaps_and_annual_observed_totals(ledger: Engine) -> None:
    report = build_report(
        ledger, Filters(currency="EUR"), Period(date(2024, 1, 1), date(2024, 8, 31))
    )
    figure = cashflow_figure(report.series, "EUR", rolling=True)
    spending = next(trace for trace in figure.data if trace.name == "Spending")
    assert spending.y[3] is None
    assert spending.y[2] == 78
    outgoing = next(trace for trace in figure.data if trace.name == "Uncategorized outgoing")
    incoming = next(trace for trace in figure.data if trace.name == "Uncategorized incoming")
    assert outgoing.y[2] == 3
    assert incoming.y[2] == 2
    assert outgoing.y[3] is None
    assert outgoing.customdata[2]["bucket"] == "uncategorized_outgoing"
    assert incoming.customdata[2]["bucket"] == "uncategorized_incoming"
    average = next(trace for trace in figure.data if trace.name == "3-month average spending")
    assert average.y[4] is None
    assert average.y[6] == 20
    yearly = cashflow_figure(report.series, "EUR", yearly=True)
    assert yearly.data[0].y == (228.0,)
    breakdown = breakdown_figure(report.categories, "EUR")
    assert len(breakdown.data[0].customdata) == 4
    assert all("category_id" in item for item in breakdown.data[0].customdata)
    heatmap = spending_heatmap(report.categories, [row["month"] for row in report.series], "EUR")
    assert heatmap.data[0].z[0][3] is None


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2024, 1, 1), date(2024, 8, 31), 1000),
        (date(2024, 3, 16), date(2024, 7, 31), 0),
        (date(2024, 1, 1), date(2024, 1, 31), None),
    ],
)
def test_monthly_average_line_uses_filtered_eligible_months(
    ledger: Engine, start: date, end: date, expected: int | None
) -> None:
    report = build_report(ledger, Filters(currency="EUR", merchant="Food Mar"), Period(start, end))
    assert report.average_cents == expected
    figure = cashflow_figure(
        report.series, "EUR", spending_only=True, average_cents=report.average_cents
    )
    if expected is None:
        assert not figure.layout.shapes
    else:
        (line,) = figure.layout.shapes
        assert line.y0 == line.y1 == expected / 100
        assert line.line.dash == "dash"
        assert line.name == "Average monthly spending"
        assert line.showlegend
    assert not cashflow_figure(report.series, "EUR").layout.shapes


@pytest.mark.parametrize(
    ("currency", "month", "grouping", "name", "description", "cents", "average"),
    [
        ("EUR", 3, "category", "Travel", "Travel Mar", 2000, 1500),
        ("EUR", 3, "merchant", "Food Mar", "Food Mar", 5000, 2500),
        ("EUR", 1, "category", "Food", "Food Jan", 1000, None),
        ("EUR", 3, "category", "Uncategorized", "Unknown debit", 300, 150),
        ("EUR", 3, "category", "Salary", "Income reversal", 500, 250),
        ("EUR", 3, "merchant", "Unknown debit", "Unknown debit", 300, 150),
        ("USD", 3, "category", "Food", "US shop", 10000, None),
    ],
)
def test_selected_spending_and_average_reconcile(
    ledger: Engine,
    currency: str,
    month: int,
    grouping: str,
    name: str,
    description: str,
    cents: int,
    average: int | None,
) -> None:
    period = month_period(date(2024, month, 1))
    report = build_report(ledger, Filters(currency=currency), period)
    groups = report.categories if grouping == "category" else report.merchants
    group = next(row for row in groups if row["name"] == name)
    selection = replace(
        report.filters,
        category_id=group["key"] if grouping == "category" else None,
        merchant=group["key"] if grouping == "merchant" else None,
    )
    debits = [row for row in transaction_rows(ledger, selection) if row["amount_cents"] < 0]
    assert [(row["description"], row["amount_cents"]) for row in debits] == [(description, -cents)]
    assert {row["currency"] for row in debits} == {currency}
    assert group["spending_cents"] == cents
    assert build_report(ledger, selection, period).totals["spending_cents"] == cents
    trend = build_report(ledger, selection, Period(date(2024, 1, 1), period.end))
    assert trend.eligible_months == (["2024-02", "2024-03"] if average is not None else [])
    assert trend.average_cents == average
    figure = cashflow_figure(
        trend.series, currency, spending_only=True, average_cents=trend.average_cents
    )
    if average is None:
        assert not figure.layout.shapes
    else:
        (line,) = figure.layout.shapes
        assert line.y0 == line.y1 == average / 100


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2024, 8, 20), "2024-07"),
        (date(2024, 8, 1), "2024-07"),
        (date(2025, 1, 1), "2024-08"),
        (date(2024, 1, 20), "2024-08"),
    ],
)
def test_analysis_monthly_view_defaults_to_completed_month(today: date, expected: str) -> None:
    assert default_overview_month(Period(date(2024, 1, 15), date(2024, 8, 20)), today) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2024, 3, 1), date(2024, 3, 31), "March 2024"),
        (date(2024, 1, 1), date(2024, 12, 31), "2024"),
        (date(2024, 2, 10), date(2024, 3, 20), "2024-02-10 through 2024-03-20"),
        (date(2024, 3, 10), date(2024, 3, 20), "2024-03-10 through 2024-03-20"),
        (date(2024, 4, 1), date(2024, 4, 30), "April 2024"),
        (date(2024, 2, 1), date(2024, 2, 29), "February 2024"),
        (date(2024, 1, 1), date(2024, 12, 30), "2024-01-01 through 2024-12-30"),
    ],
)
def test_analysis_period_label(start: date, end: date, expected: str) -> None:
    assert period_label(Period(start, end)) == expected


def test_overview_chart_limits_categories() -> None:
    groups: list[dict[str, Any]] = [
        {
            "key": f"category-{number}",
            "name": f"Category {number}",
            "spending_cents": 2500 - number * 100,
            "share": 4,
        }
        for number in range(25)
    ]
    figure = breakdown_figure(groups, "EUR", show_amounts=True)
    (trace,) = figure.data
    assert isinstance(trace, go.Bar)
    assert len(trace.customdata) == 20
    assert list(trace.customdata) == [{"category_id": row["key"]} for row in reversed(groups[:20])]
    assert list(trace.x) == [row["spending_cents"] / 100 for row in reversed(groups[:20])]
    assert list(trace.y) == [row["name"] for row in reversed(groups[:20])]
    assert len(groups) == 25  # Limiting the view must not truncate the report.
