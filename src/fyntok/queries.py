"""Shared SQL filters for tables and financial analysis."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import aliased

from .models import Assignment, Category, Transaction


@dataclass(frozen=True)
class Filters:
    start: date | None = None
    end: date | None = None
    category_id: str | None = None
    merchant: str | None = None
    search: str = ''
    source: str | None = None
    currency: str | None = None

    def validate(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError('Start date must be before the end date.')

    def apply(self, query):
        self.validate()
        t, a, c = Transaction, Assignment, Category
        for condition in [t.booking_date >= self.start if self.start else None,
                          t.booking_date <= self.end if self.end else None,
                          func.coalesce(c.id, 'uncategorized') == self.category_id if self.category_id else None,
                          t.merchant == self.merchant if self.merchant else None,
                          t.description.contains(self.search, autoescape=True) if self.search else None,
                          func.coalesce(a.source, 'unassigned') == self.source if self.source else None,
                          t.currency == self.currency if self.currency else None]:
            if condition is not None:
                query = query.where(condition)
        return query


def transaction_rows(engine: Engine, filters: Filters = Filters()) -> list[dict]:
    t, a, c = Transaction, Assignment, Category
    restored = aliased(Assignment)
    query = select(t.id, t.description, t.merchant, t.booking_date, t.value_date, t.amount_cents,
                   t.currency, t.revision, func.coalesce(c.id, 'uncategorized').label('category_id'),
                   func.coalesce(c.name, 'Uncategorized').label('category'),
                   func.coalesce(a.source, 'unassigned').label('source'),
                   func.coalesce(a.protected, False).label('protected'),
                   func.coalesce(restored.source, a.source, 'unassigned').label('origin_source')).select_from(t)\
        .outerjoin(a, and_(a.transaction_id == t.id, a.active.is_(True))).outerjoin(c, c.id == a.category_id).outerjoin(restored, restored.id == a.restored_assignment_id)
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(filters.apply(query).order_by(t.booking_date.desc(), t.id)).mappings()]
