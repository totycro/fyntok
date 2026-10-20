"""Atomic manual workflows. Archived categories retain historical references."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .database import UNCATEGORIZED_ID
from .models import Assignment, Category, ConfirmedExample, Transaction, now


class EditConflict(ValueError):
    pass


def _category(session: Session, category_id: str | None) -> Category:
    category = (
        session.get(Category, category_id)
        if category_id is not None and category_id != ""
        else None
    )
    if category is None or category.archived:
        raise ValueError("Category no longer exists. Refresh and choose another category.")
    return category


def categories(engine: Engine) -> list[dict[str, Any]]:
    with Session(engine) as session:
        return [
            {
                "id": c.id,
                "name": c.name,
                "definition": c.definition,
                "kind": c.kind,
                "revision": c.revision,
            }
            for c in session.scalars(
                select(Category).where(Category.archived.is_(False)).order_by(Category.name)
            )
        ]


def save_category(
    engine: Engine,
    name: str,
    definition: str,
    kind: str = "spending",
    *,
    category_id: str | None = None,
    expected_revision: int | None = None,
) -> str:
    name = name.strip()
    if not name or name.casefold() == "uncategorized":
        raise ValueError("Choose a nonempty category name other than Uncategorized.")
    if kind not in {"spending", "income", "transfer"}:
        raise ValueError("Invalid category type.")
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if any(
            existing.casefold() == name.casefold()
            for existing in session.scalars(
                select(Category.name).where(Category.id != (category_id or ""))
            )
        ):
            raise ValueError("That category name already exists (possibly in history).")
        if category_id is not None and category_id != "":
            c = _category(session, category_id)
            if c.id == UNCATEGORIZED_ID:
                raise ValueError("Uncategorized is a permanent system category.")
            if expected_revision is not None and c.revision != expected_revision:
                raise EditConflict("Category changed elsewhere. Reopen the editor.")
            c.name, c.definition, c.kind = name, definition.strip(), kind
            c.revision += 1
        else:
            c = Category(name=name, definition=definition.strip(), kind=kind)
            session.add(c)
        session.flush()
        return c.id


def _active(session: Session, tid: str) -> Assignment | None:
    return session.scalar(
        select(Assignment).where(Assignment.transaction_id == tid, Assignment.active.is_(True))
    )


def _assign(
    session: Session,
    transaction: Transaction,
    category_id: str,
    source: str,
    reason: str,
    *,
    protected: bool = False,
    confirmed: bool = False,
    restored_assignment_id: str | None = None,
) -> Assignment:
    old = _active(session, transaction.id)
    if old:
        old.active = False
        session.flush()  # Release the unique active-assignment slot first.
    assignment = Assignment(
        transaction_id=transaction.id,
        category_id=category_id,
        source=source,
        reason=reason,
        protected=protected,
        restored_assignment_id=restored_assignment_id,
        previous_assignment_id=old.id if old else None,
    )
    session.add(assignment)
    session.flush()
    example = session.get(ConfirmedExample, transaction.id)
    if confirmed:
        if example is None:
            example = ConfirmedExample(transaction_id=transaction.id)
            session.add(example)
        example.assignment_id, example.category_id = assignment.id, category_id
        example.confirmed_at = now()
    elif example is not None:
        session.delete(example)
    transaction.revision += 1
    return assignment


def correct_transaction(
    engine: Engine, transaction_id: str, category_id: str, *, expected_revision: int | None = None
) -> str:
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        _category(session, category_id)
        t = session.get(Transaction, transaction_id)
        if t is None:
            raise ValueError("Transaction does not exist.")
        if expected_revision is not None and t.revision != expected_revision:
            raise EditConflict("Transaction changed elsewhere. Reopen its details.")
        return _assign(
            session,
            t,
            category_id,
            "manual",
            "Individual correction",
            protected=True,
            confirmed=True,
        ).id


def delete_example(engine: Engine, transaction_id: str, *, expected_assignment_id: str) -> None:
    """Remove teaching guidance without changing the transaction or assignment history."""
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        example = session.get(ConfirmedExample, transaction_id)
        if example is None or example.assignment_id != expected_assignment_id:
            raise EditConflict("Example changed or was deleted elsewhere. Refresh and try again.")
        session.delete(example)


def undo_assignment(
    engine: Engine, transaction_id: str, *, expected_revision: int | None = None
) -> str:
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        t = session.get(Transaction, transaction_id)
        if t is None:
            raise ValueError("Transaction does not exist.")
        if expected_revision is not None and t.revision != expected_revision:
            raise EditConflict("Transaction changed elsewhere. Reopen its details.")
        current = _active(session, t.id)
        if current is None:
            raise ValueError("No assignment to undo.")
        effective = current
        if current.source == "undo":
            effective = (
                session.get(Assignment, current.restored_assignment_id)
                if (
                    current.restored_assignment_id is not None
                    and current.restored_assignment_id != ""
                )
                else None
            )
        if effective is None:
            raise ValueError("No earlier assignment change to undo.")
        previous = (
            session.get(Assignment, effective.previous_assignment_id)
            if (
                effective.previous_assignment_id is not None
                and effective.previous_assignment_id != ""
            )
            else None
        )
        while previous and previous.source == "undo":
            previous = (
                session.get(Assignment, previous.restored_assignment_id)
                if (
                    previous.restored_assignment_id is not None
                    and previous.restored_assignment_id != ""
                )
                else None
            )
        category_id = previous.category_id if previous else UNCATEGORIZED_ID
        _category(session, category_id)  # Never silently resurrect a deleted category.
        protected = bool(previous and (previous.protected or previous.source == "manual"))
        return _assign(
            session,
            t,
            category_id,
            "undo",
            f"Undo {current.id}; restored {previous.id if previous else 'unassigned'}",
            protected=protected,
            confirmed=protected,
            restored_assignment_id=previous.id if previous else None,
        ).id


def remove_category(
    engine: Engine,
    category_id: str,
    destination_id: str | None = UNCATEGORIZED_ID,
    *,
    expected_revision: int | None = None,
) -> None:
    """Merge/delete: move active assignments and examples, retain immutable history."""
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        c = _category(session, category_id)
        destination = _category(session, destination_id)
        if c.id == UNCATEGORIZED_ID or c.id == destination.id:
            raise ValueError("Choose a different destination; Uncategorized cannot be removed.")
        if expected_revision is not None and c.revision != expected_revision:
            raise EditConflict("Category changed elsewhere. Reopen the editor.")
        for t in session.scalars(
            select(Transaction)
            .join(Assignment, Assignment.transaction_id == Transaction.id)
            .where(Assignment.active.is_(True), Assignment.category_id == c.id)
        ).all():
            old = _active(session, t.id)
            if old is None:
                raise EditConflict("Active assignment changed during category removal.")
            confirmed = session.get(ConfirmedExample, t.id) is not None
            _assign(
                session,
                t,
                destination.id,
                "system",
                f"Category {c.name} merged/deleted into {destination.name}",
                protected=old.protected or old.source == "manual",
                confirmed=confirmed,
            )
        c.archived = True
        c.revision += 1


def assignment_history(engine: Engine, transaction_id: str) -> list[dict[str, Any]]:
    with Session(engine) as session:
        return [
            {
                "id": a.id,
                "category": c.name,
                "archived": c.archived,
                "source": a.source,
                "protected": a.protected,
                "active": a.active,
                "reason": a.reason,
                "created_at": a.created_at.isoformat(),
                "previous_assignment_id": a.previous_assignment_id,
            }
            for a, c in session.execute(
                select(Assignment, Category)
                .join(Category, Assignment.category_id == Category.id)
                .where(Assignment.transaction_id == transaction_id)
                .order_by(Assignment.created_at.desc(), Assignment.id)
            )
        ]
