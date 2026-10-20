"""Monnify REST client.

Endpoints and fields follow developers.monnify.com as recorded in
docs/monnify-integration-plan.md section 1 (with page citations there).
Items the docs left open are marked TO CONFIRM.
"""
import base64
import hashlib
import hmac
import threading
import time
from decimal import Decimal

import httpx


class MonnifyError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def naira(kobo: int) -> Decimal:
    """Monnify amounts are naira with two decimals."""
    return (Decimal(kobo) / 100).quantize(Decimal("0.01"))


def to_kobo(value) -> int:
    if value is None or value == "":
        return 0
    return int((Decimal(str(value)) * 100).quantize(Decimal("1")))


def verify_signature(raw_body: bytes, header: str | None, secret: str) -> bool | None:
    """monnify-signature = hex(HMAC-SHA512(key=secret, msg=raw request body)).

    Returns None when no header was sent (Monnify sends none in sandbox).
    The raw bytes are hashed exactly as received: never re-serialised JSON.
    TO CONFIRM with Monnify which secret signs production webhooks."""
    if not header:
        return None
    if not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected.lower(), header.strip().lower())


class Monnify:
    def __init__(self, base_url: str, api_key: str, secret_key: str, contract_code: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 25.0):
        self.contract_code = contract_code
        self._basic = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                    headers={"Accept": "application/json"})
        self._token = None
        self._token_expires = 0.0
        self._lock = threading.Lock()

    # -- auth ---------------------------------------------------------------
    def _login(self) -> str:
        r = self._client.post("/api/v1/auth/login", headers={"Authorization": f"Basic {self._basic}"})
        body = self._unwrap(r, "authentication")
        token = body.get("accessToken")
        if not token:
            raise MonnifyError("Monnify did not return an access token", r.status_code, r.text)
        expires_in = int(body.get("expiresIn") or 3600)
        self._token = token
        self._token_expires = time.time() + max(60, expires_in - 300)  # refresh 5 min early
        return token

    def _bearer(self, force=False) -> str:
        with self._lock:
            if force or not self._token or time.time() >= self._token_expires:
                return self._login()
            return self._token

    def _call(self, method: str, path: str, **kw):
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._bearer(force=attempt == 1)}"}
            r = self._client.request(method, path, headers=headers, **kw)
            if r.status_code == 401 and attempt == 0:
                continue  # token expired early: log in again once
            return self._unwrap(r, path)
        raise MonnifyError("unreachable")

    @staticmethod
    def _unwrap(r: httpx.Response, what: str):
        try:
            data = r.json()
        except ValueError:
            raise MonnifyError(f"Monnify {what}: non-JSON response (HTTP {r.status_code})", r.status_code, r.text)
        if r.status_code >= 400 or not data.get("requestSuccessful", False):
            msg = data.get("responseMessage") or f"HTTP {r.status_code}"
            raise MonnifyError(f"Monnify {what}: {msg}", r.status_code, data)
        return data.get("responseBody") or {}

    # -- collections --------------------------------------------------------
    def init_transaction(self, *, amount_kobo: int, payment_reference: str, description: str,
                         customer_email: str, customer_name: str, redirect_url: str,
                         metadata: dict | None = None) -> dict:
        amount = naira(amount_kobo)
        payload = {
            "amount": float(amount),
            "customerName": customer_name,
            "customerEmail": customer_email,
            "paymentReference": payment_reference,
            "paymentDescription": description,
            "currencyCode": "NGN",
            "contractCode": self.contract_code,
            "redirectUrl": redirect_url,
            "metadata": metadata or {},
        }
        body = self._call("POST", "/api/v1/merchant/transactions/init-transaction", json=payload)
        # The docs warn that values can be altered in transit: check the echo.
        if body.get("paymentReference") != payment_reference:
            raise MonnifyError("Monnify returned a different payment reference", body=body)
        if "amount" in body and to_kobo(body["amount"]) != amount_kobo:
            raise MonnifyError("Monnify returned a different amount", body=body)
        if not body.get("checkoutUrl") or not body.get("transactionReference"):
            raise MonnifyError("Monnify did not return a checkout URL", body=body)
        return body

    def query(self, *, payment_reference: str | None = None, transaction_reference: str | None = None) -> dict:
        if payment_reference:
            params = {"paymentReference": payment_reference}
        elif transaction_reference:
            params = {"transactionReference": transaction_reference}
        else:
            raise ValueError("a reference is required")
        return self._call("GET", "/api/v2/merchant/transactions/query", params=params)

    def search(self, *, from_ms: int, to_ms: int, status: str | None = None, page: int = 0, size: int = 100) -> dict:
        params = {"page": page, "size": size, "from": from_ms, "to": to_ms}
        if status:
            params["paymentStatus"] = status
        return self._call("GET", "/api/v1/merchant/transactions/search", params=params)

    def close(self):
        self._client.close()
