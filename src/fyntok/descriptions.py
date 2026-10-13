"""Shared parsing of bank descriptions for merchant grouping and AI inputs."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedDescription:
    merchant_name: str
    minimized_description: str


def _redact(text: str, account: str) -> str:
    # Redact emails first so an adjacent IBAN cannot absorb the email's local part.
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    if account:
        text = re.sub(r"(?<!\w)" + re.escape(account) + r"(?!\w)", "[ACCOUNT]", text)
    # Match printed, space-grouped IBANs to their country length rather than
    # greedily consuming whitespace and the merchant's next word.
    for country, length in {"AT": 20, "DE": 22, "FR": 27, "GB": 22}.items():
        text = re.sub(
            rf"\b{country}\s*\d{{2}}(?:\s*[A-Z0-9]){{{length - 4}}}(?![A-Z0-9])",
            "[ACCOUNT]",
            text,
            flags=re.I,
        )
    text = re.sub(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", "[ACCOUNT]", text, flags=re.I)
    text = re.sub(
        r"(?i)\b(?:name|kontoinhaber|beneficiary|reference|referenz|mandat|end.to.end|bic)"
        r"\s*[:=].*",
        "",
        text,
    )
    # A leading BIC is bank routing metadata when paired with an account.
    text = re.sub(r"^[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\s+(?=\[ACCOUNT\])", "", text)
    return re.sub(r"\b[A-Z]{4}(?:AT|DE|FR|GB)[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b", "", text)


def _clean_merchant(text: str) -> str:
    text = re.sub(r"(?i)^SUMUP\s*\*\s*", "", text)
    text = re.sub(r"(?i)-FIL\.\s*[A-Z]\d+\b", "", text)
    text = re.sub(r"(?i)\s+DANKT(?:\s+\d+)?$", "", text)
    # Standalone numbers are branch/reference metadata; brand digits survive.
    text = re.sub(r"(?<!\S)\d+(?!\S)", "", text)
    return re.sub(r"\s+", " ", text).strip(" /,")


def _select_merchant(description: str, account: str) -> str:
    segments = [re.sub(r"\s+", " ", part).strip() for part in description.split("|")]
    if statement := re.fullmatch(r"(.+ kreditkarte \S+) \d{6} MC/\d+", segments[0]):
        return _redact(segments[0][: statement.end(1)], account)
    fallback = ""
    for segment in reversed(segments):
        if not segment or segment.isdigit() or re.match(r"(?i)^SPESEN\s*:", segment):
            continue
        if re.fullmatch(r"(?i)(?:Bezahlung Karte\s+)?(?:MC|OG|FE)/\d+", segment):
            continue
        redacted = _redact(segment, account)
        meaningful = re.sub(r"\[(?:ACCOUNT|EMAIL)\]", "", redacted)
        if re.search(r"[^\W\d_]", meaningful):
            return redacted
        if not fallback and re.search(r"\[(?:ACCOUNT|EMAIL)\]", redacted):
            fallback = redacted
    return fallback


def parse_description(description: str, account: str = "") -> ParsedDescription:
    """Return a clean merchant name and a private-data-minimized description with city context."""
    selected = _select_merchant(description, account)
    merchant, *locations = re.split(r"\\+", selected)
    minimized = _clean_merchant(merchant)
    name = _clean_merchant(re.sub(r"\[(?:ACCOUNT|EMAIL)\]", "", merchant))
    if locations and (city := _clean_merchant(locations[0])):
        minimized = f"{minimized} / {city}" if minimized else city
    return ParsedDescription(name or "Unknown", minimized[:160] or "Unknown merchant")
