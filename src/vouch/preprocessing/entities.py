from typing import List
import re

from vouch.preprocessing.models import EntityType, ExtractedEntity, Pattern

RAZORPAY_IFSC_URL = "https://ifsc.razorpay.com/"

IFSC_PATTERN = r'\b[A-Z]{4}0[A-Z0-9]{6}\b'
UPI_VPA_PATTERN = r'\b[\w.\-]{2,}@[a-zA-Z]{2,}\b'
URL_PATTERN = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+'
AMOUNT_PATTERN = r"(?:[£$€₹]|USD|EUR|INR)\s*[,\d.]+"
EMAIL_PATTERN = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}'

def extract_entities(text: str) -> List[ExtractedEntity]:
    entities: List[ExtractedEntity] = []
    patterns = [
        Pattern(pattern=IFSC_PATTERN, type=EntityType.IFSC),
        Pattern(pattern=UPI_VPA_PATTERN, type=EntityType.UPI_VPA),
        Pattern(pattern=URL_PATTERN, type=EntityType.URL),
        Pattern(pattern=AMOUNT_PATTERN, type=EntityType.AMOUNT),
        Pattern(pattern=EMAIL_PATTERN, type=EntityType.EMAIL),
    ]

    for pattern in patterns:
        matches = re.findall(pattern.pattern, text)

        for match in matches:
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
