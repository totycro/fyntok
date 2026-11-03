"""Experiment-local payload shaping; shared scoring and transport stay unchanged."""

from typing import Any

from fyntok.experimentation import provider_response


def nest_examples(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "categories": [
            {
                **category,
                "confirmed_examples": [
                    {k: v for k, v in example.items() if k != "category_name"}
                    for example in payload["confirmed_examples"]
                    if example["category_name"] == category["name"]
                ],
            }
            for category in payload["categories"]
        ],
        "transactions": payload["transactions"],
    }


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    variant = options["config"]["variant"]
    layout = variant["example_layout"]
    if layout not in {"flat", "nested"} or variant["category_format"] != "name-only-v1":
        raise ValueError("This experiment requires flat/nested layouts and name-only-v1.")
    return provider_response(
        prompt, options, context, transform=nest_examples if layout == "nested" else None
    )
