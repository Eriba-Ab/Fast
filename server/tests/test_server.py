import gzip
import json

import pytest

from app import db, ledger, security, sync, worker
from app.monnify import verify_signature
from tests.conftest import OTHER_DN, SETTLED_DN, TMP, UNPAID_DN, make_export, paid_body

TOTAL = 1_500_000  # kobo, from conftest heads


# ---- webhook signature ---------------------------------------------------
def test_signature():
    import hashlib, hmac
    body = b'{"eventType":"SUCCESSFUL_TRANSACTION","eventData":{"a":1}}'
    good = hmac.new(b"secret", body, hashlib.sha512).hexdigest()
    assert verify_signature(body, good, "secret") is True
    assert verify_signature(body, good.upper(), "secret") is True
    assert verify_signature(body + b" ", good, "secret") is False      # raw bytes, not re-serialised JSON
    assert verify_signature(body, "deadbeef", "secret") is False
    assert verify_signature(body, None, "secret") is None               # sandbox: no header


# ---- public lookup -----------------------------------------------------------
def test_lookup_by_dn_and_ttin_never_returns_phone(client):
    r = client.get(f"/api/notice?q={UNPAID_DN}")
    assert r.status_code == 200
    body = r.json()
    assert body["dn"] == UNPAID_DN and body["balance_kobo"] == TOTAL and not body["settled"]
    assert "phone" not in json.dumps(body).lower() and "0803" not in json.dumps(body)
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/notice?q=ttin20250000001").json()["dn"] == UNPAID_DN


def test_lookup_rejects_registration_ids_and_partial_input_uniformly(client):
    msgs = {client.get(f"/api/notice?q={q}").json()["error"]
            for q in ["TR/MSME/2025/00001", "Business", "DN2025", "DN999999999999999"]}
    assert len(msgs) == 1                                   # one message for every kind of miss


def test_lookup_rate_limits_misses(client):
    for i in range(security.MISS_LIMIT_MINUTE):
        assert client.get(f"/api/notice?q=DN99999999999{i:04d}").status_code == 404
    assert client.get(f"/api/notice?q={UNPAID_DN}").status_code == 429


def test_settled_2025_notice(client):
    body = client.get(f"/api/notice?q={SETTLED_DN}").json()
    assert body["settled"] and body["legacy_kobo"] == TOTAL and body["live_kobo"] == 0
    assert client.post("/api/pay/start", json={"q": SETTLED_DN}).status_code == 409


# ---- payment flow --------------------------------------------------------------
def test_pay_start_uses_server_amount_and_reuses_open_checkout(client):
    r = client.post("/api/pay/start", json={"q": UNPAID_DN, "amount": 1})      # a client amount is ignored
    assert r.status_code == 200 and r.json()["checkoutUrl"].startswith("https://")
    sent = client.calls["init"][0]
    assert sent["amount"] == TOTAL / 100 and sent["paymentReference"] == f"TIRS-{UNPAID_DN}-1"
    assert sent["customerEmail"].endswith("@notices.testserver")
    again = client.post("/api/pay/start", json={"q": UNPAID_DN})
    assert again.json()["checkoutUrl"] == r.json()["checkoutUrl"] and len(client.calls["init"]) == 1


def test_pay_start_rejects_tampered_echo(client):
    client.calls["init_override"] = {"paymentReference": "TIRS-SOMETHING-ELSE"}
    r = client.post("/api/pay/start", json={"q": UNPAID_DN})
    assert r.status_code == 502
    with db.tx() as conn:
        assert conn.execute("SELECT status FROM payment_intents").fetchone()["status"] == "FAILED"


def test_return_verifies_with_monnify_before_recording(client):
    client.post("/api/pay/start", json={"q": UNPAID_DN})
    ref = f"TIRS-{UNPAID_DN}-1"
    # the browser claims PAID, but Monnify says PENDING: nothing is recorded
    client.calls["query"][ref] = dict(paid_body(ref, "MNFY|1|1", TOTAL), paymentStatus="PENDING", amountPaid="0")
    r = client.get(f"/api/pay/return/{ref}?paymentStatus=PAID", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("result=pending")
    assert client.get(f"/api/notice?q={UNPAID_DN}").json()["balance_kobo"] == TOTAL
    # now Monnify confirms
    client.calls["query"][ref] = paid_body(ref, "MNFY|1|1", TOTAL)
    r = client.get(f"/api/pay/return/{ref}", follow_redirects=False)
    assert r.headers["location"] == f"/#/pay/{UNPAID_DN}?result=paid"
    n = client.get(f"/api/notice?q={UNPAID_DN}").json()
    assert n["settled"] and n["live_kobo"] == TOTAL and len(n["payments"]) == 1
    p = n["payments"][0]
    rc = client.get(f"/api/receipt?ref={p['transaction_reference']}&t={p['receipt_token']}")
    assert rc.status_code == 200 and rc.json()["receipt"]["amount_kobo"] == TOTAL
    assert client.get(f"/api/receipt?ref={p['transaction_reference']}&t=forged").status_code == 404
    assert client.post("/api/pay/start", json={"q": UNPAID_DN}).status_code == 409    # nothing left to pay


def test_webhook_is_idempotent_and_verified(client):
    client.post("/api/pay/start", json={"q": OTHER_DN})
    ref, txn = f"TIRS-{OTHER_DN}-1", "MNFY|1|1"
    client.calls["query"][txn] = paid_body(ref, txn, TOTAL)
    event = {"eventType": "SUCCESSFUL_TRANSACTION", "eventData": paid_body(ref, txn, TOTAL)}
    for _ in range(3):                                          # Monnify may resend
        assert client.post("/api/monnify/webhook", content=json.dumps(event)).status_code == 200
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM webhook_events").fetchone()["n"] == 1
    assert worker.process_webhooks(client.mock) == 1
    assert worker.process_webhooks(client.mock) == 0
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM payments").fetchone()["n"] == 1
    assert client.get(f"/api/notice?q={OTHER_DN}").json()["settled"]


def test_webhook_body_alone_never_records_a_payment(client):
    client.post("/api/pay/start", json={"q": OTHER_DN})
    ref, txn = f"TIRS-{OTHER_DN}-1", "MNFY|1|1"
    # a forged webhook claims PAID; Monnify's API does not know the transaction
    event = {"eventType": "SUCCESSFUL_TRANSACTION", "eventData": paid_body(ref, txn, TOTAL)}
    client.post("/api/monnify/webhook", content=json.dumps(event))
    worker.process_webhooks(client.mock)
    assert client.get(f"/api/notice?q={OTHER_DN}").json()["balance_kobo"] == TOTAL


def test_reversal_reopens_balance_and_voids_receipt(client):
    client.post("/api/pay/start", json={"q": UNPAID_DN})
    ref, txn = f"TIRS-{UNPAID_DN}-1", "MNFY|1|1"
    client.calls["query"][ref] = paid_body(ref, txn, TOTAL)
    client.get(f"/api/pay/return/{ref}", follow_redirects=False)
    token = client.get(f"/api/notice?q={UNPAID_DN}").json()["payments"][0]["receipt_token"]
    client.calls["query"][txn] = paid_body(ref, txn, TOTAL, status="REVERSED")
    assert ledger.verify_and_record(client.mock, source="recon", transaction_reference=txn)["result"] == "recorded"
    n = client.get(f"/api/notice?q={UNPAID_DN}").json()
    assert not n["settled"] and n["balance_kobo"] == TOTAL
    assert client.get(f"/api/receipt?ref={txn}&t={token}").status_code == 404
    assert client.get(f"/api/verify?ref={txn}").json()["valid"] is False


def test_payments_disabled_gate(client, monkeypatch):
    from dataclasses import replace
    s = client.app.state.settings
    monkeypatch.setattr(client.app.state, "settings", replace(s, payments_enabled=False))
    assert client.post("/api/pay/start", json={"q": UNPAID_DN}).status_code == 503
    assert client.get(f"/api/notice?q={UNPAID_DN}").json()["payments_enabled"] is False


# ---- data sync safety ------------------------------------------------------------
def test_sync_refuses_to_orphan_payments(client):
    client.post("/api/pay/start", json={"q": UNPAID_DN})
    path = TMP / "renumbered.json.gz"
    export = make_export(path)
    export["rows"][0]["dn"] = "DN202520000199999"           # same business, new number
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(export, f)
    with pytest.raises(sync.SyncError, match="renumbered"):
        sync.sync(str(path))
    assert client.get(f"/api/notice?q={UNPAID_DN}").status_code == 200   # nothing changed


# ---- staff console -----------------------------------------------------------------
def test_staff_console_requires_sign_in(client):
    from app import staff
    with db.tx() as conn:
        conn.execute("INSERT INTO staff_users (username, full_name, password_hash) VALUES ('officer', 'Test Officer', %s)",
                     (staff.hash_password("correct horse battery"),))
    r = client.get("/staff/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/staff/login"
    assert "Wrong username or password" in client.post("/staff/login", data={"username": "officer", "password": "nope"}).text
    r = client.post("/staff/login", data={"username": "officer", "password": "correct horse battery"}, follow_redirects=False)
    assert r.status_code == 303 and "tirs_staff" in r.cookies
    page = client.get("/staff/")
    assert "STAFF CONSOLE" in page.text and "Signed in as" in page.text
    client.post("/staff/logout", follow_redirects=False)
    assert client.get("/staff/", follow_redirects=False).status_code == 303
    with db.tx() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["login_failed", "login", "view_console", "logout"]


def test_staff_lockout(client):
    for _ in range(5):
        client.post("/staff/login", data={"username": "someone", "password": "x"})
    assert "Too many failed attempts" in client.post("/staff/login", data={"username": "someone", "password": "x"}).text
