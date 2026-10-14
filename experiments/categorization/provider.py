"""Promptfoo entrypoint; run with the root uv-managed Python interpreter."""

from typing import Any

from fyntok.experimentation import get_assert as score_output
from fyntok.experimentation import provider_response


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return provider_response(prompt, options, context)


def get_assert(output: str, context: dict[str, Any]) -> dict[str, Any]:
    return score_output(output, context)
