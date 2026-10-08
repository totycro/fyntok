"""Offline checks of response validation and held-out examples."""

import json
import unittest
from pydantic import ValidationError

from experiment import validate_output


class ResponseValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = {"transactions": [{"transaction_id": "target-1"}, {"transaction_id": "target-2"}],
                        "categories": [{"id": "groceries"}]}
        self.examples = [{"example_id": "example-1"}]
        self.assignments = [{"transaction_id": tid, "category_id": "groceries", "reason": "Supermarket",
                             "example_ids_used": ["example-1"]} for tid in ["target-1", "target-2"]]

    def validate(self):
        return validate_output(json.dumps({"assignments": self.assignments}), self.fixture, self.examples)

    def test_complete_response(self):
        self.assertEqual(len(self.validate().assignments), 2)

    def test_missing_target(self):
        self.assignments.pop()
        with self.assertRaises(ValueError):
            self.validate()

    def test_duplicate_target(self):
        self.assignments[1]["transaction_id"] = "target-1"
        with self.assertRaises(ValueError):
            self.validate()

    def test_unknown_category(self):
        self.assignments[0]["category_id"] = "invented"
        with self.assertRaises(ValueError):
            self.validate()

    def test_unknown_example(self):
        self.assignments[0]["example_ids_used"] = ["invented"]
        with self.assertRaises(ValueError):
            self.validate()

    def test_example_attribution_without_examples(self):
        self.examples = []
        with self.assertRaises(ValueError):
            self.validate()

    def test_malformed_output(self):
        with self.assertRaises(ValidationError):
            validate_output("not JSON", self.fixture, self.examples)


if __name__ == "__main__":
    unittest.main()
