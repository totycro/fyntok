"""Shared SQL filters for tables and financial analysis."""

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from .descriptions import parse_description
from .models import Assignment, Category, ConfirmedExample, Transaction


@dataclass(frozen=True)
class Filters:
    start: date | None = None
    end: date | None = None
    category_id: str | None = None
    merchant: str | None = None
    search: str = ""
    source: str | None = None
    currency: str | None = None

    def validate(self) -> None:
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start date must be before the end date.")

    def apply[*Columns](self, query: Select[*Columns]) -> Select[*Columns]:
        self.validate()
        t, a, c = Transaction, Assignment, Category
        for condition in [
            t.booking_date >= self.start if self.start else None,
            t.booking_date <= self.end if self.end else None,
            func.coalesce(c.id, "uncategorized") == self.category_id
            if (self.category_id is not None and self.category_id != "")
            else None,
            t.merchant == self.merchant
            if (self.merchant is not None and self.merchant != "")
            else None,
            t.description.contains(self.search, autoescape=True) if self.search else None,
            func.coalesce(a.source, "unassigned") == self.source
            if (self.source is not None and self.source != "")
            else None,
            t.currency == self.currency
            if (self.currency is not None and self.currency != "")
            else None,
        ]:
            if condition is not None:
                query = query.where(condition)
        return query


DEFAULT_FILTERS = Filters()


def example_rows(engine: Engine) -> list[dict[str, Any]]:
    """List current human-confirmed guidance, independently of transaction filters."""
    t, e, c = Transaction, ConfirmedExample, Category
    query = (
        select(
            t.id,
            t.description,
            t.account,
            t.booking_date,
            t.amount_cents,
            t.currency,
            e.assignment_id,
            e.category_id,
            e.confirmed_at,
            c.name.label("category"),
        )
        .select_from(e)
        .join(t, t.id == e.transaction_id)
        .join(c, c.id == e.category_id)
        .order_by(e.confirmed_at.desc(), t.id)
    )
    with engine.connect() as connection:
        rows: list[dict[str, Any]] = []
        for result in connection.execute(query).mappings():
            row = dict(result)
            row["cleaned_description"] = parse_description(
                row["description"], row.pop("account")
            ).cleaned_description
            rows.append(row)
        return rows


def transaction_rows(engine: Engine, filters: Filters = DEFAULT_FILTERS) -> list[dict[str, Any]]:
    t, a, c = Transaction, Assignment, Category
    restored = aliased(Assignment)
    query = (
        select(
            t.id,
            t.description,
            t.account,
            t.merchant,
            t.booking_date,
            t.value_date,
            t.amount_cents,
            t.currency,
            t.revision,
            func.coalesce(c.id, "uncategorized").label("category_id"),
            func.coalesce(c.name, "Uncategorized").label("category"),
            func.coalesce(a.source, "unassigned").label("source"),
            func.coalesce(a.protected, False).label("protected"),
            func.coalesce(restored.source, a.source, "unassigned").label("origin_source"),
        )
        .select_from(t)
        .outerjoin(a, and_(a.transaction_id == t.id, a.active.is_(True)))
        .outerjoin(c, c.id == a.category_id)
        .outerjoin(restored, restored.id == a.restored_assignment_id)
    )
    with engine.connect() as connection:
        rows: list[dict[str, Any]] = []
        for result in connection.execute(
            filters.apply(query).order_by(t.booking_date.desc(), t.id)
        ).mappings():
            row = dict(result)
            row["cleaned_description"] = parse_description(
                row["description"], row.pop("account")
            ).cleaned_description
            rows.append(row)
        return rows
