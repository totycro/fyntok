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
    text = re.sub(r"(?i)\b(?:BG|MC|OG|FE)/\d+\b", "", text).strip()
    text = re.sub(r"(?i)^SUMUP\s*\*\s*", "", text)
    # Recognize merchant_country_reference tokens and their duplicated display
    # prefixes. Require the whole field to match and a digit or hyphen in a
    # nonempty reference so ordinary underscore-separated names survive.
    # Anonymized originals: "ACME_AT_X7Y2-9999-A" and "ACME AT ACME_AT_" -> "ACME".
    if order := re.fullmatch(
        r"(?i)(?P<name>[^\W_]+(?:[.-][^\W_]+)*)"
        r"(?: (?P<country>[A-Z]{2}) (?P=name)_(?P=country)_|_[A-Z]{2}_)"
        r"(?P<reference>[A-Z0-9_-]*)",
        text,
    ):
        reference = order.expand(r"\g<reference>")
        if not reference or re.search(r"[0-9-]", reference):
            text = order.expand(r"\g<name>")
    text = re.sub(r"(?i)-FIL\.\s*[A-Z]\d+\b", "", text)
    text = re.sub(r"(?i)\s+DANKT(?:\s+\d+)?$", "", text)
    # Standalone numbers are branch/reference metadata; brand digits survive.
    text = re.sub(r"(?<!\S)\d+(?!\S)", "", text)
    text = re.sub(r"\s+", " ", text).strip(" /,")
    return text if re.search(r"[^\W\d_]", text) else ""


def _select_merchant(description: str, account: str) -> str:
    segments = [re.sub(r"\s+", " ", part).strip() for part in description.split("|")]
    if statement := re.fullmatch(r"(.+ kreditkarte \S+) \d{6} MC/\d+", segments[0]):
        return _redact(segments[0][: statement.end(1)], account)
    header = _clean_merchant(segments[0]).casefold()
    if header == "auszahlung karte" and any(
        re.match(r"(?i)^(?:AUTOMAT|BANKOMAT|ATM)\b", segment) for segment in segments[1:]
    ):
        return "Bargeldabhebung"
    if header == "sb-eigenerlag" and any(
        re.search(r"(?i)\bSB-Gerät\b", segment) for segment in segments[1:]
    ):
        return "Bargeldeinzahlung"
    if len(segments) == 1 and re.fullmatch(
        r"(?i)(?:AUTOMAT|BANKOMAT|ATM)\s+(?:\S+\s+)*[KD]\d{3}"
        r"\s+\d{2}\.\d{2}\.\s+\d{2}:\d{2}",
        segments[0],
    ):
        return "Bargeldabhebung"
    fallback = ""
    for segment in reversed(segments):
        if not segment or segment.isdigit() or re.match(r"(?i)^SPESEN\s*:", segment):
            continue
        if re.fullmatch(r"(?i)(?:Bezahlung Karte\s+)?(?:MC|OG|FE|BG)/\d+", segment):
            continue
        redacted = _redact(segment, account)
        meaningful = re.sub(r"\[(?:ACCOUNT|EMAIL)\]", "", redacted)
        if _clean_merchant(re.split(r"\\+", meaningful)[0]):
            return redacted
        if not fallback and re.search(r"\[(?:ACCOUNT|EMAIL)\]", redacted):
            fallback = redacted
    return fallback


def parse_description(description: str, account: str = "") -> ParsedDescription:
    """Return a clean merchant name and a private-data-minimized description with city context."""
    selected = _select_merchant(description, account).replace("[ACCOUNT]", "")
    merchant, *locations = re.split(r"\\+", selected)
    minimized = _clean_merchant(merchant)
    name = _clean_merchant(re.sub(r"\[(?:ACCOUNT|EMAIL)\]", "", merchant))
    if locations and (city := _clean_merchant(locations[0])):
        minimized = f"{minimized} / {city}" if minimized else city
    return ParsedDescription(name or "Unknown", minimized[:160] or "Unknown merchant")
