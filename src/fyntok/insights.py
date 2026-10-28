"""Dashboard calculations built on the CLI's integer-cent SQL aggregates.

Observed transaction dates describe available history, never proof of import completeness.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine

from .analysis import aggregate
from .models import Assignment, Category, Transaction
from .queries import Filters


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("Start date must be before the end date.")


def month_start(day: date, offset: int = 0) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, month + 1, 1)


def month_period(day: date) -> Period:
    return Period(month_start(day), month_start(day, 1) - timedelta(days=1))


def months_in(period: Period) -> list[str]:
    result = []
    cursor = month_start(period.start)
    while cursor <= period.end:
        result.append(cursor.strftime("%Y-%m"))
        cursor = month_start(cursor, 1)
    return result


def default_overview_month(period: Period, today: date) -> str:
    """Prefer the latest completed month, falling back to the latest available month."""
    available = months_in(period)
    current_month = month_start(today).isoformat()[:7]
    completed = [month for month in available if month < current_month]
    return completed[-1] if completed else available[-1]


def period_label(period: Period) -> str:
    """Describe full calendar years/months without hiding partial date ranges."""
    if period == Period(date(period.start.year, 1, 1), date(period.start.year, 12, 31)):
        return str(period.start.year)
    if period == month_period(period.start):
        return period.start.strftime("%B %Y")
    return f"{period.start} through {period.end}"


def comparison_period(period: Period, *, year_ago: bool = False) -> Period:
    def previous_year(day: date) -> date:
        return day.replace(
            year=day.year - 1, day=min(day.day, calendar.monthrange(day.year - 1, day.month)[1])
        )

    if year_ago:
        return Period(previous_year(period.start), previous_year(period.end))
    if period.start.day == 1 and period.end == month_period(period.end).end:
        count = (
            (period.end.year - period.start.year) * 12 + period.end.month - period.start.month + 1
        )
        return Period(month_start(period.start, -count), period.start - timedelta(days=1))
    end = period.start - timedelta(days=1)
    return Period(end - (period.end - period.start), end)


def currency_options(engine: Engine, filters: Filters) -> list[str]:
    t, a, c = Transaction, Assignment, Category
    query = (
        select(t.currency)
        .outerjoin(a, and_(a.transaction_id == t.id, a.active.is_(True)))
        .outerjoin(c, c.id == a.category_id)
        .group_by(t.currency)
        .order_by(func.count(t.id).desc(), t.currency)
    )
    with engine.connect() as connection:
        return list(connection.scalars(replace(filters, currency=None).apply(query)))


def observed_history(engine: Engine, currency: str) -> dict[str, tuple[date, date]]:
    """Currency-wide activity distinguishes no category spending from absent history."""
    t = Transaction
    query = (
        select(
            func.strftime("%Y-%m", t.booking_date),
            func.min(t.booking_date),
            func.max(t.booking_date),
        )
        .where(t.currency == currency)
        .group_by(func.strftime("%Y-%m", t.booking_date))
        .order_by(func.strftime("%Y-%m", t.booking_date))
    )
    with engine.connect() as connection:
        return {month: (first, last) for month, first, last in connection.execute(query)}


def totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    result = {
        "spending_cents": 0,
        "income_cents": 0,
        "net_cents": 0,
        "outgoing_cents": 0,
        "incoming_cents": 0,
        "uncategorized_outgoing_cents": 0,
        "uncategorized_incoming_cents": 0,
    }
    for row in rows:
        for field in ("net_cents", "outgoing_cents", "incoming_cents"):
            result[field] += row[field]
        result["spending_cents"] += row["outgoing_cents"]
        if row["bucket"] == "income":
            result["income_cents"] += row["incoming_cents"]
        elif row["bucket"] == "uncategorized":
            result["uncategorized_outgoing_cents"] += row["outgoing_cents"]
            result["uncategorized_incoming_cents"] += row["incoming_cents"]
    return result


def rounded_mean(values: list[int]) -> int | None:
    return (sum(values) * 2 + len(values)) // (len(values) * 2) if values else None


def rounded_median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid] + 1) // 2


def spending_groups(rows: list[dict[str, Any]], *, merchant: bool = False) -> list[dict[str, Any]]:
    """Group every non-transfer debit, regardless of its category or assignment."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row["outgoing_cents"]:
            continue
        key = row["merchant"] if merchant else row["category_id"]
        group = grouped.setdefault(
            key,
            {
                "key": key,
                "name": row["merchant"] if merchant else row["category_name"],
                "spending_cents": 0,
            },
        )
        group["spending_cents"] += row["outgoing_cents"]
    total = sum(group["spending_cents"] for group in grouped.values())
    return sorted(
        [
            dict(group, share=100 * group["spending_cents"] / total if total else 0)
            for group in grouped.values()
        ],
        key=lambda group: (-group["spending_cents"], group["name"]),
    )


def changes(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before = {row["key"]: row for row in previous}
    after = {row["key"]: row for row in current}
    result = []
    for key in before.keys() | after.keys():
        prior = before.get(key, {}).get("spending_cents", 0)
        amount = after.get(key, {}).get("spending_cents", 0)
        result.append(
            {
                "key": key,
                "name": (after.get(key) or before[key])["name"],
                "spending_cents": amount,
                "previous_cents": prior,
                "change_cents": amount - prior,
                "change_percent": 100 * (amount - prior) / prior if prior else None,
            }
        )
    return sorted(result, key=lambda row: (-abs(row["change_cents"]), row["name"]))


@dataclass(frozen=True)
class Report:
    filters: Filters
    period: Period
    comparison: Period
    history: dict[str, tuple[date, date]]
    totals: dict[str, int]
    previous: dict[str, int]
    comparison_observed: bool
    series: list[dict[str, Any]]
    categories: list[dict[str, Any]]
    merchants: list[dict[str, Any]]
    category_changes: list[dict[str, Any]]
    merchant_changes: list[dict[str, Any]]
    eligible_months: list[str]
    average_cents: int | None
    median_cents: int | None
    above_baseline: list[dict[str, Any]]
    baseline_months: list[str]


def build_report(
    engine: Engine,
    filters: Filters,
    period: Period,
    *,
    year_ago: bool = False,
    today: date | None = None,
) -> Report:
    if filters.currency is None:
        raise ValueError("Choose one currency for analysis.")
    today = today or date.today()
    current_filters = replace(filters, start=period.start, end=period.end)
    previous_period = comparison_period(period, year_ago=year_ago)
    rows = aggregate(engine, filters=current_filters)
    merchants = aggregate(engine, by="merchant", filters=current_filters)
    previous_filters = replace(
        current_filters, start=previous_period.start, end=previous_period.end
    )
    previous_rows = aggregate(engine, filters=previous_filters)
    previous_merchants = aggregate(engine, by="merchant", filters=previous_filters)
    history = observed_history(engine, filters.currency)
    first = min((span[0] for span in history.values()), default=None)
    last = max((span[1] for span in history.values()), default=None)

    def eligible(month: str) -> bool:
        bounds = month_period(date.fromisoformat(f"{month}-01"))
        return bool(
            first
            and last
            and month in history
            and first <= bounds.start
            and bounds.end <= min(last, today)
            and period.start <= bounds.start
            and bounds.end <= period.end
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["month"]].append(row)
    series: list[dict[str, Any]] = [
        dict(month=month, observed=month in history, **totals(grouped[month]))
        for month in months_in(period)
    ]
    for row in series:
        row["typical"] = eligible(row["month"])
    eligible_months = [row["month"] for row in series if row["typical"]]
    values = [row["spending_cents"] for row in series if row["typical"]]
    category_rows = spending_groups(rows)
    for category in category_rows:
        monthly = [
            sum(
                row["outgoing_cents"]
                for row in grouped[month]
                if row["category_id"] == category["key"]
            )
            for month in eligible_months
        ]
        category["average_cents"] = rounded_mean(monthly)
        category["median_cents"] = rounded_median(monthly)
        category["monthly_cents"] = {
            month: sum(
                row["outgoing_cents"]
                for row in grouped[month]
                if row["category_id"] == category["key"]
            )
            if month in history
            else None
            for month in months_in(period)
        }

    baseline_period = Period(
        month_start(period.start, -6), month_start(period.start) - timedelta(days=1)
    )
    baseline_months = [
        month
        for month in months_in(baseline_period)
        if month in history
        and first
        and last
        and first <= date.fromisoformat(f"{month}-01")
        and month_period(date.fromisoformat(f"{month}-01")).end <= min(last, today)
    ]
    above_baseline = []
    if len(baseline_months) >= 3 and period == month_period(period.start):
        baseline_rows = aggregate(
            engine,
            filters=replace(current_filters, start=baseline_period.start, end=baseline_period.end),
        )
        baseline_groups = spending_groups(
            [row for row in baseline_rows if row["month"] in baseline_months]
        )
        averages = {
            row["key"]: (row["spending_cents"] * 2 + len(baseline_months))
            // (2 * len(baseline_months))
            for row in baseline_groups
        }
        for row in category_rows:
            average = averages.get(row["key"]) or 0
            if row["spending_cents"] > average:
                above_baseline.append(
                    dict(row, baseline_cents=average, above_cents=row["spending_cents"] - average)
                )
        above_baseline.sort(key=lambda row: (-row["above_cents"], row["name"]))

    with engine.connect() as connection:
        comparison_observed = bool(
            connection.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(
                    Transaction.currency == filters.currency,
                    Transaction.booking_date >= previous_period.start,
                    Transaction.booking_date <= previous_period.end,
                )
            )
        )
    return Report(
        filters=current_filters,
        period=period,
        comparison=previous_period,
        history=history,
        totals=totals(rows),
        previous=totals(previous_rows),
        comparison_observed=comparison_observed,
        series=series,
        categories=category_rows,
        merchants=spending_groups(merchants, merchant=True),
        category_changes=changes(category_rows, spending_groups(previous_rows)),
        merchant_changes=changes(
            spending_groups(merchants, merchant=True),
            spending_groups(previous_merchants, merchant=True),
        ),
        eligible_months=eligible_months,
        average_cents=rounded_mean(values),
        median_cents=rounded_median(values),
        above_baseline=above_baseline,
        baseline_months=baseline_months,
    )
