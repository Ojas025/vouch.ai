"""Unit tests for vouch.preprocessing.adapter.

The adapters are pure functions from provider payload dicts to the canonical
IncomingMessage, so every test is a small fixture payload in, field-level
assertions out — no network, no broker, no database.
"""

import hashlib
import json
from datetime import datetime, timezone

import pytest

from vouch.ingestion import Channel, Event
from vouch.preprocessing.adapter import _adapt_telegram, _adapt_whatsapp, adapt
from vouch.preprocessing.models import Modality


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

WHATSAPP_TEXT_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "waba-1",
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {
                    "display_phone_number": "919876543210",
                    "phone_number_id": "1122334455",
                },
                "contacts": [{"profile": {"name": "Ravi"}, "wa_id": "91111111111"}],
                "messages": [{
                    "from": "91111111111",
                    "id": "wamid.ABC123",
                    "timestamp": "1700000000",
                    "type": "text",
                    "text": {"body": "urgent verify your account"},
                }],
            },
        }],
    }],
}


def telegram_update(**message_overrides):
    """A minimal Telegram update with the caller's message fields merged in."""
    message = {
        "message_id": 42,
        "from": {"id": 123, "is_bot": False, "first_name": "Ravi", "username": "ravi"},
        "chat": {"id": 987654321, "type": "private"},
        "date": 1700000000,
        "text": "hi",
    }
    message.update(message_overrides)
    return {"update_id": 987654321, "message": message}


# --------------------------------------------------------------------------
# WhatsApp
# --------------------------------------------------------------------------


class TestAdaptWhatsapp:
    def test_maps_a_plain_text_message(self):
        msg = _adapt_whatsapp(WHATSAPP_TEXT_PAYLOAD)

        assert msg.channel == Channel.WHATSAPP
        assert msg.provider == "whatsapp"
        assert msg.message_id == "wamid.ABC123"
        assert msg.submission_id == "wamid.ABC123"
        assert msg.text == "urgent verify your account"
        assert msg.sender_id == "91111111111"
        assert msg.recipient_id == "919876543210"
        assert msg.gateway_account_id == 1122334455
        assert msg.media == []

    def test_parses_provider_timestamp_as_utc(self):
        msg = _adapt_whatsapp(WHATSAPP_TEXT_PAYLOAD)

        assert msg.timestamp == datetime.fromtimestamp(1700000000, tz=timezone.utc)
        assert msg.sent_at == msg.timestamp
        assert msg.received_at.tzinfo is not None

    def test_payload_digest_is_sha256_of_canonical_payload(self):
        msg = _adapt_whatsapp(WHATSAPP_TEXT_PAYLOAD)

        expected = hashlib.sha256(
            json.dumps(WHATSAPP_TEXT_PAYLOAD, sort_keys=True).encode("utf-8")
        ).hexdigest()
        assert msg.payload_digest == expected
        assert len(msg.payload_digest) == 64

    def test_frequently_forwarded_flag_from_context(self):
        payload = json.loads(json.dumps(WHATSAPP_TEXT_PAYLOAD))
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["context"] = {
            "from": "91222222222",
            "id": "wamid.PARENT",
            "forwarded": True,
            "frequently_forwarded": True,
        }

        msg = _adapt_whatsapp(payload)

        assert msg.is_frequently_forwarded is True
        # The forward origin is provenance: it must not be dropped.
        assert msg.forwarded_origin == "91222222222"

    def test_forwarded_but_not_frequently_still_records_the_origin(self):
        payload = json.loads(json.dumps(WHATSAPP_TEXT_PAYLOAD))
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["context"] = {
            "from": "91222222222",
            "id": "wamid.PARENT",
            "forwarded": True,
        }

        msg = _adapt_whatsapp(payload)

        assert msg.is_frequently_forwarded is False
        assert msg.forwarded_origin == "91222222222"

    def test_unforwarded_message_has_no_origin(self):
        msg = _adapt_whatsapp(WHATSAPP_TEXT_PAYLOAD)

        assert msg.forwarded_origin is None

    def test_not_forwarded_by_default(self):
        msg = _adapt_whatsapp(WHATSAPP_TEXT_PAYLOAD)

        assert msg.is_frequently_forwarded is False

    def test_image_message_creates_attachment_metadata(self):
        payload = json.loads(json.dumps(WHATSAPP_TEXT_PAYLOAD))
        payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
            "from": "91111111111",
            "id": "wamid.IMG",
            "timestamp": "1700000000",
            "type": "image",
            "image": {
                "id": "media-1",
                "mime_type": "image/jpeg",
                "sha256": "a" * 64,
                "caption": "look at this",
                "file_size": 2048,
            },
        }

        msg = _adapt_whatsapp(payload)

        assert len(msg.media) == 1
        media = msg.media[0]
        assert media.kind == Modality.IMAGE
        assert media.file_id == "media-1"
        assert media.mime_type == "image/jpeg"
        assert media.sha256 == "a" * 64
        assert media.file_size_bytes == 2048
        assert msg.caption == "look at this"
        assert msg.text == "look at this"

    @pytest.mark.parametrize(
        ("kind", "block"),
        [
            ("audio", {"id": "media-aud", "mime_type": "audio/ogg", "sha256": "b" * 64}),
            ("video", {"id": "media-vid", "mime_type": "video/mp4", "sha256": "c" * 64}),
        ],
    )
    def test_audio_and_video_messages_create_attachments(self, kind, block):
        payload = json.loads(json.dumps(WHATSAPP_TEXT_PAYLOAD))
        payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
            "from": "91111111111",
            "id": "wamid.MEDIA",
            "timestamp": "1700000000",
            "type": kind,
            kind: block,
        }

        msg = _adapt_whatsapp(payload)

        assert len(msg.media) == 1
        assert msg.media[0].file_id == block["id"]
        assert msg.media[0].kind.value == kind

    def test_document_message_keeps_filename(self):
        payload = json.loads(json.dumps(WHATSAPP_TEXT_PAYLOAD))
        payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
            "from": "91111111111",
            "id": "wamid.DOC",
            "timestamp": "1700000000",
            "type": "document",
            "document": {
                "id": "media-doc",
                "mime_type": "application/pdf",
                "filename": "invoice.pdf",
                "sha256": "d" * 64,
                "caption": "pay this",
            },
        }

        msg = _adapt_whatsapp(payload)

        assert msg.media[0].kind == Modality.DOCUMENT
        assert msg.media[0].file_name == "invoice.pdf"
        assert msg.text == "pay this"


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


class TestAdaptTelegram:
    def test_maps_a_plain_text_message(self):
        msg = _adapt_telegram(telegram_update())

        assert msg.channel == Channel.TELEGRAM
        assert msg.provider == "telegram"
        assert msg.submission_id == "987654321"
        assert msg.message_id == "42"
        assert msg.text == "hi"
        assert msg.sender_id == "123"
        assert msg.recipient_id == "987654321"
        assert msg.gateway_account_id == 987654321
        assert msg.media == []

    def test_caption_only_message_uses_caption_as_text(self):
        update = telegram_update(text=None, caption="invoice attached")
        del update["message"]["text"]

        msg = _adapt_telegram(update)

        assert msg.caption == "invoice attached"
        assert msg.text == "invoice attached"

    def test_photo_selects_the_largest_resolution(self):
        update = telegram_update(text=None)
        del update["message"]["text"]
        update["message"]["photo"] = [
            {"file_id": "small", "width": 90, "height": 60, "file_size": 100},
            {"file_id": "large", "width": 800, "height": 600, "file_size": 9000},
            {"file_id": "medium", "width": 320, "height": 240, "file_size": 500},
        ]

        msg = _adapt_telegram(update)

        assert len(msg.media) == 1
        assert msg.media[0].file_id == "large"
        assert msg.media[0].kind == Modality.IMAGE
        assert msg.media[0].mime_type == "image/jpeg"
        assert msg.media[0].file_size_bytes == 9000

    @pytest.mark.parametrize(
        ("key", "kind", "file_name"),
        [
            ("document", Modality.DOCUMENT, "report.pdf"),
            ("audio", Modality.AUDIO, ""),
            ("voice", Modality.VOICE, ""),
            ("video", Modality.VIDEO, ""),
        ],
    )
    def test_media_attachments_are_typed(self, key, kind, file_name):
        update = telegram_update(text=None)
        del update["message"]["text"]
        update["message"][key] = {
            "file_id": f"tg-{key}",
            "file_name": file_name,
            "mime_type": "application/octet-stream",
            "file_size": 42,
        }

        msg = _adapt_telegram(update)

        assert len(msg.media) == 1
        assert msg.media[0].kind == kind
        assert msg.media[0].file_id == f"tg-{key}"
        assert msg.media[0].file_size_bytes == 42

    def test_forward_origin_from_user(self):
        update = telegram_update()
        update["message"]["forward_origin"] = {
            "type": "user",
            "sender_user": {"id": 555, "first_name": "Someone"},
        }

        assert _adapt_telegram(update).forwarded_origin == "555"

    def test_forward_origin_from_channel(self):
        update = telegram_update()
        update["message"]["forward_origin"] = {
            "type": "channel",
            "chat": {"id": -100123, "title": "News"},
        }

        assert _adapt_telegram(update).forwarded_origin == "-100123"

    def test_hidden_forward_origin_is_not_invented(self):
        update = telegram_update()
        update["message"]["forward_origin"] = {"type": "hidden_user"}

        assert _adapt_telegram(update).forwarded_origin is None

    def test_no_forward_origin_by_default(self):
        assert _adapt_telegram(telegram_update()).forwarded_origin is None

    def test_payload_digest_is_sha256_of_canonical_payload(self):
        update = telegram_update()

        msg = _adapt_telegram(update)

        expected = hashlib.sha256(
            json.dumps(update, sort_keys=True).encode("utf-8")
        ).hexdigest()
        assert msg.payload_digest == expected


# --------------------------------------------------------------------------
# adapt() routing
# --------------------------------------------------------------------------


class TestAdaptRouting:
    def test_whatsapp_channel_routes_to_the_whatsapp_adapter(self):
        event = Event(channel="whatsapp", idempotency_key="whatsapp:wamid.ABC123",
                      payload=WHATSAPP_TEXT_PAYLOAD)

        msg = adapt(event)

        assert msg.channel == Channel.WHATSAPP
        assert msg.message_id == "wamid.ABC123"

    def test_telegram_channel_routes_to_the_telegram_adapter(self):
        event = Event(channel="telegram", idempotency_key="telegram:987654321",
                      payload=telegram_update())

        msg = adapt(event)

        assert msg.channel == Channel.TELEGRAM
        assert msg.message_id == "42"

    @pytest.mark.parametrize("channel", ["rest", "carrier_pigeon"])
    def test_unsupported_channel_is_rejected_not_misrouted(self, channel):
        # Falling through to the Telegram parser would turn an unknown channel
        # into an opaque KeyError('message') (or silently misparse a payload
        # that happens to carry a "message" key).
        event = Event(channel=channel, idempotency_key=f"{channel}:1",
                      payload=WHATSAPP_TEXT_PAYLOAD)

        with pytest.raises(ValueError, match=channel):
            adapt(event)
