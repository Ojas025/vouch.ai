"""Unit tests for vouch.preprocessing.entities.

extract_entities() is a pure text -> typed-entities function, so each test is
an input string and the entities that must (or must not) come out of it.
"""

import pytest

from vouch.preprocessing.entities import extract_entities
from vouch.preprocessing.models import EntityType


def types_of(text):
    return [e.type for e in extract_entities(text)]


def one(text, entity_type):
    """The single entity of the given type, failing if there is not exactly one."""
    matches = [e for e in extract_entities(text) if e.type == entity_type]
    assert len(matches) == 1, f"expected exactly one {entity_type}, got {matches}"
    return matches[0]


@pytest.mark.parametrize("code", ["HDFC0001234", "SBIN0000456"])
def test_valid_ifsc_is_extracted(code):
    entity = one(f"IFSC for the transfer is {code}", EntityType.IFSC)

    assert entity.extracted_value == code


def test_ifsc_is_normalized_to_uppercase():
    entity = one("use HDFC0001234 for NEFT", EntityType.IFSC)

    assert entity.normalized_value == "HDFC0001234"
    assert entity.normalized_value == entity.extracted_value.upper()


def test_ifsc_with_a_non_zero_fifth_character_is_ignored():
    # The 5th character of an IFSC must be '0'; HDFC1234567 is not an IFSC.
    assert EntityType.IFSC not in types_of("reference HDFC1234567 failed")


@pytest.mark.parametrize("vpa", ["merchant@okaxis", "support.pay@icici"])
def test_valid_upi_vpa_is_extracted(vpa):
    entity = one(f"pay {vpa} now", EntityType.UPI_VPA)

    assert entity.extracted_value == vpa


def test_upi_vpa_is_normalized_to_lowercase():
    entity = one("refund goes to Merchant@OKAxis today", EntityType.UPI_VPA)

    assert entity.normalized_value == "merchant@okaxis"
    assert entity.extracted_value == "Merchant@OKAxis"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://example.com", "http://example.com"),
        ("https://sub.domain.org/path?q=1", "https://sub.domain.org/path?q=1"),
    ],
)
def test_url_is_extracted_with_path_and_query(url, expected):
    entity = one(f"see {url} for details", EntityType.URL)

    assert entity.normalized_value == expected


def test_trailing_sentence_punctuation_is_not_part_of_the_url():
    entity = one("check https://example.com. really", EntityType.URL)

    assert entity.normalized_value == "https://example.com"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("₹500", "₹500"),
        ("$1,200.50", "$1,200.50"),
        ("INR 2500", "INR 2500"),
    ],
)
def test_amount_is_extracted(raw, expected):
    entity = one(f"received {raw} yesterday", EntityType.AMOUNT)

    assert entity.normalized_value == expected


@pytest.mark.parametrize("email", ["user@example.com", "alert+sec@domain.co.in"])
def test_email_is_extracted(email):
    entity = one(f"contact {email} immediately", EntityType.EMAIL)

    assert entity.normalized_value == email.lower()


def test_email_is_normalized_to_lowercase():
    entity = one("write to Alert+Sec@Domain.CO.IN now", EntityType.EMAIL)

    assert entity.normalized_value == "alert+sec@domain.co.in"


def test_an_email_is_not_also_reported_as_a_upi_vpa():
    # An email address has a dotted domain, so it must not leak into the
    # VPA list as a truncated "user@example" match.
    assert EntityType.UPI_VPA not in types_of("mail user@example.com today")


def test_source_field_is_text():
    entity = one("pay merchant@okaxis", EntityType.UPI_VPA)

    assert entity.source_field == "text"


def test_multiple_entities_in_one_message():
    entities = extract_entities(
        "pay ₹500 to merchant@okaxis via https://example.com/pay now"
    )

    assert [(e.type, e.normalized_value) for e in entities] == [
        (EntityType.UPI_VPA, "merchant@okaxis"),
        (EntityType.URL, "https://example.com/pay"),
        (EntityType.AMOUNT, "₹500"),
    ]


def test_empty_text_yields_no_entities():
    assert extract_entities("") == []


def test_text_without_matches_yields_no_entities():
    assert extract_entities("please verify your account urgently") == []
