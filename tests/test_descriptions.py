"""Shared merchant and AI description parsing, using synthetic transaction data."""

import pytest

from fyntok.descriptions import ParsedDescription, parse_description


# Patterns from the opening CSV rows, with synthetic merchants, identifiers, and dates.
@pytest.mark.parametrize(
    ("description", "merchant_name", "cleaned_description"),
    [
        pytest.param(
            r"Bezahlung Karte MC/000000001|BIOMARKT999 9999 K009 01.01. 12:34|"
            r"BEISPIEL BIOMARKT\\WIEN\9999",
            "BEISPIEL BIOMARKT",
            "BEISPIEL BIOMARKT / WIEN",
            id="card-supermarket-location",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000002|POS M9XX K009 01.01. 12:34|"
            r"SUMUP *BEISPIELPRAXIS\\WIEN\9",
            "BEISPIELPRAXIS",
            "BEISPIELPRAXIS / WIEN",
            id="card-payment-processor",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000003|E-COMM K009 01.01. 12:34|"
            r"UZR*BEISPIELTICKET*DEMO\\WIEN\9999",
            "UZR*BEISPIELTICKET*DEMO",
            "UZR*BEISPIELTICKET*DEMO / WIEN",
            id="ecommerce-ticket-processor",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000004|POS 9999 K009 01.01. 12:34|"
            r"BEISPIEL CAFE\\WIEN\9999",
            "BEISPIEL CAFE",
            "BEISPIEL CAFE / WIEN",
            id="card-cafe",
        ),
        pytest.param(
            "/CNR/999999/DOC/99999999/12.34/20240101|OG/000000005|"
            "TESTATWWXXX AT000000000000000000 Beispiel Lieferdienst GmbH",
            "Beispiel Lieferdienst GmbH",
            "Beispiel Lieferdienst GmbH",
            id="direct-debit-inline-iban",
        ),
        pytest.param(
            "Beispiel Mobil KdNr: 9999999 OG/000000006|"
            "TESTATWWXXX AT000000000000000000|Beispiel Firma GmbH",
            "Beispiel Firma GmbH",
            "Beispiel Firma GmbH",
            id="direct-debit-customer-number",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000007|MARKT 9999 K009 01.01. 12:34|"
            r"BEISPIEL MARKT DANKT 9999\\WIEN\9999",
            "BEISPIEL MARKT",
            "BEISPIEL MARKT / WIEN",
            id="card-merchant-branch-number",
        ),
        pytest.param(
            "REF-EXAMPLE999999 OG/000000008|"
            "TESTFRP1XXX FR0000000000000000000000000|Beispiel Medien GmbH",
            "Beispiel Medien GmbH",
            "Beispiel Medien GmbH",
            id="transfer-reference-and-foreign-iban",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000009|DROGERIE-FIL. A999 9999 K009 01.01. 12:34|"
            r"DROGERIE-FIL. A999\\WIEN\9999      W",
            "DROGERIE",
            "DROGERIE / WIEN",
            id="card-alphanumeric-branch-and-suffix",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000010|POS 9999 K009 01.01. 12:34|"
            r"BEISPIEL PIZZERIA\\WIEN\9999",
            "BEISPIEL PIZZERIA",
            "BEISPIEL PIZZERIA / WIEN",
            id="card-restaurant",
        ),
        pytest.param(
            "MIETE 01/2024 OG/000000011|TESTATWWXXX AT000000000000000000|"
            "Beispiel gemeinnutzige Wohnungsakt",
            "Beispiel gemeinnutzige Wohnungsakt",
            "Beispiel gemeinnutzige Wohnungsakt",
            id="rent-transfer",
        ),
        pytest.param(
            "SALDO BEISPIEL 999999999999 OG/000000012|"
            "TESTATWWXXX AT000000000000000000 Beispiel Betreiberfirma GmbH",
            "Beispiel Betreiberfirma GmbH",
            "Beispiel Betreiberfirma GmbH",
            id="invoice-transfer-inline-iban",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000013|POS 9999 K009 01.01. 12:34|"
            r"BEISPIEL DROGERIE DANKT 9999\\WIEN\9999",
            "BEISPIEL DROGERIE",
            "BEISPIEL DROGERIE / WIEN",
            id="card-drugstore",
        ),
        pytest.param(
            r"Bezahlung Karte MC/000000014|POS 9999 K009 01.01. 12:34|"
            r"APOTHEKE BEISPIELVIERT\\WIEN\9",
            "APOTHEKE BEISPIELVIERT",
            "APOTHEKE BEISPIELVIERT / WIEN",
            id="card-pharmacy",
        ),
    ],
)
def test_csv_description_patterns(
    description: str, merchant_name: str, cleaned_description: str
) -> None:
    assert parse_description(description, "AT000000000000000000") == ParsedDescription(
        merchant_name, cleaned_description
    )


@pytest.mark.parametrize(
    ("description", "account", "merchant_name", "cleaned_description"),
    [
        (
            "Beispielbank kreditkarte TEST 200001 MC/000000001|"
            "Beispielbank kreditkarte TEST 200001Abrechnung Nr. 000000001|10",
            "",
            "Beispielbank kreditkarte TEST",
            "Beispielbank kreditkarte TEST",
        ),
        (
            "Musterbank kreditkarte DEMO           200002       MC/000000002|"
            "Abrechnung Nr. 000000002",
            "",
            "Musterbank kreditkarte DEMO",
            "Musterbank kreditkarte DEMO",
        ),
        (
            "Beispielbank kreditkarte account 200001 MC/000000001|Abrechnung Nr. 000000001",
            "account",
            "Beispielbank kreditkarte",
            "Beispielbank kreditkarte",
        ),
        (
            "Beispielbank kreditkarte user@example.com 200001 MC/000000001|10",
            "",
            "Beispielbank kreditkarte",
            "Beispielbank kreditkarte [EMAIL]",
        ),
        ("Bank | Shop 24 | 10 | 11 | ", "", "Shop", "Shop"),
        ("Bank |  Café   Beispiel  | 12 | ", "", "Café Beispiel", "Café Beispiel"),
        ("Bank | 7-Shop", "", "7-Shop", "7-Shop"),
        ("Pharmacy", "a", "Pharmacy", "Pharmacy"),
        (
            "account | card 123 01.01.2024 | EXAMPLE GROCERY 543 Berlin",
            "account",
            "EXAMPLE GROCERY Berlin",
            "EXAMPLE GROCERY Berlin",
        ),
        (
            "Bank | AT000000000000000000 / Beispiel Sonnenschutz und | 0000000001",
            "",
            "Beispiel Sonnenschutz und",
            "Beispiel Sonnenschutz und",
        ),
        (
            "Bank | account user@example.com Shop Reference: secret | 10",
            "account",
            "Shop",
            "[EMAIL] Shop",
        ),
        (
            "DE00000000000000000000 user@example.com account 123456 Reference: secret",
            "account",
            "Unknown",
            "[EMAIL]",
        ),
        (
            "AT00 0000 0000 0000 0000 DEMO",
            "",
            "DEMO",
            "DEMO",
        ),
        (
            "GB00 TEST 0000 0000 0000 00 Example",
            "",
            "Example",
            "Example",
        ),
        (
            r"Bank | EXAMPLE24 GMBH\\DRESDEN\01237|SPESEN: 0,00",
            "",
            "EXAMPLE24 GMBH",
            "EXAMPLE24 GMBH / DRESDEN",
        ),
        (
            r"Bank | EXAMPLE* SOFTWARE SUBSCR\\DUBLIN|SPESEN: 0,00|10|",
            "",
            "EXAMPLE* SOFTWARE SUBSCR",
            "EXAMPLE* SOFTWARE SUBSCR / DUBLIN",
        ),
        (r"Bank | EXAMPLE MARKET DANKT 4624\\WIEN\1020", "", "EXAMPLE MARKET", "EXAMPLE MARKET / WIEN"),
        (r"Bank | EXAMPLE CARE-FIL. A112\\WIEN\1020 W", "", "EXAMPLE CARE", "EXAMPLE CARE / WIEN"),
        (
            r"Bank | SUMUP *EXAMPLE CLINIC\\WIEN\1",
            "",
            "EXAMPLE CLINIC",
            "EXAMPLE CLINIC / WIEN",
        ),
        ("Shop | Reference: secret", "", "Shop", "Shop"),
        ("Shop | SPESEN: 0,00", "", "Shop", "Shop"),
        ("Shop | OG/000000001", "", "Shop", "Shop"),
        ("Shop | TESTATWWXXX", "", "Shop", "Shop"),
        ("TESTATWWXXX Example", "", "Example", "Example"),
        ("AT000000000000000000", "", "Unknown", "Unknown merchant"),
        ("account", "account", "Unknown", "Unknown merchant"),
        (
            r"Shop\\WIEN AT000000000000000000\9999",
            "",
            "Shop",
            "Shop / WIEN",
        ),
        ("10 | 11 | ", "", "Unknown", "Unknown merchant"),
        (" | ", "", "Unknown", "Unknown merchant"),
        ("", "", "Unknown", "Unknown merchant"),
        ("Shop " + "x" * 200, "", "Shop " + "x" * 200, "Shop " + "x" * 155),
    ],
)
def test_description_parsing(
    description: str, account: str, merchant_name: str, cleaned_description: str
) -> None:
    assert parse_description(description, account) == ParsedDescription(
        merchant_name, cleaned_description
    )


@pytest.mark.parametrize(
    ("description", "merchant_name", "cleaned_description"),
    [
        ("Zinsen HABEN BG/000000001", "Zinsen HABEN", "Zinsen HABEN"),
        ("Einbehaltene KESt BG/000000002", "Einbehaltene KESt", "Einbehaltene KESt"),
        ("  Zinsen HABEN    BG/000000003  ", "Zinsen HABEN", "Zinsen HABEN"),
        ("BG/000000004", "Unknown", "Unknown merchant"),
        ("-", "Unknown", "Unknown merchant"),
        (" , . / - ", "Unknown", "Unknown merchant"),
        (r"Bezahlung Karte MC/000000005|-\\WIEN\9999", "Unknown", "Unknown merchant"),
        (
            "Auszahlung Karte MC/000000006|AUTOMAT K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
        ),
        (
            "Auszahlung Karte MC/000000007|AUTOMAT EDAT9999 K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
        ),
        ("AUTOMAT K009 01.01. 12:34", "Bargeldabhebung", "Bargeldabhebung"),
        ("BANKOMAT K009 01.01. 12:34", "Bargeldabhebung", "Bargeldabhebung"),
        (
            "Auszahlung Karte MC/000000011|BANKOMAT K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
        ),
        (
            "Auszahlung Karte MC/000000012|ATM 9999,00 XXX K009 01.01. 12:34",
            "Bargeldabhebung",
            "Bargeldabhebung",
        ),
        (
            "SB-Eigenerlag BG/000000008|01.01.2024 UM 12:34 / 0999 SB-Gerät Beispielbank FIL. 999",
            "Bargeldeinzahlung",
            "Bargeldeinzahlung",
        ),
        (
            r"Bezahlung Karte MC/000000009|EXAMPLE RAIL TICKET AUTOMAT\\WIEN\9999",
            "EXAMPLE RAIL TICKET AUTOMAT",
            "EXAMPLE RAIL TICKET AUTOMAT / WIEN",
        ),
        ("ACME_AT_TEST-9999-X", "ACME", "ACME"),
        ("ACME_AT_TEST-DEMO", "ACME", "ACME"),
        ("ACME_AT_TEST123", "ACME", "ACME"),
        ("ACME_AT_1234", "ACME", "ACME"),
        ("ACME_US_SUPPORT", "ACME_US_SUPPORT", "ACME_US_SUPPORT"),
        ("MADE_IN_ITALY", "MADE_IN_ITALY", "MADE_IN_ITALY"),
        ("ACME_AT_TEST_REF", "ACME_AT_TEST_REF", "ACME_AT_TEST_REF"),
        ("ACME_US___", "ACME_US___", "ACME_US___"),
        ("BIKE24_DE_SUPPORT", "BIKE24_DE_SUPPORT", "BIKE24_DE_SUPPORT"),
        (
            "ACME US ACME_US_SUPPORT",
            "ACME US ACME_US_SUPPORT",
            "ACME US ACME_US_SUPPORT",
        ),
        ("OTHER_DE_TEST-9999-X", "OTHER", "OTHER"),
        ("BIKE24_FR_ORDER-123", "EXAMPLE24", "EXAMPLE24"),
        ("Brand.Name_GB_ORDER-123", "Brand.Name", "Brand.Name"),
        ("ACME_AT_", "ACME", "ACME"),
        ("OTHER DE OTHER_DE_TEST-9999-X", "OTHER", "OTHER"),
        ("ACME_AT", "ACME_AT", "ACME_AT"),
        ("ACME_COUNTRY_REFERENCE", "ACME_COUNTRY_REFERENCE", "ACME_COUNTRY_REFERENCE"),
        (
            "ACME AT OTHER_AT_TEST-9999-X",
            "ACME AT OTHER_AT_TEST-9999-X",
            "ACME AT OTHER_AT_TEST-9999-X",
        ),
        (
            "ACME DE ACME_AT_TEST-9999-X",
            "ACME DE ACME_AT_TEST-9999-X",
            "ACME DE ACME_AT_TEST-9999-X",
        ),
        ("acme_at_test-9999-x", "acme", "acme"),
        ("ACME", "ACME", "ACME"),
        ("ACME AT", "ACME AT", "ACME AT"),
        (
            r"Bezahlung Karte MC/000000010|E-COMM D009 01.01. 12:34|"
            r"ACME AT ACME_AT_\\WIEN\9",
            "ACME",
            "ACME / WIEN",
        ),
        (
            r"Bank | SUMUP *ACME_AT_TEST-9999-X\\WIEN\9999",
            "ACME",
            "ACME / WIEN",
        ),
        ("ACMEWARE", "ACMEWARE", "ACMEWARE"),
        ("ACME MARKET", "ACME MARKET", "ACME MARKET"),
        (
            "SHOP ACME_AT_TEST-9999-X",
            "SHOP ACME_AT_TEST-9999-X",
            "SHOP ACME_AT_TEST-9999-X",
        ),
    ],
)
def test_bank_operations_and_order_references(
    description: str, merchant_name: str, cleaned_description: str
) -> None:
    assert parse_description(description) == ParsedDescription(merchant_name, cleaned_description)
