"""Account identities and conservative credit-card repayment reconciliation."""

import re
from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from .models import Account, Assignment, CardRepayment, Category, Transaction

ACCOUNT_KINDS = {"bank": "Bank", "credit_card": "Credit card"}
TREATMENTS = {"auto": "Automatic", "include": "Include in totals", "exclude": "Internal transfer"}


def register_accounts(
    session: Session, rows: list[dict[str, Any]], kind: str | None = None
) -> None:
    """Only infer new identities; subsequent imports cannot silently change their type."""
    if kind is not None and kind not in ACCOUNT_KINDS:
        raise ValueError("Unknown account type")
    descriptions: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        descriptions[row["account"]].append(row["description"].strip().upper())
    for identity, values in descriptions.items():
        account = session.get(Account, identity)
        if account is not None:
            if kind is not None and account.kind != kind:
                raise ValueError(
                    "Account type differs from the saved account; edit it in Data tools"
                )
            continue
        inferred = "credit_card" if "EINZUGSBETRAG" in values else "bank"
        chosen = kind or inferred
        session.add(
            Account(id=identity, label=f"{ACCOUNT_KINDS[chosen]} · {identity[-4:]}", kind=chosen)
        )
    session.flush()


def repayment_role(transaction: Transaction, kind: str) -> str | None:
    description = transaction.description.strip().upper()
    if kind == "credit_card" and transaction.amount_cents > 0 and description == "EINZUGSBETRAG":
        return "credit_card"
    # Require explicit credit-card settlement wording, never an amount-only guess.
    if (
        kind == "bank"
        and transaction.amount_cents < 0
        and re.search(r"\bKREDITKARTE\b", description)
        # Exports may concatenate the numeric card reference and this word.
        and re.search(r"(?:^|[\W\d])ABRECHNUNG\b", description)
    ):
        return "bank"
    return None


def reconcile_repayments(session: Session) -> None:
    """Link only mutually unique candidates within three booking days, in either import order."""
    kinds = {account.id: account.kind for account in session.scalars(select(Account))}
    transactions = list(session.scalars(select(Transaction)))
    records = {r.transaction_id: r for r in session.scalars(select(CardRepayment))}
    for record in records.values():
        record.partner_id = None
    candidates: dict[str, list[Transaction]] = {"bank": [], "credit_card": []}
    for transaction in transactions:
        role = repayment_role(transaction, kinds.get(transaction.account, "bank"))
        record = records.get(transaction.id)
        if role is None:
            # Account type corrections must not erase an explicit human review.
            if record is not None and record.treatment == "auto":
                session.delete(record)
            continue
        if record is None:
            record = CardRepayment(transaction_id=transaction.id, role=role)
            session.add(record)
            records[transaction.id] = record
        record.role = role
        record.partner_id = None
        candidates[role].append(transaction)
    matches: dict[str, list[str]] = defaultdict(list)
    for bank in candidates["bank"]:
        for card in candidates["credit_card"]:
            if (
                bank.account != card.account
                and bank.currency == card.currency
                and bank.amount_cents == -card.amount_cents
                and abs(bank.booking_date - card.booking_date) <= timedelta(days=3)
            ):
                matches[bank.id].append(card.id)
                matches[card.id].append(bank.id)
    for identity, partners in matches.items():
        if len(partners) == 1 and len(matches[partners[0]]) == 1:
            records[identity].partner_id = partners[0]
    session.flush()


def excluded_repayment() -> ColumnElement[bool]:
    """Correlated predicate shared by the table, aggregates, and all report drilldowns.

    Protected non-transfer decisions on either side block automatic exclusion. An
    explicit repayment review can override that without modifying any assignment.
    """
    repayment = aliased(CardRepayment)
    assignment = aliased(Assignment)
    category = aliased(Category)
    conflict = (
        select(assignment.id)
        .join(category, category.id == assignment.category_id)
        .where(
            assignment.transaction_id.in_([repayment.transaction_id, repayment.partner_id]),
            assignment.active.is_(True),
            assignment.protected.is_(True),
            category.kind != "transfer",
        )
        .correlate(repayment)
        .exists()
    )
    return exists(
        select(repayment.transaction_id)
        .where(
            repayment.transaction_id == Transaction.id,
            or_(
                repayment.treatment == "exclude",
                and_(
                    repayment.treatment == "auto",
                    or_(repayment.role == "credit_card", repayment.partner_id.is_not(None)),
                    ~conflict,
                ),
            ),
        )
        .correlate(Transaction)
    )


def account_rows(engine: Engine) -> list[dict[str, str]]:
    with Session(engine) as session:
        return [
            {"id": a.id, "label": a.label, "kind": a.kind}
            for a in session.scalars(select(Account).order_by(Account.label, Account.id))
        ]


def update_account(engine: Engine, identity: str, label: str, kind: str) -> None:
    if kind not in ACCOUNT_KINDS or not label.strip():
        raise ValueError("Choose an account type and a nonempty label")
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        account = session.get(Account, identity)
        if account is None:
            raise ValueError("Account does not exist")
        account.label = label.strip()
        account.kind = kind
        reconcile_repayments(session)


def review_repayment(
    engine: Engine, transaction_id: str, treatment: str, *, expected_revision: int
) -> None:
    if treatment not in TREATMENTS:
        raise ValueError("Unknown repayment treatment")
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        transaction = session.get(Transaction, transaction_id)
        repayment = session.get(CardRepayment, transaction_id)
        if transaction is None or repayment is None:
            raise ValueError("Repayment does not exist")
        if transaction.revision != expected_revision:
            raise ValueError("Transaction changed; reopen it before reviewing")
        repayment.treatment = treatment
        transaction.revision += 1


def repayment_warning(rows: list[dict[str, Any]]) -> str:
    pending = sum(r["repayment_status"] == "Review needed" for r in rows)
    unmatched = sum(
        r["repayment_role"] == "credit_card" and not r["repayment_partner"] for r in rows
    )
    messages = []
    if pending:
        messages.append(
            f"{pending} credit-card repayments need review: missing card detail, "
            "ambiguous matching, or a protected category decision. "
            "They remain in totals unless categorized as transfers. "
            "Review them in Transactions; transaction dates do not prove complete card coverage."
        )
    if unmatched:
        messages.append(f"{unmatched} card repayment credits have no matched bank withdrawal.")
    return " ".join(messages)
