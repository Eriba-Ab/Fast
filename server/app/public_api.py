"""Public, taxpayer-facing API: one notice at a time, payment start and return,
receipts for verified payments. No lists, no name search, never a phone number."""
import re
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import db, ledger, security
from .monnify import MonnifyError

router = APIRouter(prefix="/api")

DN_RE = re.compile(r"^DN\d{15}$")
TTIN_RE = re.compile(r"^TTIN\d{11}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
NO_MATCH = "No demand notice matches that number. Check it against your notice: notice numbers start with DN and have 15 digits; TTINs start with TTIN."
METHOD_LABEL = {"ACCOUNT_TRANSFER": "Bank transfer", "CARD": "Debit card", "USSD": "USSD",
                "PHONE_NUMBER": "Phone number", "PAY_WITH_BANK": "Pay with Bank", "CASH": "Cash at Moniepoint agent"}


def _err(status: int, message: str):
    return JSONResponse({"error": message}, status_code=status)


def _find_dn(conn, q: str) -> str | None:
    key = (q or "").strip().upper()
    if DN_RE.match(key):
        row = conn.execute("SELECT dn FROM assessments WHERE dn = %s", (key,)).fetchone()
    elif TTIN_RE.match(key):
        row = conn.execute("""SELECT dn FROM assessments WHERE upper(ttin) = %s
                              ORDER BY tax_year DESC LIMIT 1""", (key,)).fetchone()
    else:
        return None
    return row["dn"] if row else None


def _payments_enabled(request: Request) -> bool:
    s = request.app.state.settings
    return s.payments_enabled and s.monnify_configured


def notice_payload(conn, dn: str, settings) -> dict:
    st = ledger.notice_status(conn, dn)
    a = st["assessment"]
    meta = conn.execute("SELECT value FROM meta WHERE key = 'workbook'").fetchone()["value"]
    heads = [{"name": n, "amount_kobo": int(v)} for n, v in zip(meta["heads"], a["heads_kobo"])]
    pays = conn.execute("""SELECT transaction_reference, amount_paid_kobo, paid_on, payment_method, payment_status
                           FROM payments WHERE dn = %s ORDER BY paid_on NULLS LAST""", (dn,)).fetchall()
    return {
        "dn": a["dn"], "ttin": a["ttin"], "name": a["name"], "address": a["address"],
        "tax_year": a["tax_year"], "assessment_date": a["assessment_date"].isoformat(),
        "due_date": a["due_date"].isoformat(), "heads": heads, "fee_heads": meta["feeHeads"],
        "subtotal_kobo": a["subtotal_fees_kobo"], "total_kobo": st["total_kobo"],
        "legacy_kobo": st["legacy_kobo"], "live_kobo": st["live_kobo"],
        "balance_kobo": st["balance_kobo"], "settled": st["settled"],
        "payments": [{
            "transaction_reference": p["transaction_reference"], "amount_kobo": p["amount_paid_kobo"],
            "paid_on": p["paid_on"].isoformat() if p["paid_on"] else None,
            "method": METHOD_LABEL.get(p["payment_method"] or "", p["payment_method"] or "Online"),
            "status": p["payment_status"],
            "receipt_token": security.receipt_token(settings.app_secret, p["transaction_reference"])
            if p["payment_status"] in ledger.COUNTED else None,
        } for p in pays],
    }


@router.get("/config")
def config(request: Request):
    return {"payments_enabled": _payments_enabled(request),
            "environment": request.app.state.settings.monnify_env}


@router.get("/notice")
def notice(request: Request, q: str = ""):
    s = request.app.state.settings
    iphash = security.ip_hash(s.app_secret, security.client_ip(request))
    with db.tx() as conn:
        if security.lookup_blocked(conn, iphash):
            return _err(429, "Too many unsuccessful searches. Please wait a while and try again.")
        dn = _find_dn(conn, q)
        security.record_lookup(conn, iphash, "notice", dn is not None)
        if not dn:
            return _err(404, NO_MATCH)
        body = notice_payload(conn, dn, s)
    body["payments_enabled"] = _payments_enabled(request)
    return body


@router.post("/pay/start")
def pay_start(request: Request, payload: dict = Body(...)):
    s = request.app.state.settings
    if not _payments_enabled(request):
        return _err(503, "Online payment is not yet available for this notice.")
    iphash = security.ip_hash(s.app_secret, security.client_ip(request))
    email = (payload.get("email") or "").strip()
    if email and not EMAIL_RE.match(email):
        return _err(400, "Enter a valid email address, or leave it blank.")

    with db.tx() as conn:
        if security.pay_start_blocked(conn, iphash):
            return _err(429, "Too many payment attempts. Please wait a while and try again.")
        security.record_lookup(conn, iphash, "pay_start", True)
        dn = _find_dn(conn, payload.get("q") or payload.get("dn") or "")
        if not dn:
            return _err(404, NO_MATCH)
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (dn,))
        st = ledger.notice_status(conn, dn)
        balance = st["balance_kobo"]
        if balance <= 0:
            return _err(409, "This notice is already settled. Nothing is due.")
        # an unexpired checkout for the same amount is reused instead of creating another
        open_intent = conn.execute("""SELECT checkout_url FROM payment_intents
            WHERE dn = %s AND status = 'CREATED' AND expected_amount_kobo = %s AND checkout_url IS NOT NULL
              AND expires_at > now() + interval '5 minutes'
            ORDER BY created_at DESC LIMIT 1""", (dn, balance)).fetchone()
        if open_intent:
            return {"checkoutUrl": open_intent["checkout_url"]}
        attempt = conn.execute("SELECT COALESCE(MAX(attempt),0)+1 AS n FROM payment_intents WHERE dn = %s",
                               (dn,)).fetchone()["n"]
        pay_ref = ledger.payment_reference(dn, attempt)
        host = urlparse(s.public_base_url).hostname or "taraba.govpayhub.com"
        cust_email = email or f"{dn.lower()}@notices.{host}"
        conn.execute("""INSERT INTO payment_intents (payment_reference, dn, attempt, expected_amount_kobo,
                          customer_email, status, expires_at)
                        VALUES (%s, %s, %s, %s, %s, 'CREATED', now() + interval '40 minutes')""",
                     (pay_ref, dn, attempt, balance, cust_email))
        a = st["assessment"]

    client = request.app.state.monnify()
    try:
        res = client.init_transaction(
            amount_kobo=balance, payment_reference=pay_ref,
            description=f"TIRS demand notice {dn} ({a['tax_year']})",
            customer_email=cust_email, customer_name=a["name"][:100],
            redirect_url=f"{s.public_base_url}/api/pay/return/{quote(pay_ref)}",
            metadata={"dn": dn, "ttin": a["ttin"] or "", "taxYear": str(a["tax_year"])})
    except MonnifyError as e:
        with db.tx() as conn:
            conn.execute("UPDATE payment_intents SET status = 'FAILED' WHERE payment_reference = %s", (pay_ref,))
        request.app.state.log.warning("init-transaction failed for %s: %s", pay_ref, e)
        return _err(502, "The payment service could not be reached. Please try again in a few minutes.")
    with db.tx() as conn:
        conn.execute("""UPDATE payment_intents SET transaction_reference = %s, checkout_url = %s
                        WHERE payment_reference = %s""",
                     (res["transactionReference"], res["checkoutUrl"], pay_ref))
    return {"checkoutUrl": res["checkoutUrl"]}


@router.get("/pay/return/{pay_ref}")
def pay_return(request: Request, pay_ref: str):
    """Monnify sends the taxpayer back here. Its query parameters are only a hint:
    the server asks Monnify itself before showing anything as paid."""
    with db.tx() as conn:
        intent = conn.execute("SELECT dn FROM payment_intents WHERE payment_reference = %s", (pay_ref,)).fetchone()
    if not intent:
        return RedirectResponse("/#/pay", status_code=303)
    result = ledger.verify_and_record(request.app.state.monnify(), source="return", payment_reference=pay_ref)
    status = result.get("status", "")
    outcome = "paid" if status in ledger.COUNTED else "failed" if status in ("FAILED", "EXPIRED") else "pending"
    return RedirectResponse(f"/#/pay/{intent['dn']}?result={outcome}", status_code=303)


@router.get("/receipt")
def receipt(request: Request, ref: str = "", t: str = ""):
    s = request.app.state.settings
    if not security.receipt_token_ok(s.app_secret, ref, t):
        return _err(404, "This receipt link is not valid.")
    with db.tx() as conn:
        p = conn.execute("SELECT * FROM payments WHERE transaction_reference = %s", (ref,)).fetchone()
        if not p or p["payment_status"] not in ledger.COUNTED:
            return _err(404, "No verified payment matches this receipt.")
        body = notice_payload(conn, p["dn"], s)
    body["receipt"] = {
        "transaction_reference": p["transaction_reference"], "payment_reference": p["payment_reference"],
        "amount_kobo": p["amount_paid_kobo"], "paid_on": p["paid_on"].isoformat() if p["paid_on"] else None,
        "method": METHOD_LABEL.get(p["payment_method"] or "", p["payment_method"] or "Online"),
        "status": p["payment_status"], "payer_account_last4": p["payer_account_last4"],
        "verified_at": p["verified_at"].isoformat(),
    }
    return body


@router.get("/verify")
def verify(request: Request, ref: str = ""):
    """Anyone holding a receipt can check it here (the receipt's QR code points to it)."""
    s = request.app.state.settings
    iphash = security.ip_hash(s.app_secret, security.client_ip(request))
    with db.tx() as conn:
        if security.lookup_blocked(conn, iphash):
            return _err(429, "Too many unsuccessful checks. Please wait a while and try again.")
        p = conn.execute("""SELECT p.*, a.name, a.tax_year FROM payments p JOIN assessments a ON a.dn = p.dn
                            WHERE p.transaction_reference = %s""", (ref.strip(),)).fetchone()
        security.record_lookup(conn, iphash, "notice", p is not None)
    if not p:
        return _err(404, "No payment with this reference is recorded by the Taraba State Internal Revenue Service.")
    return {"transaction_reference": p["transaction_reference"], "dn": p["dn"], "name": p["name"],
            "tax_year": p["tax_year"], "amount_kobo": p["amount_paid_kobo"],
            "paid_on": p["paid_on"].isoformat() if p["paid_on"] else None,
            "method": METHOD_LABEL.get(p["payment_method"] or "", p["payment_method"] or "Online"),
            "status": p["payment_status"], "valid": p["payment_status"] in ledger.COUNTED}
