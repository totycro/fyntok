"""Financial reconciliation, observed-history limits, and dashboard interactions."""

import asyncio
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Self, override
from unittest.mock import patch

import plotly.graph_objects as go
import pytest
from nicegui import events, ui
from nicegui.testing.user_simulation import user_simulation
from sqlalchemy.engine import Engine

from fyntok.analysis import aggregate
from fyntok.analysis_plot import AnalysisPlot
from fyntok.analysis_ui import AnalysisState, build_analysis
from fyntok.app import build_page, columns, money
from fyntok.auth import ChatGPTSession
from fyntok.charts import breakdown_figure, cashflow_figure, category_color, spending_heatmap
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.insights import (
    Period,
    build_report,
    comparison_period,
    currency_options,
    month_period,
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
        "spending_cents": 7000,
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
    assert sum(row["spending_cents"] for row in report.categories) == 7000
    assert sum(row["spending_cents"] for row in report.merchants) == 7000
    assert sum(row["share"] for row in report.categories) == pytest.approx(100)
    assert report.previous["spending_cents"] == 4000
    assert sum(row["change_cents"] for row in report.category_changes) == 3000
    assert sum(row["change_cents"] for row in report.merchant_changes) == 3000
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
    assert report.average_cents == 3400
    assert report.median_cents == 3000
    april = next(row for row in report.series if row["month"] == "2024-04")
    assert not april["observed"]
    food = next(row for row in report.categories if row["name"] == "Food")
    travel = next(row for row in report.categories if row["name"] == "Travel")
    assert food["average_cents"] == 2800
    assert food["monthly_cents"]["2024-04"] is None
    assert travel["monthly_cents"]["2024-05"] == 0
    assert travel["average_cents"] == 600
    partial = build_report(
        ledger, Filters(currency="EUR"), Period(date(2024, 3, 16), date(2024, 3, 25))
    )
    assert partial.average_cents is None
    assert partial.median_cents is None
    assert partial.totals["spending_cents"] == 2000
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
    assert unassigned.totals["spending_cents"] == 0
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
    assert spending.y[2] == 70
    average = next(trace for trace in figure.data if trace.name == "3-month average spending")
    assert average.y[4] is None
    assert average.y[6] == 20
    yearly = cashflow_figure(report.series, "EUR", yearly=True)
    assert yearly.data[0].y == (220.0,)
    breakdown = breakdown_figure(report.categories, "EUR")
    assert len(breakdown.data[0].customdata) == 2
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
    ("month", "grouping", "name", "message", "expected"),
    [
        ("2024-03", "Categories", "Travel", "15.00 EUR · 2 eligible months", 15),
        ("2024-03", "Merchants", "Food Mar", "25.00 EUR · 2 eligible months", 25),
        ("2024-01", "Categories", "Food", "Insufficient history for average monthly", None),
    ],
)
def test_drilldown_monthly_average(
    ledger: Engine,
    tmp_path: Path,
    month: str,
    grouping: str,
    name: str,
    message: str,
    expected: int | None,
) -> None:
    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            month_select = user.find("Overview month").elements.pop()
            breakdown_toggle = user.find(marker="analysis-breakdown").elements.pop()
            assert isinstance(month_select, ui.select)
            assert isinstance(breakdown_toggle, ui.toggle)
            with user:
                month_select.set_value(month)
                breakdown_toggle.set_value(grouping)
            await asyncio.sleep(0.05)  # NiceGUI schedules refreshes in the background.
            breakdown = user.find(marker="analysis-breakdown-table").elements.pop()
            assert isinstance(breakdown, ui.table)
            row = next(row for row in breakdown.rows if row["name"] == name)
            with user:
                assert user.client is not None
                listener = next(
                    listener
                    for listener in breakdown._event_listeners.values()
                    if listener.type == "rowClick"
                )
                events.handle_event(
                    listener.handler,
                    events.GenericEventArguments(
                        sender=breakdown, client=user.client, args=[{}, row, 0]
                    ),
                )
            await user.should_see(message)
            dialog = next(d for d in user.find(ui.dialog).elements if d.value)
            plot = next(p for p in dialog.descendants() if isinstance(p, ui.plotly))
            assert isinstance(plot.figure, go.Figure)
            if expected is None:
                assert not plot.figure.layout.shapes
            else:
                (line,) = plot.figure.layout.shapes
                assert line.y0 == line.y1 == expected

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2024, 8, 20), "2024-07"),
        (date(2024, 8, 1), "2024-07"),
        (date(2025, 1, 1), "2024-08"),
        (date(2024, 1, 20), "2024-08"),
    ],
)
def test_analysis_monthly_view_defaults_to_completed_month(
    ledger: Engine, tmp_path: Path, today: date, expected: str
) -> None:
    class Calendar(date):
        @classmethod
        @override
        def today(cls) -> Self:
            return cls(today.year, today.month, today.day)

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            month = user.find("Overview month").elements.pop()
            assert isinstance(month, ui.select)
            assert month.value == expected

    with patch("fyntok.analysis_ui.date", Calendar):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("state", "filters", "expected"),
    [
        (AnalysisState(month="2024-03"), Filters(), "March 2024"),
        (
            AnalysisState(view="Historical explorer", history_period="2024"),
            Filters(),
            "2024",
        ),
        (
            AnalysisState(
                view="Historical explorer",
                history_period="Custom dates",
                custom_start="2024-02-10",
                custom_end="2024-03-20",
            ),
            Filters(),
            "2024-02-10 through 2024-03-20",
        ),
        (
            AnalysisState(month="2024-03"),
            Filters(start=date(2024, 3, 10), end=date(2024, 3, 20), currency="EUR"),
            "2024-03-10 through 2024-03-20",
        ),
        (AnalysisState(month="2024-04"), Filters(), "April 2024"),
        (AnalysisState(), Filters(search="No matching transaction"), None),
        (
            AnalysisState(
                view="Historical explorer",
                history_period="Custom dates",
                custom_start="invalid",
            ),
            Filters(),
            None,
        ),
    ],
)
def test_analysis_context_uses_effective_period(
    ledger: Engine, state: AnalysisState, filters: Filters, expected: str | None
) -> None:
    async def scenario() -> None:
        async with user_simulation(
            lambda: build_analysis(ledger, filters, state, lambda _row: None, money, columns)
        ) as user:
            await user.open("/")
            if expected is None:
                await user.should_not_see(marker="analysis-context")
            else:
                context = user.find(marker="analysis-context").elements.pop()
                labels = [
                    label.text for label in context.descendants() if isinstance(label, ui.label)
                ]
                assert labels == [expected, f"{state.view} · EUR"]

    asyncio.run(scenario())


def test_analysis_navigation_and_drilldown_preserve_state(ledger: Engine, tmp_path: Path) -> None:
    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            await user.should_see("40.00 EUR")
            await user.should_not_see("What changed?")
            await user.should_not_see("Compare with")
            month = user.find("Overview month").elements.pop()
            assert isinstance(month, ui.select)
            with user:
                month.set_value("2024-03")
            await user.should_see("70.00 EUR")
            await user.should_see("March 2024")
            user.find(marker="analysis-drill").click()
            await user.should_see("Transactions behind this report")
            dialog = next(d for d in user.find(ui.dialog).elements if d.value)
            table = next(t for t in dialog.descendants() if isinstance(t, ui.table))
            assert len(table.rows) == 7
            assert {row["currency"] for row in table.rows} == {"EUR"}
            assert sum(row["amount_cents"] for row in table.rows) == 3400
            user.find("Back to analysis").click()
            month = user.find("Overview month").elements.pop()
            assert isinstance(month, ui.select)
            assert month.value == "2024-03"
            await user.should_see("March 2024")
            breakdown = user.find(marker="analysis-breakdown-table").elements.pop()
            assert isinstance(breakdown, ui.table)
            food = next(row for row in breakdown.rows if row["name"] == "Food")
            with user:
                assert user.client is not None
                listener = next(
                    listener
                    for listener in breakdown._event_listeners.values()
                    if listener.type == "rowClick"
                )
                events.handle_event(
                    listener.handler,
                    events.GenericEventArguments(
                        sender=breakdown, client=user.client, args=[{}, food, 0]
                    ),
                )
            dialog = next(d for d in user.find(ui.dialog).elements if d.value)
            table = next(t for t in dialog.descendants() if isinstance(t, ui.table))
            assert len(table.rows) == 1
            assert sum(row["amount_cents"] for row in table.rows) == -5000
            user.find("Back to analysis").click()
            view = user.find(marker="analysis-view").elements.pop()
            assert isinstance(view, ui.toggle)
            with user:
                view.set_value("Historical explorer")
            await user.should_see("average 34.00 EUR")
            await user.should_see("What changed?")
            await user.should_see("Compare with")
            await user.should_see("Historical explorer · EUR")
            await user.should_see(marker="analysis-month-table")
            grouping = user.find("Trend grouping").elements.pop()
            assert isinstance(grouping, ui.select)
            with user:
                grouping.set_value("Yearly")
            await asyncio.sleep(0.05)  # NiceGUI schedules refreshes in the background.
            # Simulate the actual Plotly event shape to inspect a year without changing controls.
            plot = next(
                plot
                for plot in user.find(ui.plotly).elements
                if isinstance(plot.figure, go.Figure)
                and plot.figure.layout.title.text == "Annual trends"
            )
            with user:
                assert user.client is not None
                listener = next(
                    listener
                    for listener in plot._event_listeners.values()
                    if listener.type == "plotly_click"
                )
                events.handle_event(
                    listener.handler,
                    events.GenericEventArguments(
                        sender=plot,
                        client=user.client,
                        args={
                            "points": [{"customdata": {"start": "2024-01-01", "end": "2024-12-31"}}]
                        },
                    ),
                )
            await user.should_see("Transactions behind this report")
            dialog = next(d for d in user.find(ui.dialog).elements if d.value)
            table = next(t for t in dialog.descendants() if isinstance(t, ui.table))
            assert all(
                row["currency"] == "EUR" and row["category"] != "Transfers" for row in table.rows
            )
            user.find("Back to analysis").click()
            assert grouping.value == "Yearly"
            assert view.value == "Historical explorer"

    asyncio.run(scenario())


def test_analysis_context_updates_currency(ledger: Engine, tmp_path: Path) -> None:
    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            await user.should_see("Monthly overview · EUR")
            currency = user.find("Analysis currency").elements.pop()
            assert isinstance(currency, ui.select)
            with user:
                currency.set_value("USD")
            await user.should_see("Monthly overview · USD")
            await user.should_see("March 2024")
            await user.should_not_see("Monthly overview · EUR")

    asyncio.run(scenario())


def test_empty_filter_results_and_invalid_dates(ledger: Engine, tmp_path: Path) -> None:
    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            user.find("Description contains").type("no such merchant")
            user.find("Apply filters").click()
            await user.should_see("0 matching transactions")
            await user.should_see(marker="analysis-spending_cents")
            await user.should_see(marker="overview-spending_cents")
            user.find(marker="clear-transaction-filters").click()
            await user.should_see("16 matching transactions")
            user.find("Booking date from").type("2025-01-01")
            user.find("Through").type("2024-01-01")
            user.find("Apply filters").click()
            await user.should_see("Start date must be before the end date.")
            await user.should_see(marker="analysis-spending_cents")
            await user.should_see(marker="overview-spending_cents")

    asyncio.run(scenario())


def test_simple_overview_month_currency_and_category_totals(ledger: Engine, tmp_path: Path) -> None:
    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            assert any(panel.value == "Overview" for panel in user.find(ui.tab_panels).elements)
            month = user.find(marker="overview-month").elements.pop()
            assert isinstance(month, ui.select)
            assert month.value == "2024-08"
            assert month.options == [f"2024-{number:02}" for number in range(8, 0, -1)]
            await user.should_see("Partial month:")
            with user:
                month.set_value("2024-03")
            await user.should_see("March 2024 · EUR")
            spending = user.find(marker="overview-spending_cents").elements.pop()
            income = user.find(marker="overview-income_cents").elements.pop()
            assert isinstance(spending, ui.label)
            assert isinstance(income, ui.label)
            assert spending.text == "70.00 EUR"
            assert income.text == "110.00 EUR"
            plot = user.find(marker="overview-category-chart").elements.pop()
            assert isinstance(plot, AnalysisPlot)
            assert isinstance(plot.figure, go.Figure)
            trace = plot.figure.data[0]
            assert isinstance(trace, go.Bar)
            assert list(zip(trace.y, trace.x, strict=True)) == [("Travel", 20), ("Food", 50)]
            await user.should_not_see(marker="overview-categories")
            await user.should_see("Uncategorized · outgoing 3.00 EUR · incoming 2.00 EUR")
            month = user.find(marker="overview-month").elements.pop()
            assert isinstance(month, ui.select)
            with user:
                month.set_value("2024-04")
            await user.should_see("No recorded activity this month.")
            await user.should_not_see(marker="overview-category-chart")
            currency = user.find(marker="overview-currency").elements.pop()
            assert isinstance(currency, ui.select)
            with user:
                currency.set_value("USD")
            await user.should_see("March 2024 · USD")
            spending = user.find(marker="overview-spending_cents").elements.pop()
            assert isinstance(spending, ui.label)
            assert spending.text == "100.00 USD"
            month = user.find(marker="overview-month").elements.pop()
            assert isinstance(month, ui.select)
            assert month.value == "2024-03"

    asyncio.run(scenario())


@pytest.mark.parametrize("currency, expected_cents", [("EUR", 5000), ("USD", 10000)])
def test_overview_category_drilldown(
    ledger: Engine, tmp_path: Path, currency: str, expected_cents: int
) -> None:
    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            month = user.find(marker="overview-month").elements.pop()
            assert isinstance(month, ui.select)
            with user:
                month.set_value("2024-03")
            await user.should_see("March 2024 · EUR")
            currency_select = user.find(marker="overview-currency").elements.pop()
            assert isinstance(currency_select, ui.select)
            with user:
                currency_select.set_value(currency)
            await user.should_see(f"March 2024 · {currency}")
            plot = user.find(marker="overview-category-chart").elements.pop()
            assert isinstance(plot, AnalysisPlot)
            report = build_report(
                ledger, Filters(currency=currency), Period(date(2024, 3, 1), date(2024, 3, 31))
            )
            food = next(row for row in report.categories if row["name"] == "Food")
            figure = plot.figure
            assert isinstance(figure, go.Figure)
            trace = figure.data[0]
            assert isinstance(trace, go.Bar)
            assert list(trace.x) == [
                row["spending_cents"] / 100 for row in reversed(report.categories)
            ]
            assert list(trace.marker.color) == [
                category_color(row["key"]) for row in reversed(report.categories)
            ]
            args = {"points": [{"customdata": {"category_id": food["key"]}}]}
            with user:
                assert user.client is not None
                listener = next(
                    item for item in plot._event_listeners.values() if item.type == "plotly_click"
                )
                events.handle_event(
                    listener.handler,
                    events.GenericEventArguments(sender=plot, client=user.client, args=args),
                )
            await user.should_see("Category: Food")
            transactions = user.find(marker="overview-transactions").elements.pop()
            assert isinstance(transactions, ui.table)
            assert len(transactions.rows) == 1
            assert -sum(row["amount_cents"] for row in transactions.rows) == expected_cents
            assert {row["currency"] for row in transactions.rows} == {currency}
            assert all(row["booking_date"].startswith("2024-03-") for row in transactions.rows)
            dialog = next(item for item in user.find(ui.dialog).elements if item.value)
            user.find("Back to overview").click()
            assert not dialog.value
            # User simulation has no browser to emit Quasar's completed hide transition.
            with user:
                hide_listener = next(
                    item for item in dialog._event_listeners.values() if item.type == "hide"
                )
                events.handle_event(
                    hide_listener.handler,
                    events.GenericEventArguments(sender=dialog, client=user.client, args={}),
                )
            await user.should_not_see(marker="overview-transactions")
            selected_month = user.find(marker="overview-month").elements.pop()
            selected_currency = user.find(marker="overview-currency").elements.pop()
            assert isinstance(selected_month, ui.select)
            assert isinstance(selected_currency, ui.select)
            assert selected_month.value == "2024-03"
            assert selected_currency.value == currency
            with user:
                events.handle_event(
                    listener.handler,
                    events.GenericEventArguments(sender=plot, client=user.client, args=args),
                )
            transactions = user.find(marker="overview-transactions").elements.pop()
            assert isinstance(transactions, ui.table)
            with user:
                row_listener = next(
                    item
                    for item in transactions._event_listeners.values()
                    if item.type == "rowClick"
                )
                events.handle_event(
                    row_listener.handler,
                    events.GenericEventArguments(
                        sender=transactions,
                        client=user.client,
                        args=[{}, transactions.rows[0], 0],
                    ),
                )
            await user.should_see("Transaction details")

    asyncio.run(scenario())


def test_overview_chart_limits_categories(ledger: Engine, tmp_path: Path) -> None:
    content = "".join(f"a;Extra {number};15.03.2024;15.03.2024;-0,01;EUR\n" for number in range(21))
    import_content(ledger, content.encode(), "extra-categories.csv")
    for row in transaction_rows(ledger, Filters(search="Extra")):
        category_id = save_category(ledger, row["description"], "Extra spending", "spending")
        correct_transaction(ledger, row["id"], category_id)

    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            month = user.find(marker="overview-month").elements.pop()
            assert isinstance(month, ui.select)
            with user:
                month.set_value("2024-03")
            await user.should_see("March 2024 · EUR")
            plot = user.find(marker="overview-category-chart").elements.pop()
            assert isinstance(plot, AnalysisPlot)
            assert isinstance(plot.figure, go.Figure)
            trace = plot.figure.data[0]
            assert isinstance(trace, go.Bar)
            assert len(trace.customdata) == 20
            assert (
                len(
                    build_report(
                        ledger, Filters(currency="EUR"), month_period(date(2024, 3, 1))
                    ).categories
                )
                == 23
            )
            await user.should_not_see(marker="overview-categories")

    asyncio.run(scenario())


def test_tab_filters_periods_and_selections_are_independent(ledger: Engine, tmp_path: Path) -> None:
    async def scenario() -> None:
        async with user_simulation(
            lambda: build_page(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
        ) as user:
            await user.open("/")
            month = user.find(marker="overview-month").elements.pop()
            assert isinstance(month, ui.select)
            with user:
                month.set_value("2024-03")
            await user.should_see("March 2024 · EUR")
            user.find(kind=ui.tab, content="Transactions").click()
            user.find("Booking date from").type("2024-03-19")
            user.find("Through").type("2024-03-20")
            user.find("Apply filters").click()
            await user.should_see("2 matching transactions")
            user.find(kind=ui.tab, content="Analysis").click()
            month = user.find("Overview month").elements.pop()
            assert isinstance(month, ui.select)
            with user:
                month.set_value("2024-03")
            await user.should_see(marker="analysis-spending_cents", content="70.00 EUR")
            user.find("Analysis description contains").type("Food")
            user.find("Apply analysis filters").click()
            await user.should_see(marker="analysis-spending_cents", content="50.00 EUR")
            spending = user.find(marker="analysis-spending_cents").elements.pop()
            assert isinstance(spending, ui.label)
            assert spending.text == "50.00 EUR"
            context = user.find(marker="analysis-context").elements.pop()
            assert (
                next(e.text for e in context.descendants() if isinstance(e, ui.label))
                == "March 2024"
            )
            view = user.find(marker="analysis-view").elements.pop()
            assert isinstance(view, ui.toggle)
            with user:
                view.set_value("Historical explorer")
            await user.should_see("History period")
            history = user.find("History period").elements.pop()
            assert isinstance(history, ui.select)
            user.find(marker="analysis-context").elements.pop().mark("old-period")
            with user:
                history.set_value("2024")
            await user.should_not_see(marker="old-period")
            context = user.find(marker="analysis-context").elements.pop()
            assert next(e.text for e in context.descendants() if isinstance(e, ui.label)) == "2024"
            user.find(kind=ui.tab, content="Transactions").click()
            user.find("Description contains").type("Unknown")
            user.find("Apply filters").click()
            await user.should_see("1 matching transactions")
            # A refresh in another tab preserves both overview and analysis controls.
            user.find(kind=ui.tab, content="Overview").click()
            spending = user.find(marker="overview-spending_cents").elements.pop()
            assert isinstance(spending, ui.label)
            assert spending.text == "70.00 EUR"
            user.find(kind=ui.tab, content="Analysis").click()
            history = user.find("History period").elements.pop()
            assert isinstance(history, ui.select)
            assert history.value == "2024"
            user.find("Clear analysis filters").click()
            await user.should_see("Historical explorer · EUR")
            user.find("Analysis description contains").type("No matching description")
            user.find("Apply analysis filters").click()
            await user.should_see("No matching history.")
            user.find(kind=ui.tab, content="Transactions").click()
            user.find("Booking date from").clear().type("2025-01-01")
            user.find("Apply filters").click()
            await user.should_see("Start date must be before the end date.")
            user.find(kind=ui.tab, content="Overview").click()
            spending = user.find(marker="overview-spending_cents").elements.pop()
            assert isinstance(spending, ui.label)
            assert spending.text == "70.00 EUR"

    asyncio.run(scenario())
