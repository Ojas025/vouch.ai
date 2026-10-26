from enum import Enum
from typing import List, Literal
from pydantic import BaseModel, Field

class Modality(str, Enum):
    TEXT = "text"
    URL = "url"
    APK = "apk"
    DOCUMENT = "document"
    IMAGE = "image"
    # Media kinds the adapters can produce from provider payloads; without
    # these members an audio/video/voice attachment fails model validation.
    AUDIO = "audio"
    VIDEO = "video"
    VOICE = "voice"
    UNKNOWN = "unknown"


class EntityType(str, Enum):
    """Entity extracted from the provided input message"""
    PHONE = "phone"
    UPI_VPA = "upi_vpa"
    IFSC = "ifsc"
    AMOUNT = "amount"
    URL = "url"
    EMAIL = "email"
    APK_PACKAGE = "apk_package"
    FILE_SHA256 = "file_sha256"
    BRAND = "brand"
    NAME = "name"
    OTHER = "other"

class ExtractedEntity(BaseModel):
    type: EntityType
    extracted_value: str
    normalized_value: str = ""
    source_field: Literal["text", "document", "qr", "image"] | None = None

class Pattern(BaseModel):
    pattern: str
    type: EntityType

class TextForms(BaseModel):
    raw_text: str = ""
    nfkc_text: str = ""
    format_stripped_text: str = ""
    confusable_skeleton: str = ""
    script_profile: List[str] = Field(default_factory=list)
    mixed_script: bool = False

class CanonicalMessage(BaseModel):
    submission_id: str
    text_forms: TextForms
    entities: list[ExtractedEntity]
    modalities: dict[str, Modality]
