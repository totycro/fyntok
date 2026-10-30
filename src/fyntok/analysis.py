"""Financial sums use SQLite integers, grouped by currency (never mixed)."""

from datetime import date
from typing import Any, Literal

from sqlalchemy import and_, case, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from .accounts import excluded_repayment
from .models import Assignment, Category, Transaction
from .queries import Filters


def aggregate(
    engine: Engine,
    *,
    by: Literal["monthly", "trends", "merchant"] = "monthly",
    start: date | None = None,
    end: date | None = None,
    category_id: str | None = None,
    merchant: str | None = None,
    filters: Filters | None = None,
) -> list[dict[str, Any]]:
    """Monthly category totals are also the category trend series.

    Internal transfers are excluded from all totals and counts. Unassigned rows
    stay uncategorized regardless of sign; other categorized credits count as income.
    net_cents is signed; outgoing_cents and incoming_cents are positive magnitudes.
    """
    if by not in {"monthly", "trends", "merchant"}:
        raise ValueError(f"Unknown grouping: {by}")
    t, a, c = Transaction, Assignment, Category
    category = func.coalesce(c.id, "uncategorized").label("category_id")
    category_name = func.coalesce(c.name, "Uncategorized").label("category_name")
    bucket = case(
        (func.coalesce(c.kind, "uncategorized") == "uncategorized", "uncategorized"),
        (c.kind == "income", "income"),
        (t.amount_cents > 0, "income"),
        else_="spending",
    ).label("bucket")
    dimensions: list[ColumnElement[Any] | InstrumentedAttribute[Any]] = (
        [t.merchant.label("merchant")]
        if by == "merchant"
        else [func.strftime("%Y-%m", t.booking_date).label("month")]
    )
    dimensions.extend([t.currency, category, category_name, bucket])
    query = (
        select(
            *dimensions,
            func.count(t.id).label("transaction_count"),
            func.sum(t.amount_cents).label("net_cents"),
            func.sum(case((t.amount_cents < 0, -t.amount_cents), else_=0)).label("outgoing_cents"),
            func.sum(case((t.amount_cents > 0, t.amount_cents), else_=0)).label("incoming_cents"),
        )
        .select_from(t)
        .outerjoin(a, and_(a.transaction_id == t.id, a.active.is_(True)))
        .outerjoin(c, c.id == a.category_id)
        .where(func.coalesce(c.kind, "uncategorized") != "transfer")
        .where(~excluded_repayment())
    )
    if start is not None:
        query = query.where(t.booking_date >= start)
    if end is not None:
        query = query.where(t.booking_date <= end)
    if category_id is not None:
        query = query.where(category == category_id)
    if merchant is not None:
        query = query.where(t.merchant == merchant)
    if filters is not None:
        query = filters.apply(query)
    query = query.group_by(*dimensions).order_by(*dimensions)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]
