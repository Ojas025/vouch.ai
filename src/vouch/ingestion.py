from dataclasses import dataclass
from pathlib import Path
from typing import List, Any, Optional
from enum import Enum
from pydantic import BaseModel, Field
from datetime import datetime

from vouch.preprocessing.models import Modality

class Channel(str, Enum):
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    REST = "rest"

@dataclass
class Event:
    channel: str
    idempotency_key: str
    payload: dict[str, Any]

class AttachmentMetadata(BaseModel):
    kind: Modality = Modality.UNKNOWN
    file_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    file_size_bytes: int = 0
    sha256: str | None = None # filled after fetching attachment
    blob_ref: str | None = None # filled after fetching attachment
    download_metadata: "MediaDownloadMetadata | None" = None
    file_path: Path | None = None

class MediaDownloadMetadata(BaseModel):
    url: str = ""
    mime_type: str = ""
    sha256: str = ""
    file_size: int = 0
    id: str = ""

# Resolve the forward reference now that MediaDownloadMetadata is defined.
AttachmentMetadata.model_rebuild()

class IncomingMessage(BaseModel):
    submission_id: str
    channel: Channel
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
