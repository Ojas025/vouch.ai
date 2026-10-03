from typing import Literal
from enum import Enum
from pydantic import BaseModel
from datetime import datetime

class TrustLevel(str, Enum):
    SYSTEM = "system" # system metadata
    DETERMINISTIC = "deterministic" # deterministic code generated artifacts
    EXTERNAL_SOURCE = "external_source"
    UNTRUSTED_ARTIFACT = "untrusted_artifact" # anything read out of browser/apk/doc/image
    MODEL_OUTPUT = "model_output" # LLM-produced, may only exist as a Claim

class Provenance(BaseModel):
    """Who produced this, with what tool, from which bytes"""
    producer_type: Literal["AGENT", "SERVICE", "BRANCH"]
    producer_id: str
    tool_name: str
    tool_version: str
    prompt_version: str | None = None
    model_id: str | None = None
    sandbox_id: str | None = None
    artifact_sha256: str | None = None # which bytes were read as input
    observed_at: datetime

class EntityType(str, Enum):
    """Entity extracted from the provided input message"""
    PHONE = "phone"
    UPI_VPA = "upi_vpa"
    DOMAIN = "domain"
    IFSC = "ifsc"
    AMOUNT = "amount"
    URL = "url"
    APK_PACKAGE = "apk_package"
    FILE_SHA256 = "file_sha256"
    BRAND = "brand"

class EntityRef(BaseModel):
    ...
