"""Atomic imports, preserving bytes and duplicate multiplicity within exports."""

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .descriptions import parse_description
from .models import Import, ImportRow, Transaction


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    row_count: int
    inserted_count: int
    duplicate_count: int


def parse_csv(content: bytes) -> list[dict[str, Any]]:
    rows = []
    occurrences = Counter()
    for number, fields in enumerate(
        csv.reader(io.StringIO(content.decode("cp1252"), newline=""), delimiter=";", strict=True), 1
    ):
        try:
            if len(fields) != 6:
                raise ValueError("expected six columns")
            if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+),\d{2}", fields[4]):
                raise ValueError("expected a German amount with two decimal places")
            cents = int(Decimal(fields[4].replace(".", "").replace(",", ".")) * 100)
            if not -(2**63) <= cents < 2**63:
                raise ValueError("amount exceeds SQLite integer range")
            if not re.fullmatch(r"[A-Z]{3}", fields[5]):
                raise ValueError("expected a three-letter currency")
            digest = hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()
            occurrences[digest] += 1
            rows.append(
                {
                    "row_sha256": digest,
                    "occurrence": occurrences[digest],
                    "original_fields": fields,
                    "account": fields[0],
                    "description": fields[1],
                    "merchant": parse_description(fields[1], fields[0]).merchant_name,
                    "booking_date": datetime.strptime(fields[2], "%d.%m.%Y").date(),
                    "value_date": datetime.strptime(fields[3], "%d.%m.%Y").date(),
                    "amount_cents": cents,
                    "currency": fields[5],
                }
            )
        except ValueError as exc:
            raise ValueError(f"Invalid CSV row {number}: {exc}") from exc
    if not rows:
        raise ValueError("CSV contains no transactions")
    return rows


def import_csv(engine: Engine, path: str | Path) -> ImportResult:
    path = Path(path)
    content = path.read_bytes()
    return import_content(engine, content, path.name)


def import_content(engine: Engine, content: bytes, filename: str) -> ImportResult:
    """Import uploaded bytes with the same atomic behavior as a local file."""
    rows = parse_csv(content)  # Validate the entire file before writing anything.
    with Session(engine) as session, session.begin():
        existing = {
            (t.row_sha256, t.occurrence): t.id for t in session.scalars(select(Transaction))
        }
        record = Import(
            filename=Path(filename).name,
            sha256=hashlib.sha256(content).hexdigest(),
            original_bytes=content,
            row_count=len(rows),
            inserted_count=0,
            duplicate_count=0,
        )
        session.add(record)
        session.flush()
        for number, row in enumerate(rows, 1):
            key = (row["row_sha256"], row["occurrence"])
            transaction_id = existing.get(key)
            if transaction_id is None:
                transaction = Transaction(**row)
                session.add(transaction)
                session.flush()
                transaction_id = transaction.id
                existing[key] = transaction_id
                record.inserted_count += 1
            else:
                record.duplicate_count += 1
            session.add(
                ImportRow(import_id=record.id, row_number=number, transaction_id=transaction_id)
            )
        return ImportResult(
            record.id, record.row_count, record.inserted_count, record.duplicate_count
        )
