from enum import Enum

from pydantic import BaseModel, Field

class ResponseTemplate(str, Enum):
    VERIFIED_THREAT = "verified_threat"
    SUSPICIOUS = "suspicious"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    BENIGN_WITH_LIMITATIONS = "benign_with_limitations"
    ANALYSIS_UNAVAILABLE = "analysis_unavailable"

class GroundedSentence(BaseModel):
    text: str
    claim_ids: list[str] = Field(min_length=1)

class Response(BaseModel):
    response_id: str
    decision_id: str
    template: ResponseTemplate
    sentences: list[GroundedSentence]
    language: str
    grounding_verified: bool
    fell_back_to_template: bool
