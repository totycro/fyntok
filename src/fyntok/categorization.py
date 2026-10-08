"""Historical AI workflows with durable progress and atomic, revision-checked application."""
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import logging
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import AIClient, AIRequestError
from .manual import _active, _assign, categories, save_category
from .models import Category, CategorizationRun, ConfirmedExample, RunItem, Transaction
from .queries import Filters, transaction_rows


BATCH_SIZE = 25
logger = logging.getLogger(__name__)


def minimize_description(description: str, account: str = '') -> str:
    """Prefer merchant suffix; redact common identifiers in free-form descriptions."""
    text = description.rsplit('|', 1)[-1]
    if account:
        text = re.sub(r'(?<!\w)' + re.escape(account) + r'(?!\w)', '[ACCOUNT]', text)
    text = re.sub(r'\b[A-Z]{2}\s*\d{2}(?:\s*[A-Z0-9]){10,30}\b', '[ACCOUNT]', text, flags=re.I)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    text = re.sub(r'(?i)\b(?:name|kontoinhaber|beneficiary|reference|referenz|mandat|end.to.end|bic)\s*[:=].*', '', text)
    text = re.sub(r'\d+', '', text)
    text = re.sub(r'\\+', ' / ', text)
    return re.sub(r'\s+', ' ', text).strip(' / ')[:160] or 'Unknown merchant'


def category_context(engine):
    return [{k: c[k] for k in ('id', 'name', 'definition', 'kind', 'revision')} for c in categories(engine)]


def latest_examples(session):
    return [dict(example_id=e.transaction_id, category_id=e.category_id,
                 description=minimize_description(t.description, t.account))
            for e, t in session.execute(select(ConfirmedExample, Transaction)
                .join(Transaction, Transaction.id == ConfirmedExample.transaction_id)
                .order_by(ConfirmedExample.confirmed_at.desc(), ConfirmedExample.transaction_id))]


def relevant_examples(examples, transactions, limit=40):
    words = set(re.findall(r'\w+', ' '.join(t['description'].casefold() for t in transactions)))
    # Stable ranking includes recent fallback examples when no merchant words overlap.
    return sorted(examples, key=lambda e: -len(words & set(re.findall(r'\w+', e['description'].casefold()))))[:limit]


def representative_descriptions(engine, limit=120):
    with Session(engine) as session:
        rows = list(session.scalars(select(Transaction).order_by(Transaction.booking_date, Transaction.id)))
        descriptions = list(dict.fromkeys(minimize_description(t.description, t.account) for t in rows))
    if len(descriptions) <= limit:
        return descriptions
    return [descriptions[i * (len(descriptions) - 1) // (limit - 1)] for i in range(limit)]


def validate_proposals(result):
    if not isinstance(result, dict) or set(result) != {'categories'} or not isinstance(result['categories'], list) or len(result['categories']) > 12:
        raise ValueError('Malformed category proposals.')
    names = set()
    for c in result['categories']:
        if (not isinstance(c, dict) or set(c) != {'name', 'definition', 'kind'}
                or not all(isinstance(c[k], str) for k in c)
                or not c['name'].strip() or not c['definition'].strip()
                or len(c['name']) > 100 or len(c['definition']) > 2000
                or c['kind'] not in {'spending', 'income', 'transfer'}
                or c['name'].strip().casefold() in names):
            raise ValueError('Malformed category proposal.')
        names.add(c['name'].strip().casefold())
    return result['categories']


def propose_categories(engine, client: AIClient, model):
    return validate_proposals(client.propose(model, dict(categories=category_context(engine),
        descriptions=representative_descriptions(engine))))


def approve_proposal(engine, proposal):
    validate_proposals({'categories': [proposal]})
    return save_category(engine, proposal['name'], proposal['definition'], proposal['kind'])


def start_run(engine, model, filters=Filters()):
    if not model:
        raise ValueError('Choose an account-supported model first.')
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        if session.scalar(select(CategorizationRun.id).where(CategorizationRun.status.in_(['pending', 'running']))):
            raise ValueError('An unfinished run already exists. Finish or recover it before starting another.')
        # Read scope and snapshots under the same write lock as edits.
        scope = {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in asdict(filters).items()}
        rows = transaction_rows(engine, filters)
        snapshot = category_context(engine)
        run = CategorizationRun(model=model, scope=scope, category_snapshot=snapshot,
                                example_snapshot=latest_examples(session))
        session.add(run)
        session.flush()
        for row in rows:
            session.add(RunItem(run_id=run.id, transaction_id=row['id'], expected_revision=row['revision'],
                                status='skipped' if row['protected'] or row['source'] == 'manual' else 'pending'))
        return run.id


def validate_assignments(result, targets, snapshot):
    if not isinstance(result, dict) or set(result) != {'assignments'} or not isinstance(result['assignments'], list):
        raise ValueError('Malformed AI assignments.')
    expected = {t['transaction_id'] for t in targets}
    allowed = {c['id'] for c in snapshot}
    actual = []
    for a in result['assignments']:
        if (not isinstance(a, dict) or set(a) != {'transaction_id', 'category_id', 'reason'}
                or not all(isinstance(v, str) for v in a.values()) or a['category_id'] not in allowed
                or len(a['reason']) > 2000):
            raise ValueError('Malformed assignment or unknown category ID.')
        actual.append(a['transaction_id'])
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError('Missing, duplicate, or unknown transaction IDs.')
    return result['assignments']


def run_progress(engine, run_id):
    with Session(engine) as session:
        run = session.get(CategorizationRun, run_id)
        if run is None:
            raise ValueError('Run does not exist.')
        items = list(session.scalars(select(RunItem).where(RunItem.run_id == run_id)))
        return dict(id=run.id, status=run.status, model=run.model, total=len(items),
                    applied=sum(i.status == 'applied' for i in items),
                    skipped=sum(i.status == 'skipped' for i in items),
                    done=sum(i.status in {'applied', 'skipped', 'failed'} for i in items), error=run.error)


@contextmanager
def worker_lock(engine):
    """OS releases this lock on process death; never steal a live worker's run."""
    path = Path(engine.url.database).resolve()
    with open(str(path) + '.ai.lock', 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('An AI worker is still active. Wait for its request to finish before recovery.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def cancel_run(engine, run_id):
    """Persist cancellation immediately; any in-flight result is discarded."""
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        run = session.get(CategorizationRun, run_id)
        if run is None:
            raise ValueError('Run does not exist.')
        if run.status in {'pending', 'running'}:
            run.status, run.error = 'cancelled', None
    return run_progress(engine, run_id)


def resume_run(engine, client, run_id, on_progress=None):
    """Recover abandoned work and execute while holding one uninterrupted lock."""
    with worker_lock(engine):
        with Session(engine) as session, session.begin():
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            run = session.get(CategorizationRun, run_id)
            if run is None or run.status == 'completed':
                raise ValueError('Choose an unfinished run to resume.')
            other = session.scalar(select(CategorizationRun.id).where(
                CategorizationRun.id != run_id, CategorizationRun.status.in_(['pending', 'running'])))
            if other:
                raise ValueError('Cancel or recover the other unfinished run first.')
            for item in session.scalars(select(RunItem).where(RunItem.run_id == run_id,
                    RunItem.status.in_(['running', 'failed', 'validated']))):
                item.status, item.error, item.result = 'pending', None, None
            run.status, run.error = 'pending', None
        return _execute_run(engine, client, run_id, on_progress)


def execute_run(engine, client: AIClient, run_id, on_progress=None):
    with worker_lock(engine):
        return _execute_run(engine, client, run_id, on_progress)


def _execute_run(engine, client: AIClient, run_id, on_progress=None):
    """Execute in a worker thread. Each completed batch commits independently."""
    with Session(engine) as session, session.begin():
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        run = session.get(CategorizationRun, run_id)
        if run is not None and run.status == 'cancelled':
            return run_progress(engine, run_id)
        if run is None or run.status != 'pending':
            raise ValueError('Run is not pending.')
        model, snapshot, examples = run.model, run.category_snapshot, run.example_snapshot
        run.status = 'running'
    try:
        while True:
            with Session(engine) as session, session.begin():
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
                if session.get(CategorizationRun, run_id).status == 'cancelled':
                    break
                items = list(session.scalars(select(RunItem).where(RunItem.run_id == run_id, RunItem.status == 'pending')
                    .order_by(RunItem.transaction_id).limit(BATCH_SIZE)))
                if not items:
                    session.get(CategorizationRun, run_id).status = 'completed'
                    break
                targets = []
                for item in items:
                    t = session.get(Transaction, item.transaction_id)
                    targets.append(dict(transaction_id=t.id, description=minimize_description(t.description, t.account)))
                    item.status, item.attempts = 'running', item.attempts + 1
            payload = dict(categories=[{k: v for k, v in c.items() if k != 'revision'} for c in snapshot],
                           confirmed_examples=relevant_examples(examples, targets), transactions=targets)
            assignments = validate_assignments(client.classify(model, payload), targets, snapshot)
            with Session(engine) as session, session.begin():
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
                if session.get(CategorizationRun, run_id).status == 'cancelled':
                    for item in session.scalars(select(RunItem).where(RunItem.run_id == run_id, RunItem.status == 'running')):
                        item.status = 'pending'
                    break
                current_categories = [dict(id=c.id, name=c.name, definition=c.definition, kind=c.kind, revision=c.revision)
                    for c in session.scalars(select(Category).where(Category.archived.is_(False)).order_by(Category.name))]
                category_changed = current_categories != snapshot
                current_examples = latest_examples(session)
                examples_changed = current_examples != examples
                for result in assignments:
                    item = session.get(RunItem, (run_id, result['transaction_id']))
                    t = session.get(Transaction, item.transaction_id)
                    active = _active(session, t.id)
                    item.result = result
                    if category_changed or examples_changed or t.revision != item.expected_revision or (active and (active.protected or active.source == 'manual')):
                        item.status = 'skipped'
                        item.error = 'Categories, confirmed examples, or transaction changed during the run.'
                        continue
                    assignment = _assign(session, t, result['category_id'], 'ai', result['reason'])
                    assignment.run_id = run_id
                    item.applied_assignment_id, item.status = assignment.id, 'applied'
            if on_progress:
                on_progress(run_progress(engine, run_id))
    except Exception as exc:
        logger.exception('Categorization run failed (%s)', type(exc).__name__)
        # No raw transport errors or response bodies are stored: they may contain private data.
        with Session(engine) as session, session.begin():
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            run = session.get(CategorizationRun, run_id)
            if run.status != 'cancelled':
                detail = str(exc) if isinstance(exc, AIRequestError) else f'AI request failed ({type(exc).__name__}). Check sign-in, model, and plan limits.'
                run.status, run.error = 'failed', detail + ' Completed work is retained; resume when access is available.'
            for item in session.scalars(select(RunItem).where(RunItem.run_id == run_id, RunItem.status == 'running')):
                item.status, item.error = ('pending' if run.status == 'cancelled' else 'failed'), run.error
    result = run_progress(engine, run_id)
    if on_progress:
        on_progress(result)
    return result
