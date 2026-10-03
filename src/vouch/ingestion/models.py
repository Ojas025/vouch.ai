from dataclasses import dataclass
from typing import List, Literal, Any, Optional
from enum import Enum
from pydantic import BaseModel, Field
from datetime import datetime

class Channel(str, Enum):
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    REST = "rest"

@dataclass
class Event:
    idempotency_key: str
    payload: dict[str, Any]

class AttachmentMetadata(BaseModel):
    kind: Literal["image", "video", "audio", "voice", "document"]
    file_id: str
    file_name: str
    mime_type: str
    file_size_bytes: int
    sha256: str | None = None # filled after fetching attachment
    blob_ref: str | None = None # filled after fetching attachment

class IncomingMessage(BaseModel):
    submission_id: str
    channel: Channel
    delivery_id: str
    provider: str
    message_id: str
    text: str
    timestamp: datetime
    sender_id: str
    caption: str = ""
    recipient_id: str
    gateway_account_id: int

    sent_at: datetime
    received_at: datetime

    forwarded_origin: Optional[str] = None # user / channel id of the sender
    is_frequently_forwarded: bool

    payload_digest: str
    media: List[AttachmentMetadata] = Field(default_factory=list)
