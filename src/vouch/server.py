from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
import json
import hashlib
from dotenv import load_dotenv
import os

from vouch import security
from vouch.event_stream import EventStream
from vouch.logger import get_logger

load_dotenv()

logger = get_logger("server")

WHATSAPP_SECRET = os.environ["WHATSAPP_SECRET"]
TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
REDIS_URL = os.environ["REDIS_URL"]

stream = EventStream(redis_url=REDIS_URL, stream_name="events:stream")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI starting - Redis pool ready.")

    yield

    logger.info("FastAPI shutting down - Closing Redis connections")
    await stream.close()

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
    if not security.verify_whatsapp_signature(body, signature, WHATSAPP_SECRET):
        raise HTTPException(status_code=401) # Unauthorized

    payload_digest = hashlib.sha256(body).digest()

    payload = json.loads(body)

    # persist to postgresql

    # push to redis stream``
    stream_id = await stream.produce(payload)

    return {"ok": True}

@app.post("/v1/webhook/telegram")
async def telegram_webhook(request: Request):
    body = await request.body()

    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    if not security.verify_telegram_token(body, token, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=401)

    payload_digest = hashlib.sha256(body).digest()

    payload = json.loads(body)

    # persist to postgresql

    # enqueue
    stream_id = await stream.produce(payload)

    return {"ok": True}
