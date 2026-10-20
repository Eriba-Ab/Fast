"""Monnify webhook receiver.

Store the raw notification, answer 200 immediately (as Monnify asks), and leave the
real work to the worker, which re-queries Monnify before recording anything.
A duplicate notification is a no-op thanks to the unique index on webhook_events."""
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import db, security
from .monnify import verify_signature

router = APIRouter(prefix="/api")


@router.post("/monnify/webhook")
async def monnify_webhook(request: Request):
    s = request.app.state.settings
    log = request.app.state.log
    raw = await request.body()                      # exact bytes, hashed as received
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"status": "ignored"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"status": "ignored"}, status_code=400)

    sig_ok = verify_signature(raw, request.headers.get("monnify-signature"), s.monnify_webhook_secret)
    ip = security.client_ip(request)
    if s.monnify_env == "live" and sig_ok is not True:
        # not trusted, but still stored: the worker only acts on what Monnify's API confirms
        log.warning("webhook with %s signature from %s", "missing" if sig_ok is None else "invalid", ip)
    if ip != s.monnify_webhook_ip:
        log.info("webhook from unexpected IP %s (Monnify publishes %s)", ip, s.monnify_webhook_ip)

    event_type = str(payload.get("eventType") or "UNKNOWN")[:64]
    data = payload.get("eventData") if isinstance(payload.get("eventData"), dict) else {}
    txn_ref = data.get("transactionReference")
    pay_ref = data.get("paymentReference")
    with db.tx() as conn:
        conn.execute("""INSERT INTO webhook_events (event_type, transaction_reference, payment_reference, payload,
                          signature_ok, source_ip)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                        ON CONFLICT (event_type, transaction_reference) WHERE transaction_reference IS NOT NULL
                        DO NOTHING""",
                     (event_type, txn_ref, pay_ref, json.dumps(payload), sig_ok, ip))
        conn.execute("NOTIFY tirs_webhook")          # wakes the worker (delivered on commit)
    return {"status": "received"}
