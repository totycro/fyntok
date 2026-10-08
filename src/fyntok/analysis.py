"""Financial sums use SQLite integers, grouped by currency (never mixed)."""
from datetime import date
from typing import Literal

from sqlalchemy import and_, case, func, select
from sqlalchemy.engine import Engine

from .models import Assignment, Category, Transaction


def aggregate(engine: Engine, *, by: Literal['monthly', 'trends', 'merchant'] = 'monthly',
              start: date | None = None, end: date | None = None,
              category_id: str | None = None, merchant: str | None = None) -> list[dict]:
    """Monthly category totals are also the category trend series.

    Unassigned rows stay uncategorized regardless of sign. Assigned transfer
    categories take precedence; other categorized credits count as income.
    net_cents is signed; outgoing_cents and incoming_cents are positive magnitudes.
    """
    if by not in {'monthly', 'trends', 'merchant'}:
        raise ValueError(f"Unknown grouping: {by}")
    t, a, c = Transaction, Assignment, Category
    category = func.coalesce(c.id, 'uncategorized').label('category_id')
    category_name = func.coalesce(c.name, 'Uncategorized').label('category_name')
    bucket = case(
        (func.coalesce(c.kind, 'uncategorized') == 'uncategorized', 'uncategorized'),
        (c.kind == 'transfer', 'internal_transfer'),
        (c.kind == 'income', 'income'),
        (t.amount_cents > 0, 'income'),
        else_='spending',
    ).label('bucket')
    dimensions = ([t.merchant.label('merchant')] if by == 'merchant' else
                  [func.strftime('%Y-%m', t.booking_date).label('month')])
    dimensions += [t.currency, category, category_name, bucket]
    query = select(*dimensions, func.count(t.id).label('transaction_count'),
                   func.sum(t.amount_cents).label('net_cents'),
                   func.sum(case((t.amount_cents < 0, -t.amount_cents), else_=0)).label('outgoing_cents'),
                   func.sum(case((t.amount_cents > 0, t.amount_cents), else_=0)).label('incoming_cents')
                   ).select_from(t).outerjoin(a, and_(a.transaction_id == t.id, a.active.is_(True)))\
                    .outerjoin(c, c.id == a.category_id)
    if start is not None:
        query = query.where(t.booking_date >= start)
    if end is not None:
        query = query.where(t.booking_date <= end)
    if category_id is not None:
        query = query.where(category == category_id)
    if merchant is not None:
        query = query.where(t.merchant == merchant)
    query = query.group_by(*dimensions).order_by(*dimensions)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]
