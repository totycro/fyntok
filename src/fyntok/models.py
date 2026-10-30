# ty: ignore[unsound-assignment]
"""Schema v3. Amounts are signed integer cents; IDs survive category renames."""

# SQLAlchemy infers mapped_column types from Mapped annotations at runtime.
# Its generic Any return does not express that declarative typing contract.

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(primary_key=True)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(unique=True)
    definition: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(default="spending")
    revision: Mapped[int] = mapped_column(default=0)
    archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (CheckConstraint("kind IN ('spending','income','transfer','uncategorized')"),)


class Import(Base):
    __tablename__ = "imports"
    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    filename: Mapped[str]
    sha256: Mapped[str] = mapped_column(index=True)
    original_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    encoding: Mapped[str] = mapped_column(default="cp1252")
    row_count: Mapped[int]
    inserted_count: Mapped[int]
    duplicate_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    row_sha256: Mapped[str]
    occurrence: Mapped[int]
    original_fields: Mapped[list[str]] = mapped_column(JSON)
    account: Mapped[str]
    description: Mapped[str] = mapped_column(Text)
    merchant: Mapped[str] = mapped_column(Text, index=True)
    booking_date: Mapped[date] = mapped_column(Date, index=True)
    value_date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str]
    revision: Mapped[int] = mapped_column(default=0)
    __table_args__ = (
        UniqueConstraint("row_sha256", "occurrence"),
        CheckConstraint("occurrence > 0"),
        CheckConstraint("typeof(amount_cents) = 'integer'"),
    )


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(primary_key=True)
    label: Mapped[str]
    kind: Mapped[str] = mapped_column(default="bank")
    __table_args__ = (CheckConstraint("kind IN ('bank','credit_card')"),)


class CardRepayment(Base):
    """Deterministic settlement metadata, independent of category assignments."""

    __tablename__ = "card_repayments"
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str]
    partner_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    treatment: Mapped[str] = mapped_column(default="auto")
    __table_args__ = (
        CheckConstraint("role IN ('bank','credit_card')"),
        CheckConstraint("treatment IN ('auto','include','exclude')"),
    )


class ImportRow(Base):
    __tablename__ = "import_rows"
    import_id: Mapped[str] = mapped_column(ForeignKey("imports.id"), primary_key=True)
    row_number: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), index=True)


class CategorizationRun(Base):
    __tablename__ = "categorization_runs"
    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(default="pending")
    model: Mapped[str]
    scope: Mapped[dict[str, Any]] = mapped_column(JSON)
    category_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    example_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','cancelled','failed','completed')"),
    )


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"))
    source: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    protected: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("categorization_runs.id"))
    previous_assignment_id: Mapped[str | None]
    restored_assignment_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        CheckConstraint("source IN ('manual','ai','system','undo')"),
        UniqueConstraint("id", "transaction_id"),
        UniqueConstraint("id", "transaction_id", "category_id"),
        ForeignKeyConstraint(
            ["previous_assignment_id", "transaction_id"],
            ["assignments.id", "assignments.transaction_id"],
        ),
        ForeignKeyConstraint(
            ["restored_assignment_id", "transaction_id"],
            ["assignments.id", "assignments.transaction_id"],
        ),
        Index(
            "one_active_assignment", "transaction_id", unique=True, sqlite_where=text("active = 1")
        ),
    )


class ConfirmedExample(Base):
    __tablename__ = "confirmed_examples"
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), primary_key=True)
    assignment_id: Mapped[str]
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["assignment_id", "transaction_id", "category_id"],
            ["assignments.id", "assignments.transaction_id", "assignments.category_id"],
        ),
    )


class RunItem(Base):
    __tablename__ = "run_items"
    run_id: Mapped[str] = mapped_column(ForeignKey("categorization_runs.id"), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), primary_key=True)
    expected_revision: Mapped[int]
    status: Mapped[str] = mapped_column(default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    applied_assignment_id: Mapped[str | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','validated','applied','skipped','failed')"),
        ForeignKeyConstraint(
            ["applied_assignment_id", "transaction_id"],
            ["assignments.id", "assignments.transaction_id"],
        ),
    )
