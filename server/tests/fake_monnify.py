"""A local stand-in for Monnify's API, for testing only (never deployed).

It mimics the request/response shapes documented at developers.monnify.com
(see docs/monnify-integration-plan.md section 1) closely enough to run the whole
flow locally: login, init-transaction, a checkout page, the webhook, query, search.
Run: uvicorn tests.fake_monnify:app --port 8099"""
import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI()
API_KEY = os.environ.get("FAKE_API_KEY", "MK_TEST_LOCAL")
SECRET = os.environ.get("FAKE_SECRET_KEY", "SK_TEST_LOCAL")
PUBLIC = os.environ.get("FAKE_PUBLIC_URL", "http://localhost:8099").rstrip("/")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
SIGN_WEBHOOKS = os.environ.get("FAKE_SIGN_WEBHOOKS", "false") == "true"   # sandbox sends no signature
WAT = timezone(timedelta(hours=1))
TXNS: dict[str, dict] = {}
TOKEN = "fake-" + secrets.token_hex(8)


def ok(body):
    return JSONResponse({"requestSuccessful": True, "responseMessage": "success", "responseCode": "0", "responseBody": body})


def fail(msg, status=400):
    return JSONResponse({"requestSuccessful": False, "responseMessage": msg, "responseCode": "99"}, status_code=status)


def authed(request: Request) -> bool:
    return request.headers.get("authorization") == f"Bearer {TOKEN}"


@app.post("/api/v1/auth/login")
def login(request: Request):
    expected = "Basic " + base64.b64encode(f"{API_KEY}:{SECRET}".encode()).decode()
    if request.headers.get("authorization") != expected:
        return fail("Invalid client credentials", 401)
    return ok({"accessToken": TOKEN, "expiresIn": 3600})


@app.post("/api/v1/merchant/transactions/init-transaction")
async def init(request: Request):
    if not authed(request):
        return fail("Unauthorized", 401)
    p = await request.json()
    for f in ("amount", "paymentReference", "contractCode", "redirectUrl", "customerEmail"):
        if not p.get(f):
            return fail(f"{f} is required")
    if any(t["paymentReference"] == p["paymentReference"] for t in TXNS.values()):
        return fail("Duplicate payment reference")
    ref = f"MNFY|99|{datetime.now(WAT):%Y%m%d%H%M%S}|{secrets.randbelow(10**6):06d}"
    TXNS[ref] = {"transactionReference": ref, "paymentReference": p["paymentReference"], "amount": p["amount"],
                 "redirectUrl": p["redirectUrl"], "customerEmail": p["customerEmail"],
                 "customerName": p.get("customerName", ""), "status": "PENDING",
                 "createdOn": datetime.now(WAT).strftime("%Y-%m-%d %H:%M:%S.000"), "metaData": p.get("metadata", {})}
    return ok({"transactionReference": ref, "paymentReference": p["paymentReference"],
               "merchantName": "TIRS (local test)", "enabledPaymentMethod": ["CARD", "ACCOUNT_TRANSFER", "USSD"],
               "checkoutUrl": f"{PUBLIC}/checkout/{quote(ref, safe='')}"})


def query_body(t):
    paid = t["status"] in ("PAID", "OVERPAID", "PARTIALLY_PAID", "REVERSED")
    amount = t["amount"]
    fee = round(min(amount * 0.016, 2000), 2)
    return {"transactionReference": t["transactionReference"], "paymentReference": t["paymentReference"],
            "amountPaid": f"{(t.get('paid_amount', amount) if paid else 0):.2f}", "totalPayable": f"{amount:.2f}",
            "settlementAmount": f"{(amount - fee) if paid else 0:.2f}", "fee": f"{fee if paid else 0:.2f}",
            "paidOn": t.get("paidOn"), "createdOn": t["createdOn"], "completedOn": t.get("paidOn"),
            "paymentStatus": t["status"], "paymentDescription": "TIRS demand notice", "currencyCode": "NGN",
            "paymentMethod": t.get("method", "CARD"),
            "customerDTO": {"email": t["customerEmail"], "name": t["customerName"]},
            "metaData": t["metaData"],
            "paymentSourceInformation": [{"bankCode": "058", "accountName": "TEST PAYER",
                                          "accountNumber": "0123456789", "amountPaid": f"{amount:.2f}"}] if paid else []}


@app.get("/api/v2/merchant/transactions/query")
def query(request: Request, paymentReference: str = "", transactionReference: str = ""):
    if not authed(request):
        return fail("Unauthorized", 401)
    t = TXNS.get(transactionReference) or next((x for x in TXNS.values() if x["paymentReference"] == paymentReference), None)
    if not t:
        return fail("Transaction not found", 404)
    return ok(query_body(t))


@app.get("/api/v1/merchant/transactions/search")
def search(request: Request, page: int = 0, size: int = 100, paymentStatus: str = ""):
    if not authed(request):
        return fail("Unauthorized", 401)
    items = [query_body(t) for t in TXNS.values() if not paymentStatus or t["status"] == paymentStatus]
    return ok({"content": items[page * size:(page + 1) * size], "totalPages": max(1, -(-len(items) // size)),
               "totalElements": len(items)})


@app.get("/checkout/{ref:path}")
def checkout(ref: str):
    t = TXNS.get(ref)
    if not t:
        return HTMLResponse("Unknown transaction", status_code=404)
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8><title>Local test checkout</title>
<body style="font-family:system-ui;max-width:460px;margin:60px auto">
<p style="background:#fff4d6;padding:10px;border-radius:8px"><b>Local test double</b> standing in for Monnify. No money moves.</p>
<h2>Pay NGN {t['amount']:,.2f}</h2><p>Reference {t['paymentReference']}</p>
<form method=post action="/checkout/{quote(ref, safe='')}/pay"><button id=pay>Pay (test)</button></form>
<form method=post action="/checkout/{quote(ref, safe='')}/cancel"><button id=cancel>Cancel</button></form></body>""")


@app.post("/checkout/{ref:path}/pay")
def pay(ref: str):
    t = TXNS[ref]
    t.update(status="PAID", paidOn=datetime.now(WAT).strftime("%Y-%m-%d %H:%M:%S.000"), method="CARD")
    if WEBHOOK_URL:
        body = json.dumps({"eventType": "SUCCESSFUL_TRANSACTION", "eventData": query_body(t)}).encode()
        headers = {"Content-Type": "application/json"}
        if SIGN_WEBHOOKS:
            headers["monnify-signature"] = hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()
        try:
            httpx.post(WEBHOOK_URL, content=body, headers=headers, timeout=10)
        except httpx.HTTPError:
            pass
    q = urlencode({"paymentReference": t["paymentReference"], "transactionReference": ref, "paymentStatus": "PAID"})
    return RedirectResponse(f"{t['redirectUrl']}?{q}", status_code=303)


@app.post("/checkout/{ref:path}/cancel")
def cancel(ref: str):
    t = TXNS[ref]
    return RedirectResponse(f"{t['redirectUrl']}?{urlencode({'paymentReference': t['paymentReference']})}", status_code=303)


@app.post("/test/reverse/{ref:path}")
def reverse(ref: str):
    TXNS[ref]["status"] = "REVERSED"
    return {"ok": True}
