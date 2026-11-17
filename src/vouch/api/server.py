from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
import json
import hashlib
from dotenv import load_dotenv
import asyncio

from hypothesis.internal.cache import V
from pydantic import ValidationError
from vouch.api.ingestion import IngestionResult, WebhookIngestionService
from vouch.api.models import TelegramWebhookRequest, WebInvestigationRequest, WhatsAppWebhookRequest
from vouch.bootstrap.obscura import ensure_obscura
from vouch.config import settings
from vouch.db.session import SessionLocal
from vouch.logger import configure_logging, get_logger, shutdown_logging
from vouch.security import verify_telegram_token, verify_whatsapp_signature
from vouch.queue.stream import EventStream

load_dotenv()

logger = get_logger(__name__)

"""
provider_event_id:
- Telegram -> update_id
- Whatsapp -> messages[0].id
"""

if not settings.whatsapp_secret or not settings.telegram_webhook_secret:
    raise RuntimeError("WHATSAPP_WEB_SECRET and TELEGRAM_WEBHOOK_SECRET must be set")

WHATSAPP_SECRET = settings.whatsapp_secret
TELEGRAM_WEBHOOK_SECRET = settings.telegram_webhook_secret

stream = EventStream(redis_url=settings.redis_url, stream_name=settings.events_stream)
ingestion_service = WebhookIngestionService(SessionLocal)

async def _obscura_binary() -> Path:
    if settings.obscura_path is None:
        settings.set("obscura_path", await asyncio.to_thread(ensure_obscura))

    assert settings.obscura_path is not None
    return settings.obscura_path

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI starting - Redis pool ready.")

    await _obscura_binary()
    configure_logging()

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

@app.post("/v1/webhook/whatsapp", status_code=202)
async def whatsapp_webhook(request: Request):
    body = await request.body()

    # extract signature
    signature = request.headers.get("X-Hub-Signature-256", "")

    # verify signature
    if not verify_whatsapp_signature(body, signature, settings.whatsapp_secret):
        raise HTTPException(status_code=401) # Unauthorized

    payload_digest = hashlib.sha256(body).hexdigest()

    try:
        raw_payload = json.loads(body)
        payload = WhatsAppWebhookRequest.model_validate(raw_payload)


    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        raise HTTPException(status_code=400)

    messages = [
        message
        for entry in payload.entry
        for change in entry.changes
        for message in change.value.messages
    ]

    if not messages:
        return {"ok": True}

    if len(messages) != 1:
        logger.warning("Multiple messages in a single webhook payload are not supported")
        raise HTTPException(status_code=400)

    provider_event_id = messages[0].id

    try:
        # persist to postgresql
        ingestion_result: IngestionResult = await ingestion_service.ingest(
            "whatsapp",
            "0",
            provider_event_id,
            raw_payload,
            payload_digest,
        )

    except Exception:
        logger.exception("Failed to ingest webhook submission")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error",
        )

    return {
        "ok": True,
        "submission_id": ingestion_result.submission_id,
        "is_duplicate": ingestion_result.is_duplicate,
    }

@app.post("/v1/webhook/telegram", status_code=202)
async def telegram_webhook(request: Request):
    body = await request.body()

    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    # Verify - did the provider actually send this
    if not verify_telegram_token(body, token, settings.telegram_webhook_secret):
        raise HTTPException(status_code=401)

    payload_digest = hashlib.sha256(body).hexdigest()

    try:
        raw_payload = json.loads(body)
        payload = TelegramWebhookRequest.model_validate(raw_payload)

    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        raise HTTPException(status_code=400)

    provider_event_id = str(payload.update_id)

    try:
        # Transactional Outbox
        ingestion_result: IngestionResult = await ingestion_service.ingest(
            "telegram",
            "0",
            provider_event_id,
            raw_payload,
            payload_digest,
        )

    except Exception:
        logger.exception("Failed to ingest webhook submission")
        raise HTTPException(
            status_code=500,
            detail="Internal Server Error",
        )

    # Commit succeeded -> Submission + Event exists in Postgresql
    return {
        "ok": True,
        "submission_id": ingestion_result.submission_id,
        "is_duplicate": ingestion_result.is_duplicate,
    }

@app.post("/v1/investigate", status_code=202)
async def investigate(request: WebInvestigationRequest):
    logger.info("Received investigation request", extra={"channel": "web"})

    payload = request.model_dump()

    payload_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()

    try:
        ingestion_result: IngestionResult = await ingestion_service.ingest(
            "web",
            "0",
            request.event_id,
            payload,
            payload_digest,
        )

    except Exception as e:
        logger.error("Failed to ingest investigation request", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Internal Server Error")

    return {
        "ok": True,
        "submission_id": ingestion_result.submission_id,
        "is_duplicate": ingestion_result.is_duplicate,
    }
