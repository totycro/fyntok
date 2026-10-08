"""Explicit opt-in paired accuracy evaluation; no transaction database is opened."""
import argparse
from collections import defaultdict
import json
from pathlib import Path

from .categorization import relevant_examples, validate_assignments


def evaluate_sample(client, model, sample):
    if not isinstance(sample.get('reviewer'), str) or not sample['reviewer'].strip():
        raise ValueError('Record a human reviewer and confirm expected labels before evaluation.')
    categories = sample['categories']
    allowed = {c['id'] for c in categories}
    cases = sample['cases']
    if not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Evaluation requires distinct held-out cases.')
    if any(c['expected_category_id'] not in allowed for c in cases):
        raise ValueError('Unknown expected category.')
    examples = sample['examples']
    if any(e['category_id'] not in allowed for e in examples):
        raise ValueError('Unknown example category.')
    if {e['example_id'] for e in examples} & {c['id'] for c in cases}:
        raise ValueError('Evaluation cases must be held out from confirmed examples.')
    results = []
    for case in cases:
        targets = [dict(transaction_id=case['id'], description=case['description'])]
        selected = relevant_examples(examples, targets)
        predictions = {}
        for condition, guidance in [('baseline', []), ('examples', selected)]:
            output = validate_assignments(client.classify(model, dict(categories=categories,
                confirmed_examples=guidance, transactions=targets)), targets, categories)[0]
            predictions[condition] = output
        results.append(dict(case=case, predictions=predictions,
            retrieved_example_ids=[e['example_id'] for e in selected],
            relevant_example_retrieved=case.get('relevant_example_id') in {e['example_id'] for e in selected}
                if case.get('relevant_example_id') else None))
    groups = defaultdict(lambda: dict(total=0, baseline_correct=0, examples_correct=0))
    for result in results:
        for group in ('all', result['case']['group']):
            totals = groups[group]
            totals['total'] += 1
            for condition in ('baseline', 'examples'):
                totals[condition + '_correct'] += result['predictions'][condition]['category_id'] == result['case']['expected_category_id']
    corrections = [r for r in results if r['case']['group'] == 'correction']
    return dict(model=model, reviewer=sample['reviewer'], groups=dict(groups), results=results,
        correction_baseline_disagreements=sum(r['predictions']['baseline']['category_id'] != r['case']['expected_category_id'] for r in corrections),
        corrections_fixed=sum(r['predictions']['baseline']['category_id'] != r['case']['expected_category_id'] and
            r['predictions']['examples']['category_id'] == r['case']['expected_category_id'] for r in corrections))


def main():
    parser = argparse.ArgumentParser(description='Send a human-reviewed synthetic sample through your ChatGPT plan; save paired model results.')
    parser.add_argument('--sample', type=Path, default=Path('evaluations/sample.json'))
    parser.add_argument('--model', required=True, help='An account-supported model from Load models')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from .ai import ResponsesClient
    from .auth import ChatGPTSession
    sample = json.loads(args.sample.read_text())
    report = evaluate_sample(ResponsesClient(ChatGPTSession()), args.model, sample)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
