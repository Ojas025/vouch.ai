from datetime import date
from typing import List, Optional

from pydantic import BaseModel

class AttachmentMetadata(BaseModel):
    file_id: int
    file_name: str
    mime_type: str

class Request(BaseModel):
    message_id: int
    text: str
    timestamp: date
    sender_id: int
    media: List[AttachmentMetadata] | None = None
    caption: str = ""
    forwarded_origin: int # user / channel id of the sender
    is_frequently_forwarded: bool

    recipient_id: int
    gateway_account_id: int

class MessageSubmission(BaseModel):
    ...

class Result(BaseModel):
    ...
