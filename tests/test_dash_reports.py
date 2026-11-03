"""Report controls and chart/grid drill-downs reconcile with the financial model."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from fyntok.analysis_ui import choose_report, drill_rows, filter_data
from fyntok.app import create_app
from fyntok.auth import ChatGPTSession
from fyntok.insights import Period, build_report
from fyntok.manual import categories
from fyntok.queries import Filters
from tests.test_dash_driver import DashDriver
from tests.test_insights import ledger as ledger


@pytest.fixture
def report_browser(ledger: Engine, tmp_path: Path) -> Iterator[DashDriver]:
    app = create_app(ledger, auth=ChatGPTSession(tmp_path / "credentials"))
    browser = DashDriver(app)
    browser.refresh()
    try:
        yield browser
    finally:
        app.server.extensions["fyntok-ai"].close()


def report(browser: DashDriver, month: str = "2024-03", **settings: Any) -> None:
    browser.set("analysis-month", "value", month)
    for name, value in settings.items():
        browser.set(f"analysis-{name}", "date" if name in {"start", "end"} else "value", value)
    browser.call("analysis-report.children", "analysis-month", "value", month)


def test_overview_ignores_tab_filters_and_preserves_month_and_currency(
    report_browser: DashDriver,
) -> None:
    browser = report_browser
    browser.call("overview-report.children", "overview-month", "value", "2024-03")
    assert "78.00 EUR" in str(browser.get("overview-report", "children"))
    browser.set("transaction-search", "value", "Missing")
    browser.call("transaction-filters.data", "transaction-apply")
    browser.set("analysis-search", "value", "Missing")
    browser.call("analysis-filters.data", "analysis-apply")
    browser.refresh()
    assert browser.get("overview-month", "value") == "2024-03"
    assert "78.00 EUR" in str(browser.get("overview-report", "children"))
    browser.call("overview-currency.options", "overview-currency", "value", "USD")
    browser.call("overview-report.children", "overview-currency", "value", "USD")
    assert "100.00 USD" in str(browser.get("overview-report", "children"))
    assert browser.get("analysis-currency", "value") is None
    browser.call("overview-report.children", "overview-month", "value", "2024-04")
    assert "No recorded activity" in str(browser.get("overview-report", "children"))


def test_monthly_report_typical_statistics_baseline_expenses_and_filters(
    report_browser: DashDriver,
    ledger: Engine,
) -> None:
    browser = report_browser
    report(browser, month="2024-08", rolling=["rolling"])
    content = str(browser.get("analysis-report", "children"))
    assert "Typical month" in content
    assert "Baseline:" in content
    assert "Largest expenses" in content
    report(browser, month="2024-03")
    assert "78.00 EUR" in str(browser.get("analysis-report", "children"))
    food = next(row["id"] for row in categories(ledger) if row["name"] == "Food")
    report(browser, month="2024-03", trends=[food], rolling=["rolling"])
    figure = browser.get({"type": "analysis-chart", "index": "trend"}, "figure")
    assert any(trace["name"] == "Food" for trace in figure["data"])
    browser.set("analysis-from", "date", "2024-03-16")
    browser.set("analysis-through", "date", "2024-07-31")
    browser.call("analysis-filters.data", "analysis-apply")
    report(browser)
    figure = browser.get({"type": "analysis-chart", "index": "trend"}, "figure")
    assert figure["data"][0]["x"] == ["2024-03"]
    assert "2024-03-16 through 2024-03-31" in str(browser.get("analysis-report", "children"))
    assert "Insufficient history" in str(browser.get("analysis-report", "children"))


@pytest.mark.parametrize("period", ["All history", "Last 12 months", "2024", "Custom dates"])
@pytest.mark.parametrize("breakdown", ["Categories", "Merchants"])
def test_historical_report_presets_changes_gaps_and_yearly_trends(
    report_browser: DashDriver,
    period: str,
    breakdown: str,
) -> None:
    browser = report_browser
    report(
        browser,
        view="Historical explorer",
        period=period,
        grouping="Yearly",
        breakdown=breakdown,
        start="2024-02-01",
        end="2024-08-31",
    )
    content = str(browser.get("analysis-report", "children"))
    assert "What changed?" in content
    assert "No recorded activity:" in content
    assert "2024-04" in content
    assert browser.get("analysis-month-table", "rowData")
    trend = browser.get({"type": "analysis-chart", "index": "trend"}, "figure")
    assert trend["data"][0]["x"] == (["2023", "2024"] if period == "Last 12 months" else ["2024"])
    report(
        browser,
        view="Historical explorer",
        period="Custom dates",
        start="2024-03-01",
        end="2024-03-31",
        comparison="Previous period",
    )
    assert "Spending change:" in str(browser.get("analysis-report", "children"))
    groups = browser.get({"type": "analysis-group", "index": "changes"}, "rowData")
    assert groups
    report(
        browser,
        view="Historical explorer",
        period="Custom dates",
        start="2024-03-01",
        end="2024-03-31",
        comparison="Same period last year",
    )
    assert "No comparison data" in str(browser.get("analysis-report", "children"))


@pytest.mark.parametrize(
    ("metadata", "descriptions", "cents"),
    [
        (
            {"bucket": "spending"},
            {"Food Mar", "Travel Mar", "Income reversal", "Unknown debit"},
            -7800,
        ),
        ({"bucket": "income"}, {"Food credit", "Salary"}, 11000),
        ({"bucket": "uncategorized_outgoing"}, {"Unknown debit"}, -300),
        ({"bucket": "uncategorized_incoming"}, {"Unknown credit"}, 200),
        ({"merchant": "Food Mar"}, {"Food Mar"}, -5000),
        ({"month": "2024-02"}, set(), 0),
    ],
)
def test_chart_drilldown_reconciles_to_each_series(
    report_browser: DashDriver,
    metadata: dict[str, Any],
    descriptions: set[str],
    cents: int,
) -> None:
    browser = report_browser
    report(
        browser,
        view="Historical explorer",
        period="Custom dates",
        start="2024-03-01",
        end="2024-03-31",
    )
    browser.call(
        "analysis-drill.children",
        {"type": "analysis-chart", "index": "trend"},
        "clickData",
        {"points": [{"customdata": metadata}]},
    )
    rows = browser.get({"type": "transactions", "index": "analysis-drill"}, "rowData")
    assert {row["description"] for row in rows} == descriptions
    assert sum(row["amount_cents"] for row in rows) == cents
    assert {row["currency"] for row in rows} <= {"EUR"}


@pytest.mark.parametrize("source", ["overview-chart", "analysis-chart", "analysis-group"])
def test_category_drilldown_and_transaction_editing(
    report_browser: DashDriver,
    ledger: Engine,
    source: str,
) -> None:
    browser = report_browser
    prefix = source.split("-")[0]
    if prefix == "analysis":
        report(browser)
    else:
        browser.call("overview-report.children", "overview-month", "value", "2024-03")
    food = next(row["id"] for row in categories(ledger) if row["name"] == "Food")
    clicked = {"type": source, "index": "breakdown"}
    prop = "cellClicked" if source.endswith("group") else "clickData"
    data = (
        {"rowId": food}
        if prop == "cellClicked"
        else {"points": [{"customdata": {"category_id": food}}]}
    )
    browser.call(f"{prefix}-drill.children", clicked, prop, data)
    rows = browser.get({"type": "transactions", "index": f"{prefix}-drill"}, "rowData")
    assert [row["description"] for row in rows] == ["Food Mar"]
    assert "Category: Food" in str(browser.get(f"{prefix}-drill", "children"))
    assert "Average monthly spending · 40.00 EUR" in str(browser.get(f"{prefix}-drill", "children"))
    browser.select(rows[0], f"{prefix}-drill")
    assert "Food Mar" in str(browser.get("transaction-description", "children"))
    assert browser.get(f"{prefix}-month", "value") == "2024-03"
    browser.call(f"{prefix}-drill.children", f"{prefix}-close-drill")
    assert browser.get(f"{prefix}-drill", "children") == []


def test_group_merchant_drilldown_and_trend_outside_selected_month(
    report_browser: DashDriver,
) -> None:
    browser = report_browser
    report(browser, breakdown="Merchants")
    browser.call(
        "analysis-drill.children",
        {"type": "analysis-group", "index": "breakdown"},
        "cellClicked",
        {"rowId": "Food Mar"},
    )
    assert "Merchant: Food Mar" in str(browser.get("analysis-drill", "children"))
    browser.call(
        "analysis-drill.children",
        {"type": "analysis-chart", "index": "trend"},
        "clickData",
        {
            "points": [
                {"customdata": {"start": "2024-02-01", "end": "2024-02-29", "bucket": "spending"}}
            ]
        },
    )
    rows = browser.get({"type": "transactions", "index": "analysis-drill"}, "rowData")
    assert {row["description"] for row in rows} == {"Food Feb", "Travel Feb"}
    assert browser.get("analysis-month", "value") == "2024-03"
    browser.call("analysis-drill.children", "analysis-all-transactions")
    rows = browser.get({"type": "transactions", "index": "analysis-drill"}, "rowData")
    assert sum(row["amount_cents"] for row in rows) == 3400
    browser.call("analysis-drill.children", "analysis-context", "data", None)
    assert browser.get("analysis-drill", "children") == []


def test_date_filters_constrain_trend_drilldown_and_heatmap(report_browser: DashDriver) -> None:
    browser = report_browser
    browser.set("analysis-from", "date", "2024-03-16")
    browser.set("analysis-through", "date", "2024-07-20")
    browser.call("analysis-filters.data", "analysis-apply")
    report(browser, month="2024-07", view="Historical explorer", period="2024")
    browser.call(
        "analysis-drill.children",
        {"type": "analysis-chart", "index": "trend"},
        "clickData",
        {
            "points": [
                {"customdata": {"start": "2024-01-01", "end": "2024-12-31", "bucket": "spending"}}
            ]
        },
    )
    rows = browser.get({"type": "transactions", "index": "analysis-drill"}, "rowData")
    assert all("2024-03-16" <= row["booking_date"] <= "2024-07-20" for row in rows)
    assert sum(row["amount_cents"] for row in rows) == -8800
    browser.call(
        "analysis-drill.children",
        {"type": "analysis-chart", "index": "heatmap"},
        "clickData",
        {"points": [{"customdata": {"month": "2024-03"}}]},
    )
    rows = browser.get({"type": "transactions", "index": "analysis-drill"}, "rowData")
    assert {row["description"] for row in rows} == {
        "Travel Mar",
        "Income reversal",
        "Unknown debit",
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"view": "Historical explorer", "period": "Custom dates", "start": None, "end": None},
        {
            "view": "Historical explorer",
            "period": "Custom dates",
            "start": "2024-03-01",
            "end": "2024-02-01",
        },
    ],
)
def test_invalid_custom_dates_show_error(
    report_browser: DashDriver, settings: dict[str, Any]
) -> None:
    report(report_browser, **settings)
    assert report_browser.get("analysis-context", "data") is None
    assert "error" in str(report_browser.get("analysis-report", "children"))


def test_no_history_and_nonoverlapping_dates_show_errors(report_browser: DashDriver) -> None:
    browser = report_browser
    browser.call("analysis-report.children", "analysis-currency", "value", "XXX")
    assert "No matching history" in str(browser.get("analysis-report", "children"))
    browser.set("analysis-currency", "value", "EUR")
    browser.set("analysis-from", "date", "2025-01-01")
    browser.call("analysis-filters.data", "analysis-apply")
    report(browser)
    assert "No history" in str(browser.get("analysis-report", "children"))
    browser.set("analysis-from", "date", "2024-04-01")
    browser.call("analysis-filters.data", "analysis-apply")
    report(browser, month="2024-03")
    assert browser.get("analysis-context", "data") is None


def test_report_scope_and_partial_month_drilldown(ledger: Engine) -> None:
    filters = Filters(currency="EUR", start=date(2024, 3, 16), end=date(2024, 3, 20))
    report = choose_report(ledger, filters, "EUR", "2024-03")
    assert report.period == Period(date(2024, 3, 16), date(2024, 3, 20))
    rows = drill_rows(ledger, filter_data(filters), {"start": "2024-03-01", "end": "2024-03-31"})
    assert sum(row["amount_cents"] for row in rows) == report.totals["net_cents"]
    full = build_report(
        ledger, Filters(currency="EUR"), Period(date(2024, 3, 1), date(2024, 3, 31))
    )
    assert full.totals["net_cents"] == 3400


def test_applied_dates_update_available_months_without_changing_overview(
    report_browser: DashDriver,
) -> None:
    browser = report_browser
    browser.set("analysis-from", "date", "2024-02-01")
    browser.set("analysis-through", "date", "2024-05-31")
    browser.call("analysis-filters.data", "analysis-apply")
    browser.call(
        "analysis-currency.options",
        "analysis-filters",
        "data",
        browser.get("analysis-filters", "data"),
    )
    assert browser.get("analysis-month", "options") == ["2024-05", "2024-04", "2024-03", "2024-02"]
    assert browser.get("analysis-month", "value") == "2024-05"
    assert browser.get("overview-month", "value") == "2024-08"
    browser.set("analysis-search", "value", "Missing")
    browser.call("analysis-filters.data", "analysis-apply")
    browser.refresh()
    assert browser.get("analysis-currency", "value") is None
    assert "No matching history" in str(browser.get("analysis-report", "children"))
