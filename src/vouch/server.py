from fastapi import FastAPI, Request, HTTPException
from vouch import security, adapter, orchestrator
import json
import hashlib

app = FastAPI()

@app.post("/v1/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    body = await request.body()

    # extract signature
    signature = request.headers.get("X-Hub-Signature-256", "")

    # verify signature
    if not security.verify_whatsapp_signature(body, signature, ""):
        raise HTTPException(status_code=401) # Unauthorized

    payload_digest = hashlib.sha256(body).digest()

    # insert into pg submissions
    # worm log

    payload = json.loads(body)

    message = adapter.adapt_whatsapp(payload)

    # push to redis queue later
    await orchestrator.run("whatsapp", message)

    return {"ok": True}

@app.post("/v1/webhook/telegram")
async def telegram_webhook(request: Request):
    body = await request.body()

    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    if not security.verify_telegram_token(body, token, ""):
        raise HTTPException(status_code=401)

    payload_digest = hashlib.sha256(body).digest()

    payload = json.dumps(body)

    # insert into pg submissions
    # worm log

    message = adapter.adapt_telegram(payload)

    # push to redis queue later
    await orchestrator.run("telegram", message)

    return {"ok": True}
