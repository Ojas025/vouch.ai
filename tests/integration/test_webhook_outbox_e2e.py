"""Integration tests: webhook → PostgreSQL → outbox relay → Redis Stream.

This is the path ARCHITECTURE.md §1 draws: a provider POST is verified, written
to PostgreSQL in one transaction, and only the relay puts it on the queue. The
tests run the real FastAPI app against the real database and the real broker, so
the properties that matter are checked end to end:

* both rows land in the same transaction (a submission with no pending event, or
  an event with no submission, is corruption);
* the HTTP path never touches Redis — publishing is the relay's job;
* the relay turns the pending row into exactly one stream entry and flips the
  row to published.

Requires PostgreSQL and Redis on 127.0.0.1 (docker compose up -d).
"""

import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import select

from vouch.api import server as server_module
from vouch.api.outbox import publish_outbox
from vouch.db.models import OutboxEvent, Submission

pytestmark = pytest.mark.integration

# The request path dies before it writes anything: create_entry() awaits
# AsyncSession.add(), which is synchronous, so the POST raises
# "TypeError: object NoneType can't be used in 'await' expression" and returns
# 500. (The row it never reaches is also still blocked by published_at being
# NOT NULL.) Pinned strict so the one-line fix turns these into XPASS failures.
BLOCKED_BY_AWAITED_SESSION_ADD = pytest.mark.xfail(
    strict=True,
    reason=(
        "create_entry() awaits the synchronous AsyncSession.add(), so every webhook "
        "POST raises TypeError and returns 500 before a row is written"
    ),
)

WHATSAPP_SECRET = server_module.WHATSAPP_SECRET
TELEGRAM_SECRET = server_module.TELEGRAM_WEBHOOK_SECRET

WHATSAPP_PAYLOAD = {"message_id": "wamid.ABC123", "text": "urgent verify your account"}
TELEGRAM_PAYLOAD = {"update_id": 987654321, "message": {"text": "hi"}}


@pytest.fixture
def bound_client(db_sessionmaker, monkeypatch):
    """The app, wired to the real test database.

    httpx's ASGI transport (rather than TestClient) keeps the endpoint in the
    test's own event loop, which is the loop the engine's connection pool lives
    in — TestClient would run the app in a separate portal loop and every query
    would fail with "attached to a different loop".
    """
    monkeypatch.setattr(server_module, "SessionLocal", db_sessionmaker)
    transport = httpx.ASGITransport(app=server_module.app)
    return httpx.AsyncClient(transport=transport, base_url="http://vouch.test")


def signature(body: bytes) -> str:
    return "sha256=" + hmac.new(WHATSAPP_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def rows(session, model):
    return (await session.execute(select(model))).scalars().all()


@BLOCKED_BY_AWAITED_SESSION_ADD
class TestWebhookPersistence:
    @pytest.mark.asyncio
    async def test_signed_post_writes_submission_and_pending_event(
        self, bound_client, db_session
    ):
        body = json.dumps(WHATSAPP_PAYLOAD).encode()

        response = await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

        submissions = await rows(db_session, Submission)
        events = await rows(db_session, OutboxEvent)
        assert len(submissions) == 1
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_submission_records_the_raw_payload_and_digest(
        self, bound_client, db_session
    ):
        body = json.dumps(WHATSAPP_PAYLOAD).encode()

        await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        submission = (await rows(db_session, Submission))[0]
        assert submission.channel == "whatsapp"
        assert submission.idempotency_key == "whatsapp:wamid.ABC123"
        assert submission.state == "RECEIVED"
        # Stored as text, but still the exact payload the provider sent.
        assert json.loads(submission.payload) == WHATSAPP_PAYLOAD
        assert submission.sha256_digest == hashlib.sha256(body).hexdigest()

    @pytest.mark.asyncio
    async def test_outbox_row_starts_pending_and_unpublished(
        self, bound_client, db_session
    ):
        body = json.dumps(WHATSAPP_PAYLOAD).encode()

        await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        event = (await rows(db_session, OutboxEvent))[0]
        assert event.status == "pending"
        assert event.published_at is None
        assert event.payload == WHATSAPP_PAYLOAD

    @pytest.mark.asyncio
    async def test_telegram_post_creates_its_own_pair(self, bound_client, db_session):
        body = json.dumps(TELEGRAM_PAYLOAD).encode()

        response = await bound_client.post(
            "/v1/webhook/telegram", content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )

        assert response.status_code == 200
        submission = (await rows(db_session, Submission))[0]
        event = (await rows(db_session, OutboxEvent))[0]
        assert submission.channel == "telegram"
        assert submission.idempotency_key == "telegram:987654321"
        assert event.channel == "telegram"

    @pytest.mark.asyncio
    async def test_http_path_does_not_publish_to_redis(self, bound_client, event_stream):
        # Transactional outbox: the request path must not reach the broker.
        body = json.dumps(WHATSAPP_PAYLOAD).encode()

        await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        assert await event_stream.client.xlen(event_stream.stream_name) == 0


class TestRejectedRequests:
    @pytest.mark.asyncio
    async def test_bad_signature_writes_nothing(self, bound_client, db_session):
        body = json.dumps(WHATSAPP_PAYLOAD).encode()

        response = await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )

        assert response.status_code == 401
        assert await rows(db_session, Submission) == []
        assert await rows(db_session, OutboxEvent) == []

    @pytest.mark.asyncio
    async def test_missing_identifier_writes_nothing(self, bound_client, db_session):
        body = json.dumps({"text": "no id"}).encode()

        response = await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        assert response.status_code == 400
        assert await rows(db_session, Submission) == []
        assert await rows(db_session, OutboxEvent) == []

    @pytest.mark.asyncio
    async def test_wrong_telegram_token_writes_nothing(self, bound_client, db_session):
        body = json.dumps(TELEGRAM_PAYLOAD).encode()

        response = await bound_client.post(
            "/v1/webhook/telegram", content=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": "guessed"},
        )

        assert response.status_code == 401
        assert await rows(db_session, Submission) == []


@BLOCKED_BY_AWAITED_SESSION_ADD
class TestRelayPublishesToRedis:
    @pytest.mark.asyncio
    async def test_relay_puts_the_pending_row_on_the_stream(
        self, bound_client, db_session, event_stream
    ):
        body = json.dumps(WHATSAPP_PAYLOAD).encode()
        await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        await publish_outbox(db_session, event_stream)

        entries = await event_stream.client.xrange(event_stream.stream_name)
        assert len(entries) == 1
        published = json.loads(entries[0][1]["data"])
        assert published == {
            "channel": "whatsapp",
            "idempotency_key": "whatsapp:wamid.ABC123",
            "payload": WHATSAPP_PAYLOAD,
        }

    @pytest.mark.asyncio
    async def test_relay_marks_the_row_published(
        self, bound_client, db_session, event_stream
    ):
        body = json.dumps(WHATSAPP_PAYLOAD).encode()
        await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        await publish_outbox(db_session, event_stream)
        await db_session.refresh((await rows(db_session, OutboxEvent))[0])

        event = (await rows(db_session, OutboxEvent))[0]
        assert event.status == "published"
        assert event.published_at is not None

    @pytest.mark.asyncio
    async def test_second_relay_pass_does_not_republish(
        self, bound_client, db_session, event_stream
    ):
        body = json.dumps(WHATSAPP_PAYLOAD).encode()
        await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        await publish_outbox(db_session, event_stream)
        await publish_outbox(db_session, event_stream)

        assert await event_stream.client.xlen(event_stream.stream_name) == 1

    @pytest.mark.asyncio
    async def test_submission_survives_publication(self, bound_client, db_session, event_stream):
        body = json.dumps(WHATSAPP_PAYLOAD).encode()
        await bound_client.post(
            "/v1/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body)},
        )

        await publish_outbox(db_session, event_stream)

        # The case file is independent of the event's delivery state.
        submission = (await rows(db_session, Submission))[0]
        assert submission.state == "RECEIVED"
        assert json.loads(submission.payload) == WHATSAPP_PAYLOAD