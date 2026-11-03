"""Experiment-local payload shaping; shared scoring and transport stay unchanged."""

from typing import Any

from fyntok.experimentation import provider_response


def flatten_examples(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "categories": [
            {k: v for k, v in category.items() if k != "confirmed_examples"}
            for category in payload["categories"]
        ],
        "confirmed_examples": [
            {**example, "category_name": category["name"]}
            for category in payload["categories"]
            for example in category["confirmed_examples"]
        ],
        "transactions": payload["transactions"],
    }


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    variant = options["config"]["variant"]
    layout = variant["example_layout"]
    if layout not in {"flat", "nested"} or variant["category_format"] != "name-only-nested-v2":
        raise ValueError("This experiment requires flat/nested layouts and name-only-nested-v2.")
    return provider_response(
        prompt, options, context, transform=flatten_examples if layout == "flat" else None
    )
