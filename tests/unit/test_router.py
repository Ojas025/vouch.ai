"""Unit tests for the modality router (vouch.router).

What is implemented and pinned here: attachment-kind resolution and the
channel dispatch that picks a provider's metadata endpoint. The fan-out itself
is a stub (route() returns None on every path, analyze_apk is `...`), so the
branch-dispatch expectation is written in full and marked strict xfail — the
suite reports the gap instead of quietly agreeing with it.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from vouch.ingestion import AttachmentMetadata, Channel, IncomingMessage
from vouch.preprocessing.models import CanonicalMessage, Modality, TextForms
from vouch.router import RoutePlan, get_media_kind, request_media_download, route

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def message_with(media: list[AttachmentMetadata]) -> IncomingMessage:
    return IncomingMessage(
        submission_id="sub-1",
        channel=Channel.WHATSAPP,
        provider="whatsapp",
        message_id="wamid.1",
        text="forwarded text",
        timestamp=NOW,
        sender_id="91111111111",
        recipient_id="919876543210",
        gateway_account_id=1,
        sent_at=NOW,
        received_at=NOW,
        is_frequently_forwarded=False,
        payload_digest="0" * 64,
        media=media,
    )


class TestGetMediaKind:
    @pytest.mark.parametrize(
        "file_name",
        ["invoice.apk", "bank.APK", "setup.Apk"],
    )
    def test_apk_filename_is_an_apk(self, file_name):
        media = AttachmentMetadata(file_name=file_name, mime_type="application/octet-stream")

        assert get_media_kind(media) is Modality.APK

    def test_apk_mime_type_is_an_apk_even_without_a_filename(self):
        # WhatsApp sends no filename for some uploads; the mime type decides.
        media = AttachmentMetadata(
            file_name="", mime_type="application/vnd.android.package-archive"
        )

        assert get_media_kind(media) is Modality.APK

    @pytest.mark.parametrize(
        ("file_name", "mime_type"),
        [
            ("invoice.pdf", "application/pdf"),
            ("photo.jpg", "image/jpeg"),
            ("", ""),
        ],
    )
    def test_everything_else_is_unknown(self, file_name, mime_type):
        media = AttachmentMetadata(file_name=file_name, mime_type=mime_type)

        assert get_media_kind(media) is Modality.UNKNOWN


class TestMediaDownloadDispatch:
    @pytest.fixture
    def providers(self, monkeypatch):
        telegram = AsyncMock(return_value="telegram-metadata")
        whatsapp = AsyncMock(return_value="whatsapp-metadata")
        monkeypatch.setattr(
            "vouch.router._request_telegram_media_download", telegram
        )
        monkeypatch.setattr(
            "vouch.router._request_whatsapp_media_download", whatsapp
        )
        return telegram, whatsapp

    @pytest.mark.asyncio
    async def test_telegram_channel_uses_the_telegram_endpoint(self, providers):
        telegram, whatsapp = providers

        result = await request_media_download(Channel.TELEGRAM, "file-1")

        telegram.assert_awaited_once_with("file-1")
        whatsapp.assert_not_awaited()
        assert result == "telegram-metadata"

    @pytest.mark.asyncio
    async def test_whatsapp_channel_uses_the_meta_endpoint(self, providers):
        telegram, whatsapp = providers

        result = await request_media_download(Channel.WHATSAPP, "media-1")

        whatsapp.assert_awaited_once_with("media-1")
        telegram.assert_not_awaited()
        assert result == "whatsapp-metadata"


class TestBranchDispatch:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Modality branch runtime not implemented: route() returns None on every "
            "path and analyze_apk is a stub, so no RoutePlan is ever produced"
        ),
    )
    @pytest.mark.asyncio
    async def test_text_only_message_yields_a_route_plan(self):
        canonical = CanonicalMessage(
            submission_id="sub-1",
            text_forms=TextForms(raw_text="forwarded text"),
            entities=[],
            modalities_present=[Modality.TEXT],
        )

        plan = await route(message_with([]), canonical)

        assert isinstance(plan, RoutePlan)
        assert plan.jobs == []

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Modality branch runtime not implemented: APK attachments never reach a "
            "branch because route() returns None and analyze_apk is a stub"
        ),
    )
    @pytest.mark.asyncio
    async def test_apk_attachment_yields_a_branch_job(self):
        canonical = CanonicalMessage(
            submission_id="sub-1",
            text_forms=TextForms(raw_text="forwarded text"),
            entities=[],
            modalities_present=[Modality.APK],
        )
        media = AttachmentMetadata(
            kind=Modality.APK, file_id="media-1", file_name="bank.apk"
        )

        plan = await route(message_with([media]), canonical)

        assert isinstance(plan, RoutePlan)
        assert [job.modality for job in plan.jobs] == [Modality.APK]