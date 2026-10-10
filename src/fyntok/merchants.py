"""Conservative merchant text selection from bank descriptions."""

import re


def merchant_text(description: str) -> str:
    """Skip empty/reference suffixes and recognize credit card statement headers."""
    segments = [re.sub(r"\s+", " ", part).strip() for part in description.split("|")]
    if statement := re.fullmatch(r"(.+ kreditkarte \S+) \d{6} MC/\d+", segments[0]):
        return segments[0][: statement.end(1)]
    return next((part for part in reversed(segments) if part and not part.isdigit()), "")
