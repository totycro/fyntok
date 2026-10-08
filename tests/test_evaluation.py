"""Harness verification is distinct from measuring a live model's accuracy."""
import json
from pathlib import Path

import pytest

from fyntok.evaluation import evaluate_sample


def test_paired_evaluation_records_retrieval_and_baseline_disagreement():
    sample = json.loads(Path('evaluations/sample.json').read_text())
    class Scripted:
        calls = []
        def classify(self, model, payload):
            self.calls.append(payload)
            target = payload['transactions'][0]
            expected = next(c['expected_category_id'] for c in sample['cases'] if c['id'] == target['transaction_id'])
            if target['transaction_id'] == 'market' and not payload['confirmed_examples']:
                expected = 'shopping'
            return {'assignments': [dict(transaction_id=target['transaction_id'], category_id=expected, reason='Scripted harness test')]}
    client = Scripted()
    with pytest.raises(ValueError, match='human reviewer'):
        evaluate_sample(client, 'scripted', sample)
    assert not client.calls
    sample['reviewer'] = 'Test fixture reviewer (scripted, not a human accuracy review)'
    report = evaluate_sample(client, 'scripted', sample)
    assert report['groups']['all'] == dict(total=7, baseline_correct=6, examples_correct=7)
    assert report['correction_baseline_disagreements'] == report['corrections_fixed'] == 1
    assert all(r['relevant_example_retrieved'] for r in report['results'] if r['case'].get('relevant_example_id'))
    assert all(len(p['confirmed_examples']) <= 40 for p in client.calls)
    sample['examples'].append(dict(example_id='market', description='Leak', category_id='food'))
    with pytest.raises(ValueError, match='held out'):
        evaluate_sample(client, 'scripted', sample)
