from typing import Any

from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
import json
import hashlib
from dotenv import load_dotenv
import os
import uuid

from vouch.db.models import OutboxEvent, Submission
from vouch.db.session import SessionLocal
from vouch.logger import configure_logging, get_logger, shutdown_logging
from vouch.security import verify_telegram_token, verify_whatsapp_signature
from vouch.stream import EventStream

load_dotenv()

logger = get_logger(__name__)

"""
provider_event_id:
- Telegram -> update_id
- Whatsapp -> messages[0].id
"""

WHATSAPP_SECRET = os.environ["WHATSAPP_SECRET"]
TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
REDIS_URL = os.environ["REDIS_URL"]

stream = EventStream(redis_url=REDIS_URL, stream_name="events:stream")

async def create_entry(session, key: str, payload: dict[str, Any], payload_digest: bytes) -> uuid.UUID:
    """
        Transactional Outbox
        - Insert into Submission and OutboxEvent
        - A RelayService then pushes this to the Redis queue
    """

    # Persist to Postgresql
    submission = Submission(
        channel="telegram",
        payload=payload,
        sha256_digest=payload_digest,
        state="RECEIVED",
        idempotency_key=key,
    )

    await session.add(submission)

    event = OutboxEvent(
        idempotency_key=key,
        payload=payload
    )

    await session.add(event)

    await session.commit()

    return event.id

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("FastAPI starting - Redis pool ready.")

    yield

    logger.info("FastAPI shutting down - Closing Redis connections")
    await stream.close()
    shutdown_logging()

app = FastAPI(lifespan=lifespan)

@app.get("/ping")
def pong(request: Request):
    return {
        "status": "ok",
        "message": "pong"
    }

@app.post("/v1/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    body = await request.body()

    # extract signature
    signature = request.headers.get("X-Hub-Signature-256", "")

    # verify signature
    if not verify_whatsapp_signature(body, signature, WHATSAPP_SECRET):
        raise HTTPException(status_code=401) # Unauthorized

    payload_digest = hashlib.sha256(body).digest()

    payload = json.loads(body)
    idempotency_key = f"whatsapp:{str(payload["message_id"])}"

    # persist to postgresql
    async with SessionLocal() as session:
        job_id = await create_entry(session, idempotency_key, payload, payload_digest)

    return {"ok": True}

@app.post("/v1/webhook/telegram")
async def telegram_webhook(request: Request):
    body = await request.body()

    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    # Verify - did the provider actually send this
    if not verify_telegram_token(body, token, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=401)

    # Dedup - Was this request already processed


    payload_digest = hashlib.sha256(body).digest()

    payload = json.loads(body)
    idempotency_key = f"telegram:{str(payload["update_id"])}"

    # Transactional Outbox
    async with SessionLocal() as session:
        job_id = await create_entry(session, idempotency_key, payload, payload_digest)

    # Commit succeeded -> Submission + Event exists in Postgresql
    return {"ok": True}
