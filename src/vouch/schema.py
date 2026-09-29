from typing import List, Literal, Optional

from pydantic import BaseModel
from pydantic.types import datetime

class AttachmentMetadata(BaseModel):
    kind: Literal["image", "video", "audio", "voice", "document"]
    file_id: str
    file_name: str
    mime_type: str
    file_size_bytes: int

class IncomingMessage(BaseModel):
    provider: str
    message_id: str
    text: str
    timestamp: datetime
    sender_id: str
    caption: str = ""
    recipient_id: str
    gateway_account_id: int

    forwarded_origin: Optional[str] = None # user / channel id of the sender
    is_frequently_forwarded: bool

    payload_digest: str
    media: List[AttachmentMetadata] | None = None

class MessageSubmission(BaseModel):
    ...

class Result(BaseModel):
    ...
