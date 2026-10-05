"""Unit tests for the FastAPI webhook endpoints in vouch.api.server.

TestClient drives the real ASGI app, so routing, status codes and response
bodies are exercised end to end within the process. The two external edges are
mocked: the database session (SessionLocal) and the Redis stream. Signature
verification is left real so the auth path is genuinely tested.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from vouch.api import server as server_module
from vouch.api.server import create_entry

WHATSAPP_SECRET = "test-whatsapp-secret"
TELEGRAM_SECRET = "test-telegram-secret"

WHATSAPP_BODY = {"message_id": "wamid.ABC123", "text": "urgent verify your account"}
TELEGRAM_BODY = {"update_id": 987654321, "message": {"text": "hi"}}


def whatsapp_signature(body: bytes) -> str:
    return "sha256=" + hmac.new(WHATSAPP_SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def session():
    """Async-context-manager session whose adds and commits are recorded."""
    s = AsyncMock()
    s.add = AsyncMock()
    s.commit = AsyncMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    return s


@pytest.fixture
def client(session):
    """TestClient with SessionLocal replaced by a mock session factory."""
    with patch.object(server_module, "SessionLocal", return_value=session):
        with TestClient(server_module.app) as c:
            yield c


# --------------------------------------------------------------------------
# /ping
# --------------------------------------------------------------------------


class TestPing:
    def test_returns_ok(self, client):
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "message": "pong"}


# --------------------------------------------------------------------------
# WhatsApp webhook verification (GET challenge)
# --------------------------------------------------------------------------


class TestWhatsappVerification:
    def test_accepts_valid_token_and_returns_challenge(self, client):
        response = client.get(
            "/v1/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": WHATSAPP_SECRET,
            },
        )
        assert response.status_code == 200
        assert response.text == "1158201444"

    def test_rejects_invalid_verify_token(self, client):
        response = client.get(
            "/v1/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": "wrong-token",
            },
        )
        assert response.status_code == 403

    def test_rejects_non_subscribe_mode(self, client):
        response = client.get(
            "/v1/webhook",
            params={
                "hub.mode": "unsubscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": WHATSAPP_SECRET,
            },
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------
# WhatsApp webhook
# --------------------------------------------------------------------------


class TestWhatsappWebhook:
    def _post(self, client, payload, signature):
        body = json.dumps(payload).encode()
        headers = {"X-Hub-Signature-256": signature} if signature is not None else {}
        return client.post("/v1/webhook/whatsapp", content=body, headers=headers)

    def test_accepts_valid_signature(self, client):
        body = json.dumps(WHATSAPP_BODY).encode()
        response = self._post(client, WHATSAPP_BODY, whatsapp_signature(body))
        assert response.status_code == 200
        assert response.json() == {"ok": True}


    @pytest.mark.parametrize(
        "signature",
        ["", "sha256=deadbeef", "sha256=not-a-signature", "garbage", "sha1=abc123"],
        ids=["empty", "bad-hex", "garbage", "not-a-signature", "wrong-prefix"],
    )
    def test_rejects_invalid_signature(self, client, signature):
        assert self._post(client, WHATSAPP_BODY, signature).status_code == 401

    def test_rejects_signature_for_a_different_secret(self, client):
        body = json.dumps(WHATSAPP_BODY).encode()
        wrong = "sha256=" + hmac.new(b"attacker", body, hashlib.sha256).hexdigest()
        assert self._post(client, WHATSAPP_BODY, wrong).status_code == 401

    def test_rejects_tampered_body(self, client):
        signature = whatsapp_signature(json.dumps(WHATSAPP_BODY).encode())
        tampered = {**WHATSAPP_BODY, "text": "pay me"}
        assert self._post(client, tampered, signature).status_code == 401

    def test_rejected_request_writes_nothing(self, client, session):
        self._post(client, WHATSAPP_BODY, "sha256=nope")
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    def test_persists_submission_and_outbox_event(self, client, session):
        body = json.dumps(WHATSAPP_BODY).encode()
        self._post(client, WHATSAPP_BODY, whatsapp_signature(body))

        assert session.add.await_count == 2
        session.commit.assert_awaited_once()

    def test_idempotency_key_is_channel_prefixed_message_id(self, client, session):
        body = json.dumps(WHATSAPP_BODY).encode()
        self._post(client, WHATSAPP_BODY, whatsapp_signature(body))

        submission = session.add.await_args_list[0].args[0]
        assert submission.idempotency_key == "whatsapp:wamid.ABC123"

    def test_digest_is_hex_string(self, client, session):
        # sha256_digest is a String column, so the digest must be stored as hex.
        body = json.dumps(WHATSAPP_BODY).encode()
        self._post(client, WHATSAPP_BODY, whatsapp_signature(body))

        submission = session.add.await_args_list[0].args[0]
        assert submission.sha256_digest == hashlib.sha256(body).hexdigest()
        assert isinstance(submission.sha256_digest, str)

    def test_payload_is_stored_as_text(self, client, session):
        # Submission.payload is a TEXT column, so the dict must be serialised.
        body = json.dumps(WHATSAPP_BODY).encode()
        self._post(client, WHATSAPP_BODY, whatsapp_signature(body))

        submission = session.add.await_args_list[0].args[0]
        assert isinstance(submission.payload, str)
        assert json.loads(submission.payload) == WHATSAPP_BODY

    def test_channel_is_recorded_as_whatsapp(self, client, session):
        # A WhatsApp delivery must be labelled whatsapp, not telegram.
        body = json.dumps(WHATSAPP_BODY).encode()
        self._post(client, WHATSAPP_BODY, whatsapp_signature(body))

        submission = session.add.await_args_list[0].args[0]
        assert submission.channel == "whatsapp"

    def test_state_starts_received(self, client, session):
        body = json.dumps(WHATSAPP_BODY).encode()
        self._post(client, WHATSAPP_BODY, whatsapp_signature(body))

        assert session.add.await_args_list[0].args[0].state == "RECEIVED"

    def test_outbox_payload_matches_webhook_payload(self, client, session):
        body = json.dumps(WHATSAPP_BODY).encode()
        self._post(client, WHATSAPP_BODY, whatsapp_signature(body))

        outbox = session.add.await_args_list[1].args[0]
        assert outbox.payload == WHATSAPP_BODY

    def test_missing_message_id_is_rejected_as_bad_request(self, session):
        # A correctly signed payload that is structurally invalid is the
        # caller's error: 4xx, and nothing must be committed.
        payload = {"text": "no id here"}
        body = json.dumps(payload).encode()

        with patch.object(server_module, "SessionLocal", return_value=session):
            with TestClient(
                server_module.app, raise_server_exceptions=False
            ) as raw:
                response = raw.post(
                    "/v1/webhook/whatsapp", content=body,
                    headers={"X-Hub-Signature-256": whatsapp_signature(body)},
                )

        assert response.status_code == 400
        session.commit.assert_not_awaited()

    def test_malformed_json_is_rejected_as_bad_request(self, session):
        # Signature verifies over raw bytes, so a non-JSON body is a client error.
        body = b"{not json"
        with patch.object(server_module, "SessionLocal", return_value=session):
            with TestClient(
                server_module.app, raise_server_exceptions=False
            ) as raw:
                response = raw.post(
                    "/v1/webhook/whatsapp", content=body,
                    headers={"X-Hub-Signature-256": whatsapp_signature(body)},
                )

        assert response.status_code == 400
        session.commit.assert_not_awaited()


# --------------------------------------------------------------------------
# Telegram webhook
# --------------------------------------------------------------------------


class TestTelegramWebhook:
    def _post(self, client, payload, token):
        body = json.dumps(payload).encode()
        headers = {"X-Telegram-Bot-Api-Secret-Token": token} if token is not None else {}
        return client.post("/v1/webhook/telegram", content=body, headers=headers)

    def test_accepts_valid_token(self, client):
        response = self._post(client, TELEGRAM_BODY, TELEGRAM_SECRET)
        assert response.status_code == 200
        assert response.json() == {"ok": True}


    @pytest.mark.parametrize(
        "token",
        [None, "", "guessed", TELEGRAM_SECRET + " ", TELEGRAM_SECRET.upper()],
        ids=["missing", "empty", "wrong", "trailing-space", "case-mismatch"],
    )
    def test_rejects_invalid_token(self, client, token):
        assert self._post(client, TELEGRAM_BODY, token).status_code == 401

    def test_rejected_request_writes_nothing(self, client, session):
        self._post(client, TELEGRAM_BODY, "wrong")
        session.add.assert_not_called()

    def test_persists_submission_and_outbox_event(self, client, session):
        self._post(client, TELEGRAM_BODY, TELEGRAM_SECRET)
        assert session.add.await_count == 2
        session.commit.assert_awaited_once()

    def test_idempotency_key_uses_update_id(self, client, session):
        self._post(client, TELEGRAM_BODY, TELEGRAM_SECRET)

        submission = session.add.await_args_list[0].args[0]
        assert submission.idempotency_key == "telegram:987654321"

    def test_channel_is_telegram(self, client, session):
        self._post(client, TELEGRAM_BODY, TELEGRAM_SECRET)

        assert session.add.await_args_list[0].args[0].channel == "telegram"

    def test_missing_update_id_is_rejected_as_bad_request(self, session):
        # Structurally invalid payload, correctly authenticated: 4xx, no commit.
        with patch.object(server_module, "SessionLocal", return_value=session):
            with TestClient(
                server_module.app, raise_server_exceptions=False
            ) as raw:
                body = json.dumps({"message": {}}).encode()
                response = raw.post(
                    "/v1/webhook/telegram", content=body,
                    headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
                )

        assert response.status_code == 400
        session.commit.assert_not_awaited()

    def test_digest_is_hex_string(self, client, session):
        body = json.dumps(TELEGRAM_BODY).encode()
        self._post(client, TELEGRAM_BODY, TELEGRAM_SECRET)

        submission = session.add.await_args_list[0].args[0]
        assert submission.sha256_digest == hashlib.sha256(body).hexdigest()
        assert isinstance(submission.sha256_digest, str)

    def test_payload_is_stored_as_text(self, client, session):
        body = json.dumps(TELEGRAM_BODY).encode()
        self._post(client, TELEGRAM_BODY, TELEGRAM_SECRET)

        submission = session.add.await_args_list[0].args[0]
        assert isinstance(submission.payload, str)
        assert json.loads(submission.payload) == TELEGRAM_BODY


# --------------------------------------------------------------------------
# create_entry
# --------------------------------------------------------------------------


class TestCreateEntry:
    @pytest.mark.asyncio
    async def test_adds_submission_then_outbox_event(self, session):
        await create_entry(session, "k", {"a": 1}, b"digest")

        first, second = (c.args[0] for c in session.add.await_args_list)
        assert first.idempotency_key == "k"
        assert second.idempotency_key == "k"
        assert second.payload == {"a": 1}

    @pytest.mark.asyncio
    async def test_commits_once(self, session):
        await create_entry(session, "k", {}, b"d")
        session.commit.assert_awaited_once()



    @pytest.mark.asyncio
    async def test_propagates_commit_failure(self, session):
        session.commit.side_effect = RuntimeError("db down")
        with pytest.raises(RuntimeError, match="db down"):
            await create_entry(session, "k", {}, b"d")


# --------------------------------------------------------------------------
# App wiring
# --------------------------------------------------------------------------


class TestAppWiring:

    def test_closes_stream_on_shutdown(self, session):
        closed = AsyncMock()
        with patch.object(server_module, "stream", MagicMock(close=closed)):
            with TestClient(server_module.app):
                pass
        closed.assert_awaited_once()


    def test_does_not_publish_to_redis_from_the_http_path(self, client):
        # The transactional-outbox comment block is inert: enqueueing is the
        # relay's job, so the endpoint must not touch the stream.
        with patch.object(server_module, "stream") as stream:
            body = json.dumps(TELEGRAM_BODY).encode()
            client.post(
                "/v1/webhook/telegram", content=body,
                headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
            )
            stream.produce.assert_not_called()
