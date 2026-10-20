"""Test set-up: a throwaway database, a tiny assessments export, and a mocked Monnify."""
import gzip
import json
import os
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://tirs:localtest@localhost:55432/tirs_test")
TMP = Path(os.environ.get("TEST_TMP", "/tmp/tirs-tests"))
TMP.mkdir(parents=True, exist_ok=True)

STAFF_FILE = TMP / "staff.html"
STAFF_FILE.write_text("<html><body><h1>STAFF CONSOLE</h1></body></html>", encoding="utf-8")

# settings are read from the environment when the app starts
os.environ.update({
    "DATABASE_URL": TEST_DB,
    "APP_SECRET": "test-secret-0123456789abcdef0123456789abcdef",
    "PUBLIC_BASE_URL": "http://testserver",
    "PAYMENTS_ENABLED": "true",
    "MONNIFY_ENV": "sandbox",
    "MONNIFY_API_KEY": "MK_TEST",
    "MONNIFY_SECRET_KEY": "SK_TEST",
    "MONNIFY_CONTRACT_CODE": "123",
    "MONNIFY_BASE_URL": "",
    "COOKIE_SECURE": "false",
    "STAFF_FILE": str(STAFF_FILE),
    "SITE_DIR": str(TMP),
})

HEADS = ["Presumptive Turnover Tax", "Business Premises Registration", "Development Levy",
         "Environmental Sanitation Levy", "Waste Collection Fee", "Fire Safety Certificate",
         "Produce & Commodity Market Fee", "Licences", "Rent on Government Property", "Rent on Land"]
UNPAID_DN = "DN202520000100001"
SETTLED_DN = "DN202520000200002"
OTHER_DN = "DN202510000300003"


def make_export(path: Path, rows=None):
    def row(dn, reg, ttin, pay):
        heads = [1_000_000, 200_000, 50_000, 100_000, 80_000, 70_000, 0, 0, 0, 0]
        return {"dn": dn, "reg_id": reg, "ttin": ttin, "name": f"Business {reg[-1]}", "entity": "MSME",
                "category": "Wholesale, Retail & Distribution", "lga": "Jalingo", "size": "Nano",
                "address": "No. 1 Hammaruwa Way, Jalingo, Taraba State", "phone": "08030000000",
                "heads": heads, "subtotal": sum(heads[:7]), "total": sum(heads), "pay": pay}
    if rows is None:
        rows = [row(UNPAID_DN, "TR/MSME/2025/00001", "TTIN20250000001", [0] * 12),
                row(SETTLED_DN, "TR/MSME/2025/00002", "TTIN20250000002", [125_000] * 12),
                row(OTHER_DN, "TR/INST/2025/00003", "TTIN20250000003", [0] * 12)]
    export = {"source": "test.xlsx", "built": "2026-10-20", "taxYear": 2025, "assessmentDate": "2025-12-23",
              "dueDate": "2026-01-22", "months": [f"2025-{m:02d}" for m in range(1, 13)], "heads": HEADS,
              "feeHeads": 7, "rows": rows}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(export, f)
    return export


@pytest.fixture(scope="session", autouse=True)
def database():
    admin = TEST_DB.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS tirs_test WITH (FORCE)")
        c.execute("CREATE DATABASE tirs_test")
    yield


@pytest.fixture()
def client(database):
    from fastapi.testclient import TestClient
    from app import db, main, sync
    from app.monnify import Monnify
    import httpx

    calls = {"init": [], "query": {}, "init_override": None}

    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/api/v1/auth/login":
            return httpx.Response(200, json={"requestSuccessful": True, "responseBody": {"accessToken": "t", "expiresIn": 3600}})
        if path.endswith("/init-transaction"):
            body = json.loads(request.content)
            calls["init"].append(body)
            resp = {"transactionReference": f"MNFY|1|{len(calls['init'])}", "paymentReference": body["paymentReference"],
                    "checkoutUrl": f"https://sandbox.sdk.monnify.com/checkout/MNFY|1|{len(calls['init'])}"}
            if calls["init_override"]:
                resp.update(calls["init_override"])
            return httpx.Response(200, json={"requestSuccessful": True, "responseBody": resp})
        if path.endswith("/transactions/query"):
            key = request.url.params.get("transactionReference") or request.url.params.get("paymentReference")
            body = calls["query"].get(key)
            if body is None:
                return httpx.Response(404, json={"requestSuccessful": False, "responseMessage": "not found"})
            return httpx.Response(200, json={"requestSuccessful": True, "responseBody": body})
        return httpx.Response(404, json={"requestSuccessful": False})

    with TestClient(main.app) as c:
        with db.tx() as conn:
            conn.execute("""TRUNCATE payment_events, payments, webhook_events, payment_intents, legacy_collections,
                            assessments, meta, lookup_audit, recon_runs, audit_log, staff_sessions, staff_users
                            RESTART IDENTITY CASCADE""")
        export = TMP / "assessments.json.gz"
        make_export(export)
        sync.sync(str(export))
        mock = Monnify("https://sandbox.monnify.com", "MK_TEST", "SK_TEST", "123", transport=httpx.MockTransport(handler))
        main.app.state.monnify = lambda: mock
        c.calls = calls
        c.mock = mock
        yield c


def paid_body(pay_ref, txn_ref, amount_kobo, status="PAID"):
    return {"transactionReference": txn_ref, "paymentReference": pay_ref, "amountPaid": f"{amount_kobo / 100:.2f}",
            "totalPayable": f"{amount_kobo / 100:.2f}", "settlementAmount": f"{amount_kobo / 100 - 100:.2f}",
            "fee": "100.00", "paidOn": "2026-10-20 12:30:00.000", "paymentStatus": status, "paymentMethod": "CARD",
            "currencyCode": "NGN", "paymentSourceInformation": [{"accountName": "TEST", "accountNumber": "0123456789"}]}
