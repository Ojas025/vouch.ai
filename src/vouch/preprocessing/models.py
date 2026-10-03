from enum import Enum
from pydantic import BaseModel

class Modality(str, Enum):
    TEXT = "text"
    URL = "url"
    APK = "apk"
    DOCUMENT = "document"
    IMAGE = "image"

class ExtractedEntity(BaseModel):
    ...

class TextForms(BaseModel):
    raw_text: str
    nfkc_text: str
    format_stripped_text: str
    confusable_skeleton: str
    script_profile: dict[str, int]
    mixed_script: bool

class CanonicalMessage(BaseModel):
    submission_id: str
    text_forms: TextForms
    entities: list[ExtractedEntity]
    modalities_present: list[Modality]
    canonicalizer_version: str
