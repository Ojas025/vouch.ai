from typing import List
import re

from vouch.preprocessing.models import EntityType, ExtractedEntity, Pattern

RAZORPAY_IFSC_URL = "https://ifsc.razorpay.com/"

IFSC_PATTERN = r'\b[A-Z]{4}0[A-Z0-9]{6}\b'
UPI_VPA_PATTERN = r'\b[\w.\-]{2,}@[a-zA-Z]{2,}\b'
# A URL is anything up to the next whitespace/quote; sentence punctuation is
# trimmed afterwards rather than excluded up front, so paths and queries
# (…/path?q=1) survive while a trailing "." does not.
URL_PATTERN = r"""https?://[^\s<>"']+"""
AMOUNT_PATTERN = r"(?:[£$€₹]|USD|EUR|INR)\s*[,\d.]+"
EMAIL_PATTERN = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}'

TRAILING_URL_PUNCTUATION = ".,;:!?)]}'\""

PATTERNS = [
    Pattern(pattern=IFSC_PATTERN, type=EntityType.IFSC),
    Pattern(pattern=UPI_VPA_PATTERN, type=EntityType.UPI_VPA),
    Pattern(pattern=URL_PATTERN, type=EntityType.URL),
    Pattern(pattern=AMOUNT_PATTERN, type=EntityType.AMOUNT),
    Pattern(pattern=EMAIL_PATTERN, type=EntityType.EMAIL),
]


def extract_entities(text: str) -> List[ExtractedEntity]:
    """Extract typed entities from text, each normalized to its canonical form.

    Matches are collected with their spans first so that overlaps can be
    resolved: an email address also matches the VPA pattern (truncated at the
    domain's first dot), and without the overlap check it would be reported
    twice — once as a bogus VPA.
    """
    matches: dict[EntityType, list[tuple[int, int, str]]] = {}
    for pattern in PATTERNS:
        matches[pattern.type] = [
            (m.start(), m.end(), m.group(0)) for m in re.finditer(pattern.pattern, text)
        ]

    email_spans = [(start, end) for start, end, _ in matches[EntityType.EMAIL]]

    entities: List[ExtractedEntity] = []
    for pattern in PATTERNS:
        for start, end, match in matches[pattern.type]:
            if pattern.type == EntityType.UPI_VPA and any(
                start < email_end and email_start < end
                for email_start, email_end in email_spans
            ):
                # Overlaps an email address: not a VPA.
                continue

            if pattern.type == EntityType.URL:
                match = match.rstrip(TRAILING_URL_PUNCTUATION)
                if not match:
                    continue

            if pattern.type == EntityType.IFSC:
                normalized = match.upper()
            elif pattern.type in (EntityType.UPI_VPA, EntityType.EMAIL):
                normalized = match.lower()
            else:
                normalized = match

            entities.append(ExtractedEntity(
                type=pattern.type,
                normalized_value=normalized,
                extracted_value=match,
                source_field="text"
            ))

    return entities
