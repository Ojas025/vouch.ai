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
from langchain_core.messages import SystemMessage
from pydantic import BaseModel, Field
from pydantic_core import PydanticSerializationError
from vouch.agents.llm import LLMClient
from vouch.agents.models import Capability, LLMProfile
from vouch.agents.registry import PROMPTS_DIR
from vouch.agents.utils import load_prompt
from vouch.ingestion import IncomingMessage
from vouch.logger import get_logger
from vouch.preprocessing.entities import extract_entities
from vouch.preprocessing.models import CanonicalMessage, ExtractedEntity, Modality, TextForms

logger = get_logger(__file__)

class ExtractorOutput(BaseModel):
    entities: set[ExtractedEntity] = Field(
        default_factory=set,
        description="Set of unique entities extracted from the input text"
    )

entity_extractor = LLMClient(LLMProfile(
    model="minicpm",
    api_key="not-required",
    base_url="http://127.0.0.1:8080/v1",
    capabilities=[Capability.JSON_SCHEMA, Capability.VISION],
    system_prompt_path=PROMPTS_DIR / "entity_extractor.prompt.md"
), output_model=ExtractorOutput)

async def extract_entities_using_llm(text: str, canonical_msg: CanonicalMessage):
    try:
        obtained_entities = [
            entity.model_dump_json(indent=2) for entity in canonical_msg.entities
            if isinstance(entity, BaseModel)
        ]

        prompt = load_prompt(
            entity_extractor.profile.system_prompt_path,
            text=text,
            obtained_entities=obtained_entities
        )

        response = await entity_extractor.ainvoke([
            SystemMessage(content=prompt)
        ])

        if isinstance(response, BaseModel):
            return response.model_dump()

        return response

    except PydanticSerializationError:
        logger.exception(f"Error while serializing {canonical_msg.entities}")
        raise
    except Exception as e:
        logger.exception(f"LLM-based entity extraction failed: {str(e)}")
        raise


def get_media_kind(file_name: str, mime_type: str) -> Modality:
    is_apk = (
        file_name.lower().endswith(".apk")
        or mime_type == "application/vnd.android.package-archive"
    )

    if is_apk:
        return Modality.APK

    return Modality.UNKNOWN

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

    modalities = {
        f"{attachment.file_id}": get_media_kind(attachment.file_name, attachment.mime_type)
        for attachment in msg.media
    }

    return CanonicalMessage(
        submission_id=msg.submission_id,
        text_forms=processed_text,
        entities=entities,
        modalities=modalities,
    )
