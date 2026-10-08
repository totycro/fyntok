"""Boundary checks for data minimization, validation, transport, and durable batches."""
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from fyntok.ai import ResponsesClient
from fyntok.categorization import (execute_run, minimize_description, relevant_examples, start_run,
                                   validate_assignments, validate_proposals)
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.models import RunItem


def test_minimization_omits_bank_fields_and_common_identifiers():
    assert minimize_description('account | card 123 01.01.2024 | EXAMPLE GROCERY 543 Berlin', 'account') == 'EXAMPLE GROCERY Berlin'
    assert minimize_description('Pharmacy', 'a') == 'Pharmacy'
    text = minimize_description('DE89370400440532013000 user@example.com account 123456 Reference: secret', 'account')
    assert all(secret not in text for secret in ('DE89370400440532013000', 'user@example.com', '123456', 'secret'))


def test_related_examples_are_ranked_before_recent_unrelated_examples():
    unrelated = [dict(description='Unrelated pharmacy', category_id='care') for _ in range(50)]
    supermarket = dict(description='EXAMPLE GROCERY Berlin', category_id='food')
    selected = relevant_examples(unrelated + [supermarket], [{'description': 'EXAMPLE GROCERY Hamburg'}])
    assert selected[0] == supermarket and len(selected) == 40


@pytest.mark.parametrize('assignments', [[], [{'transaction_id': 'other', 'category_id': 'food', 'reason': 'x'}],
    [{'transaction_id': 'one', 'category_id': 'unknown', 'reason': 'x'}],
    [{'transaction_id': 'one', 'category_id': 'food', 'reason': 4}],
    [{'transaction_id': 'one', 'category_id': 'food', 'reason': 'x'}] * 2])
def test_invalid_assignments_are_rejected_as_a_whole(assignments):
    with pytest.raises(ValueError):
        validate_assignments({'assignments': assignments}, [{'transaction_id': 'one'}], [{'id': 'food'}])


def test_malformed_proposals_are_rejected():
    with pytest.raises(ValueError):
        validate_proposals({'categories': [dict(name='Food', kind='invented', definition='x')]})


@pytest.mark.parametrize('terminal', ['response.completed', 'response.failed', 'response.incomplete', 'interrupted'])
def test_subscription_transport_requires_a_completed_stream(monkeypatch, terminal):
    class Login:
        def active_account(self):
            return 'test-account'

        def access_token(self):
            return 'test-token'

    received = []

    def respond(request):
        received.append(json.loads(request.content))
        assert request.url == 'https://api.openai.com/v1/responses'
        assert request.headers['Authorization'] == 'Bearer test-token'
        event = dict(type=terminal, response={'output': [{'content': [{'type': 'output_text', 'text': '{"categories": []}'}]}]})
        return httpx.Response(200, text='' if terminal == 'interrupted' else 'data: ' + json.dumps(event) + '\n\n')

    factory = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs))
    client = ResponsesClient(Login())
    if terminal == 'response.completed':
        assert client.propose('selected-model', {'descriptions': ['EXAMPLE GROCERY']}) == {'categories': []}
    else:
        with pytest.raises(RuntimeError):
            client.propose('selected-model', {'descriptions': ['EXAMPLE GROCERY']})
    body = received[0]
    assert body['store'] is False and body['stream'] is True
    assert body['model'] == 'selected-model' and isinstance(body['input'], list)
    assert body['text']['format']['strict'] is True


def test_completed_batches_survive_a_later_failure_and_reopening(tmp_path, monkeypatch):
    from fyntok import categorization
    monkeypatch.setattr(categorization, 'BATCH_SIZE', 1)
    path = tmp_path / 'batches.sqlite3'
    engine = open_database(path)
    import_content(engine, b'a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\na;EXAMPLE CARE;02.01.2024;02.01.2024;-20,00;EUR\n', 'bank.csv')

    class Advisor:
        calls = 0

        def classify(self, model, payload):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError('Disconnected')
            return {'assignments': [dict(transaction_id=payload['transactions'][0]['transaction_id'],
                                         category_id='uncategorized', reason='Unclear')]}

    run_id = start_run(engine, 'model')
    result = execute_run(engine, Advisor(), run_id)
    assert result['status'] == 'failed' and result['applied'] == 1
    engine.dispose()
    engine = open_database(path)
    with Session(engine) as session:
        items = list(session.scalars(select(RunItem).where(RunItem.run_id == run_id)))
        applied = next(i for i in items if i.status == 'applied')
        assert applied.applied_assignment_id and applied.result['reason'] == 'Unclear'
    engine.dispose()
