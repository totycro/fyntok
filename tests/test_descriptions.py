"""Primary ownership of description rules; all examples are synthetic."""

import pytest

from fyntok.descriptions import ParsedDescription, parse_description


@pytest.mark.parametrize(
    ("description", "merchant_name", "cleaned_description"),
    [
        pytest.param(
            r"Bezahlung Karte MC/000000001|POS 9999 K009 01.01. 12:34|"
            r"BEISPIEL CAFE\\WIEN\9999",
            "BEISPIEL CAFE",
            "BEISPIEL CAFE / WIEN",
            id="card-with-city",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000002|E-COMM K009 01.01. 12:34|"
            r"UZR*BEISPIELTICKET*DEMO\\WIEN\9999",
            "UZR*BEISPIELTICKET*DEMO",
            "UZR*BEISPIELTICKET*DEMO / WIEN",
            id="ecommerce-keeps-unknown-processor",
        ),
        pytest.param(
            "/CNR/999999/DOC/99999999/12.34/20240101|OG/000000005|"
            "TESTATWWXXX AT000000000000000000 Beispiel Lieferdienst GmbH",
            "Beispiel Lieferdienst GmbH",
            "Beispiel Lieferdienst GmbH",
            id="transfer-inline-iban",
        ),
        pytest.param(
            "Beispiel Mobil KdNr: 9999999 OG/000000006|"
            "TESTFRP1XXX FR0000000000000000000000000|Beispiel Firma GmbH",
            "Beispiel Firma GmbH",
            "Beispiel Firma GmbH",
            id="transfer-separate-foreign-iban",
        ),
        pytest.param(
            "Abbuchung Onlinebanking FE/000000001|Beispiel Sonnenschutz und|0000000001",
            "Beispiel Sonnenschutz und",
            "Beispiel Sonnenschutz und",
            id="online-transfer-trailing-reference",
        ),
    ],
)
def test_card_and_transfer_formats(
    description: str, merchant_name: str, cleaned_description: str
) -> None:
    assert parse_description(description) == ParsedDescription(merchant_name, cleaned_description)


@pytest.mark.parametrize(
    ("description", "account", "merchant_name", "cleaned_description"),
    [
        pytest.param(
            "Beispielbank kreditkarte TEST           200001       MC/000000001|"
            "Beispielbank kreditkarte TEST 200001Abrechnung Nr. 000000001|10",
            "",
            "Beispielbank kreditkarte TEST",
            "Beispielbank kreditkarte TEST",
            id="statement-header-overrides-later-fields",
        ),
        pytest.param(
            "Beispielbank kreditkarte account 200001 MC/000000001|Abrechnung Nr. 000000001",
            "account",
            "Beispielbank kreditkarte",
            "Beispielbank kreditkarte",
            id="statement-account-redaction",
        ),
        pytest.param(
            "Beispielbank kreditkarte user@example.com 200001 MC/000000001|10",
            "",
            "Beispielbank kreditkarte",
            "Beispielbank kreditkarte [EMAIL]",
            id="statement-email-redaction",
        ),
        pytest.param(
            "Beispielbank kreditkarte TEST shop | Shop 24",
            "",
            "Shop",
            "Shop",
            id="near-match-is-not-a-statement",
        ),
    ],
)
def test_credit_card_statement_selection(
    description: str, account: str, merchant_name: str, cleaned_description: str
) -> None:
    assert parse_description(description, account) == ParsedDescription(
        merchant_name, cleaned_description
    )


@pytest.mark.parametrize(
    ("description", "merchant_name", "cleaned_description"),
    [
        pytest.param("Bank | Shop 24 | 10 | 11 | ", "Shop", "Shop", id="numeric-metadata"),
        pytest.param(
            "Bank |  Café   Beispiel  | 12 | ",
            "Café Beispiel",
            "Café Beispiel",
            id="whitespace-normalization",
        ),
        pytest.param("Bank | 7-Shop", "7-Shop", "7-Shop", id="leading-brand-digit"),
        pytest.param(
            r"Bank | EXAMPLE24 GMBH\\DRESDEN\01237|SPESEN: 0,00",
            "EXAMPLE24 GMBH",
            "EXAMPLE24 GMBH / DRESDEN",
            id="embedded-brand-digits-and-fees",
        ),
        pytest.param(
            r"Bank | EXAMPLE* SOFTWARE SUBSCR\\DUBLIN|10|",
            "EXAMPLE* SOFTWARE SUBSCR",
            "EXAMPLE* SOFTWARE SUBSCR / DUBLIN",
            id="city-without-postal-code",
        ),
        pytest.param(
            r"Bank | EXAMPLE MARKET DANKT 4624\\WIEN\1020",
            "EXAMPLE MARKET",
            "EXAMPLE MARKET / WIEN",
            id="branch-number-and-thanks",
        ),
        pytest.param(
            r"Bank | EXAMPLE CARE-FIL. A112\\WIEN\1020 W",
            "EXAMPLE CARE",
            "EXAMPLE CARE / WIEN",
            id="alphanumeric-branch-and-location-suffix",
        ),
        pytest.param(
            r"Bank | SUMUP *EXAMPLE CLINIC\\WIEN\1",
            "EXAMPLE CLINIC",
            "EXAMPLE CLINIC / WIEN",
            id="known-payment-processor",
        ),
        pytest.param("Shop | OG/000000001", "Shop", "Shop", id="trailing-bank-reference"),
        pytest.param("ACME", "ACME", "ACME", id="plain-name"),
        pytest.param(
            "Shop " + "x" * 200,
            "Shop " + "x" * 200,
            "Shop " + "x" * 155,
            id="only-cleaned-description-is-truncated",
        ),
    ],
)
def test_merchant_and_location_cleanup(
    description: str, merchant_name: str, cleaned_description: str
) -> None:
    assert parse_description(description) == ParsedDescription(merchant_name, cleaned_description)


@pytest.mark.parametrize(
    ("description", "account", "merchant_name", "cleaned_description"),
    [
        pytest.param("Pharmacy", "a", "Pharmacy", "Pharmacy", id="account-word-boundaries"),
        pytest.param(
            "account | card 123 01.01.2024 | EXAMPLE GROCERY 543 Berlin",
            "account",
            "EXAMPLE GROCERY Berlin",
            "EXAMPLE GROCERY Berlin",
            id="account-and-card-metadata",
        ),
        pytest.param(
            "Bank | AT000000000000000000 / Beispiel Sonnenschutz und | 0000000001",
            "",
            "Beispiel Sonnenschutz und",
            "Beispiel Sonnenschutz und",
            id="inline-iban",
        ),
        pytest.param(
            "Bank | account user@example.com Shop Reference: secret | 10",
            "account",
            "Shop",
            "[EMAIL] Shop",
            id="account-email-and-private-reference",
        ),
        pytest.param(
            "DE00000000000000000000 user@example.com account 123456 Reference: secret",
            "account",
            "Unknown",
            "[EMAIL]",
            id="email-next-to-iban-without-merchant",
        ),
        pytest.param(
            "AT00 0000 0000 0000 0000 DEMO",
            "",
            "DEMO",
            "DEMO",
            id="grouped-numeric-iban",
        ),
        pytest.param(
            "GB00 TEST 0000 0000 0000 00 Example",
            "",
            "Example",
            "Example",
            id="grouped-alphanumeric-iban",
        ),
        pytest.param("Shop | Reference: secret", "", "Shop", "Shop", id="private-only-field"),
        pytest.param("Shop | TESTATWWXXX", "", "Shop", "Shop", id="bic-only-field"),
        pytest.param("TESTATWWXXX Example", "", "Example", "Example", id="inline-bic"),
        pytest.param(
            r"Shop\\WIEN AT000000000000000000\9999",
            "",
            "Shop",
            "Shop / WIEN",
            id="iban-in-location",
        ),
        pytest.param(
            "MC/000000002|private-account Café",
            "private-account",
            "Café",
            "Café",
            id="account-with-merchant",
        ),
    ],
)
def test_private_data_redaction(
    description: str, account: str, merchant_name: str, cleaned_description: str
) -> None:
    assert parse_description(description, account) == ParsedDescription(
        merchant_name, cleaned_description
    )


@pytest.mark.parametrize(
    ("description", "account"),
    [
        pytest.param("AT000000000000000000", "", id="iban-only"),
        pytest.param("account", "account", id="account-only"),
        pytest.param("10 | 11 | ", "", id="numeric-fields-only"),
        pytest.param(" | ", "", id="whitespace-fields-only"),
        pytest.param("", "", id="empty"),
        pytest.param("BG/000000004", "", id="bank-reference-only"),
        pytest.param(" , . / - ", "", id="punctuation-only"),
        pytest.param(
            r"Bezahlung Karte MC/000000005|-\\WIEN\9999",
            "",
            id="no-merchant-despite-city",
        ),
    ],
)
def test_missing_merchant_fallback(description: str, account: str) -> None:
    assert parse_description(description, account) == ParsedDescription(
        "Unknown", "Unknown merchant"
    )


@pytest.mark.parametrize(
    ("description", "merchant_name", "cleaned_description"),
    [
        pytest.param(
            "  Zinsen HABEN    BG/000000003  ",
            "Zinsen HABEN",
            "Zinsen HABEN",
            id="interest-with-bank-reference",
        ),
        pytest.param(
            "Einbehaltene KESt BG/000000002",
            "Einbehaltene KESt",
            "Einbehaltene KESt",
            id="tax-with-bank-reference",
        ),
        pytest.param(
            "Auszahlung Karte MC/000000006|AUTOMAT EDAT9999 K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
            id="withdrawal-automat",
        ),
        pytest.param(
            "Auszahlung Karte MC/000000011|BANKOMAT K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
            id="withdrawal-bankomat",
        ),
        pytest.param(
            "Auszahlung Karte MC/000000012|ATM 9999,00 XXX K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
            id="withdrawal-atm",
        ),
        pytest.param(
            "AUTOMAT K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
            id="standalone-automat",
        ),
        pytest.param(
            "BANKOMAT K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
            id="standalone-bankomat",
        ),
        pytest.param(
            "SB-Eigenerlag BG/000000008|01.01.2024 UM 12:34 / 0999 SB-Gerät Beispielbank FIL. 999",
            "Bargeldeinzahlung",
            "Bargeldeinzahlung",
            id="cash-deposit",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000009|EXAMPLE RAIL TICKET AUTOMAT\\WIEN\9999",
            "EXAMPLE RAIL TICKET AUTOMAT",
            "EXAMPLE RAIL TICKET AUTOMAT / WIEN",
            id="ticket-machine-not-cash",
        ),
    ],
)
def test_bank_operations(description: str, merchant_name: str, cleaned_description: str) -> None:
    assert parse_description(description) == ParsedDescription(merchant_name, cleaned_description)


@pytest.mark.parametrize(
    ("description", "merchant_name", "cleaned_description"),
    [
        pytest.param("ACME_AT_TEST-9999-X", "ACME", "ACME", id="order-reference"),
        pytest.param("ACME_AT_TEST-DEMO", "ACME", "ACME", id="hyphen-without-digits"),
        pytest.param("ACME_AT_TEST123", "ACME", "ACME", id="digits-without-hyphen"),
        pytest.param("ACME_AT_1234", "ACME", "ACME", id="numeric-reference"),
        pytest.param("EXAMPLE24_FR_ORDER-123", "EXAMPLE24", "EXAMPLE24", id="brand-digits"),
        pytest.param("Brand.Name_GB_ORDER-123", "Brand.Name", "Brand.Name", id="brand-punctuation"),
        pytest.param("ACME_AT_", "ACME", "ACME", id="empty-reference"),
        pytest.param("acme_at_test-9999-x", "acme", "acme", id="case-insensitive"),
        pytest.param(
            "OTHER DE OTHER_DE_TEST-9999-X",
            "OTHER",
            "OTHER",
            id="duplicated-display-prefix",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000010|E-COMM D009 01.01. 12:34|ACME AT ACME_AT_\\WIEN\9",
            "ACME",
            "ACME / WIEN",
            id="empty-reference-with-prefix-and-city",
        ),
        pytest.param(
            r"Bank | SUMUP *ACME_AT_TEST-9999-X\\WIEN\9999",
            "ACME",
            "ACME / WIEN",
            id="processor-and-order-reference",
        ),
    ],
)
def test_order_reference_cleanup(
    description: str, merchant_name: str, cleaned_description: str
) -> None:
    assert parse_description(description) == ParsedDescription(merchant_name, cleaned_description)


@pytest.mark.parametrize(
    "description",
    [
        pytest.param("ACME_US_SUPPORT", id="alphabetic-reference"),
        pytest.param("MADE_IN_ITALY", id="ordinary-underscored-name"),
        pytest.param("ACME_AT_TEST_REF", id="underscored-alphabetic-reference"),
        pytest.param("ACME_US___", id="underscore-only-reference"),
        pytest.param("EXAMPLE24_DE_SUPPORT", id="brand-digits-not-reference-digits"),
        pytest.param("ACME US ACME_US_SUPPORT", id="prefix-without-reference-marker"),
        pytest.param("ACME_AT", id="missing-reference-separator"),
        pytest.param("ACME_COUNTRY_REFERENCE", id="not-a-two-letter-country"),
        pytest.param("ACME AT OTHER_AT_TEST-9999-X", id="mismatched-merchant-prefix"),
        pytest.param("ACME DE ACME_AT_TEST-9999-X", id="mismatched-country-prefix"),
        pytest.param("ACME AT", id="country-without-reference"),
        pytest.param("SHOP ACME_AT_TEST-9999-X", id="embedded-token-not-whole-field"),
    ],
)
def test_order_reference_near_matches_remain_intact(description: str) -> None:
    assert parse_description(description) == ParsedDescription(description, description)
