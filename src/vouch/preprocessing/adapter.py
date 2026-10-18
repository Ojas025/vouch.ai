"""
Adapter for converting raw Telegram or Whatsapp payload into canonical MessageSubmission schema
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from vouch.ingestion import (
    AttachmentMetadata,
    Channel,
    Event,
    IncomingMessage,
)

def _adapt_whatsapp(raw_payload: dict[str, Any]) -> IncomingMessage:
    """Adapt a Meta WhatsApp Cloud API webhook payload to IncomingMessage.

    Expected payload shape (abbreviated):
    {
      "object": "whatsapp_business_account",
      "entry": [{
        "id": "<waba_id>",
        "changes": [{
          "field": "messages",
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "...", "phone_number_id": "..."},
            "contacts": [{"profile": {"name": "..."}, "wa_id": "..."}],
            "messages": [{
              "from": "<sender_wa_id>",
              "id": "wamid.<...>",
              "timestamp": "<unix_seconds_str>",
              "type": "text" | "image" | "audio" | "document" | "video" | ...,
              "text":     {"body": "..."},
              "image":    {"id": "<media_id>", "mime_type": "...", "sha256": "...", "caption": "..."},
              "document": {"id": "<media_id>", "mime_type": "...", "sha256": "...", "filename": "...", "caption": "..."},
              "audio":    {"id": "<media_id>", "mime_type": "...", "sha256": "..."},
              "video":    {"id": "<media_id>", "mime_type": "...", "sha256": "...", "caption": "..."},
              "context":  {"from": "...", "id": "wamid...", "forwarded": true, "frequently_forwarded": true}
            }]
          }
        }]
      }]
    }
    """
    value = raw_payload["entry"][0]["changes"][0]["value"]
    msg = value["messages"][0]
    metadata = value["metadata"]

    media: list[AttachmentMetadata] = []
    for kind, key in (("image", "image"), ("video", "video"), ("audio", "audio"), ("document", "document")):
        block = msg.get(key)
        if block:
            media.append(
                AttachmentMetadata(
                    kind=kind,
                    file_id=block["id"],
                    file_name=block.get("filename", ""),
                    mime_type=block["mime_type"],
                    file_size_bytes=block.get("file_size", 0),
                    sha256=block.get("sha256"),
                )
            )

    msg_type = msg.get("type")
    text = ""
    caption = ""
    if msg_type == "text":
        text = msg["text"]["body"]
    else:
        block = msg.get(msg_type, {})
        caption = block.get("caption", "")
        text = caption

    context = msg.get("context", {})
    sent_at = datetime.fromtimestamp(int(msg["timestamp"]), tz=timezone.utc)

    return IncomingMessage(
        submission_id=msg["id"],
        channel=Channel.WHATSAPP,
        provider="whatsapp",
        message_id=msg["id"],
        text=text,
        caption=caption,
        timestamp=sent_at,
        sent_at=sent_at,
        received_at=datetime.now(tz=timezone.utc),
        sender_id=msg["from"],
        recipient_id=metadata["display_phone_number"],
        gateway_account_id=int(metadata["phone_number_id"]),
        forwarded_origin=None,
        is_frequently_forwarded=bool(context.get("frequently_forwarded", False)),
        payload_digest=hashlib.sha256(
            json.dumps(raw_payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        media=media,
    )


def _adapt_telegram(raw_payload: dict[str, Any]) -> IncomingMessage:
    """Adapt a Telegram Bot API webhook update to IncomingMessage.

    Expected payload shape (abbreviated):
    {
      "update_id": 123456,
      "message": {
        "message_id": 42,
        "from": {"id": 123, "is_bot": false, "first_name": "...", "username": "..."},
        "chat": {"id": 123, "type": "private"},
        "date": 1700000000,
        "text": "...",                        # optional
        "caption": "...",                     # optional, with media
        "forward_origin": {"type": "user" | "channel" | "hidden_user", ...},  # optional
        "photo":    [{"file_id": "...", "width": 0, "height": 0, "file_size": 0}, ...],
        "document": {"file_id": "...", "file_name": "...", "mime_type": "...", "file_size": 0},
        "audio":    {"file_id": "...", "mime_type": "...", "file_size": 0},
        "voice":    {"file_id": "...", "mime_type": "...", "file_size": 0},
        "video":    {"file_id": "...", "mime_type": "...", "file_size": 0}
      }
    }
    """
    msg = raw_payload["message"]

    media: list[AttachmentMetadata] = []
    if msg.get("photo"):
        largest = max(msg["photo"], key=lambda p: p.get("width", 0) * p.get("height", 0))
        media.append(
            AttachmentMetadata(
                kind="image",
                file_id=largest["file_id"],
                file_name="",
                mime_type="image/jpeg",
                file_size_bytes=largest.get("file_size", 0),
            )
        )
    for kind, key in (("document", "document"), ("audio", "audio"), ("voice", "voice"), ("video", "video")):
        block = msg.get(key)
        if block:
            media.append(
                AttachmentMetadata(
                    kind=kind,
                    file_id=block["file_id"],
                    file_name=block.get("file_name", ""),
                    mime_type=block.get("mime_type", ""),
                    file_size_bytes=block.get("file_size", 0),
                )
            )

    forwarded_origin = None
    origin = msg.get("forward_origin")
    if origin:
        if origin.get("type") == "user" and origin.get("sender_user"):
            forwarded_origin = str(origin["sender_user"]["id"])
        elif origin.get("type") == "channel" and origin.get("chat"):
            forwarded_origin = str(origin["chat"]["id"])

    sent_at = datetime.fromtimestamp(msg["date"], tz=timezone.utc)

    return IncomingMessage(
        submission_id=str(raw_payload["update_id"]),
        channel=Channel.TELEGRAM,
        provider="telegram",
        message_id=str(msg["message_id"]),
        text=msg.get("text", "") or msg.get("caption", ""),
        caption=msg.get("caption", ""),
        timestamp=sent_at,
        sent_at=sent_at,
        received_at=datetime.now(tz=timezone.utc),
        sender_id=str(msg["from"]["id"]) if msg.get("from") else "",
        recipient_id=str(msg["chat"]["id"]),
        gateway_account_id=int(msg["chat"]["id"]),
        forwarded_origin=forwarded_origin,
        is_frequently_forwarded=False,
        payload_digest=hashlib.sha256(
            json.dumps(raw_payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        media=media,
    )


def adapt(event: Event) -> IncomingMessage:
    if event.channel == "whatsapp":
        return _adapt_whatsapp(event.payload)

    return _adapt_telegram(event.payload)
