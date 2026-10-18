"""
Normalizes raw input user message for handling unicode trickeries and a stable cache representation

1. NFKC Normalization
2. Strip zero-width characters
3. Homoglyph / confusable folding
4. Entity extraction using regex (phone numbers, upi id, etc.)
5. Store in cache
"""

import unicodedata

from disarm import Text
from vouch.ingestion import IncomingMessage
from vouch.preprocessing.entities import extract_entities
from vouch.preprocessing.models import CanonicalMessage, TextForms


def preprocess(msg: IncomingMessage) -> CanonicalMessage:
    processed_text = TextForms(raw_text=msg.text)

    # NFKC Normalization
    processed_text.nfkc_text = unicodedata.normalize("NFKC", processed_text.raw_text)

    # Strip invisible unicode codepoints
    processed_text.format_stripped_text = ''.join(ch for ch in processed_text.nfkc_text if unicodedata.category(ch)[0] not in ['C', 'Cf'])

    # Converge confusable homoglyphs into their ASCII variants
    processed_text.confusable_skeleton = str(Text(processed_text.format_stripped_text).normalize_confusables())

    # Script profile - what writing system and its ratio used in text
    t = Text(processed_text.format_stripped_text)

    processed_text.mixed_script = t.is_mixed_script()
    processed_text.script_profile = [script.value for script in t.detect_scripts()]

    # Extract entities
    entities = extract_entities(processed_text.format_stripped_text)

    return CanonicalMessage(
        submission_id=msg.submission_id,
        text_forms=processed_text,
        entities=entities,
        modalities_present=[],
    )
