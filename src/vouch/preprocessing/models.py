from enum import Enum
from typing import List, Literal
from pydantic import BaseModel, Field

class Modality(str, Enum):
    TEXT = "text"
    URL = "url"
    APK = "apk"
    DOCUMENT = "document"
    IMAGE = "image"
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

class ExtractedEntity(BaseModel):
    type: EntityType
    normalized_value: str
    extracted_value: str
    source_field: Literal["text", "ocr", "qr", "image"]

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
    modalities_present: list[Modality]
