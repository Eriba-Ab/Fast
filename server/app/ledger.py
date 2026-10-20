"""The payment ledger.

Core rule: nothing is recorded as paid except by record_verified(), and its only
input is a transaction-status response that this server fetched from Monnify
itself. Redirect parameters and webhook bodies are triggers, never evidence.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from . import db
from .monnify import Monnify, MonnifyError, to_kobo

WAT = timezone(timedelta(hours=1))           # West Africa Time (Lagos), no DST
COUNTED = ("PAID", "OVERPAID", "PARTIALLY_PAID")
RECORDED = COUNTED + ("REVERSED",)
REF_PATTERN = re.compile(r"^TIRS-(DN\d{15})-(\d+)$")
LEGACY_TOLERANCE_KOBO = 5                    # the workbook rounds some line items


def payment_reference(dn: str, attempt: int) -> str:
    return f"TIRS-{dn}-{attempt}"


def parse_paid_on(value) -> datetime | None:
    """Monnify dates arrive as 'YYYY-MM-DD HH:MM:SS(.fff)' or ISO; naive values are
    treated as West Africa Time (TO CONFIRM against a live payload)."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %I:%M:%S %p", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=WAT)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=WAT)
    except ValueError:
        return None


def notice_status(conn, dn: str) -> dict | None:
    """Assessed, collected (2025 return + verified Monnify payments) and balance."""
    a = conn.execute("SELECT * FROM assessments WHERE dn = %s", (dn,)).fetchone()
    if not a:
        return None
    legacy = conn.execute("SELECT COALESCE(SUM(amount_kobo),0) AS s FROM legacy_collections WHERE dn = %s",
                          (dn,)).fetchone()["s"]
    live = conn.execute(
        "SELECT COALESCE(SUM(amount_paid_kobo),0) AS s FROM payments WHERE dn = %s AND payment_status = ANY(%s)",
        (dn, list(COUNTED))).fetchone()["s"]
    total = a["total_assessed_kobo"]
    raw_balance = total - legacy - live
    # the rounding tolerance applies to the 2025 return only; live balances are exact
    balance = 0 if (live == 0 and 0 < raw_balance <= LEGACY_TOLERANCE_KOBO) else max(0, raw_balance)
    return {"assessment": a, "legacy_kobo": int(legacy), "live_kobo": int(live),
            "total_kobo": int(total), "balance_kobo": int(balance),
            "settled": balance == 0}


def record_verified(conn, verified: dict, source: str) -> dict:
    """Write one Monnify transaction (as returned by OUR query call) to the ledger.

    Idempotent: replaying the same transaction changes nothing unless its status
    changed (for example PAID -> REVERSED)."""
    pay_ref = verified.get("paymentReference") or ""
    txn_ref = verified.get("transactionReference") or ""
    status = (verified.get("paymentStatus") or "").upper()
    intent = conn.execute("SELECT * FROM payment_intents WHERE payment_reference = %s FOR UPDATE",
                          (pay_ref,)).fetchone()
    if not intent:
        return {"result": "not_ours", "payment_reference": pay_ref}
    if not txn_ref:
        return {"result": "no_transaction_reference"}

    if status not in RECORDED:
        # PENDING / FAILED / EXPIRED: note it on the intent, record nothing
        new = {"PENDING": intent["status"], "FAILED": "FAILED", "EXPIRED": "EXPIRED"}.get(status, intent["status"])
        conn.execute("UPDATE payment_intents SET status = %s, last_checked_at = now() WHERE payment_reference = %s",
                     (new, pay_ref))
        return {"result": "not_paid", "status": status}

    amount_paid = to_kobo(verified.get("amountPaid"))
    src = (verified.get("paymentSourceInformation") or [{}])
    src = src[0] if isinstance(src, list) and src else {}
    acct = str(src.get("accountNumber") or "")
    row = {
        "transaction_reference": txn_ref,
        "payment_reference": pay_ref,
        "dn": intent["dn"],
        "payment_status": status,
        "amount_paid_kobo": amount_paid,
        "total_payable_kobo": to_kobo(verified.get("totalPayable")) if verified.get("totalPayable") is not None else None,
        "settlement_amount_kobo": to_kobo(verified.get("settlementAmount")) if verified.get("settlementAmount") is not None else None,
        "fee_kobo": to_kobo(verified.get("fee")) if verified.get("fee") is not None else None,
        "payment_method": verified.get("paymentMethod"),
        "paid_on": parse_paid_on(verified.get("paidOn") or verified.get("completedOn")),
        "payer_account_name": src.get("accountName"),
        "payer_account_last4": acct[-4:] if acct else None,
        "amount_matches": amount_paid >= intent["expected_amount_kobo"],
        "raw_verify": json.dumps(verified),
    }
    before = conn.execute("SELECT payment_status FROM payments WHERE transaction_reference = %s FOR UPDATE",
                          (txn_ref,)).fetchone()
    if before is None:
        conn.execute("""
            INSERT INTO payments (transaction_reference, payment_reference, dn, payment_status, amount_paid_kobo,
              total_payable_kobo, settlement_amount_kobo, fee_kobo, payment_method, paid_on, payer_account_name,
              payer_account_last4, amount_matches, raw_verify)
            VALUES (%(transaction_reference)s, %(payment_reference)s, %(dn)s, %(payment_status)s, %(amount_paid_kobo)s,
              %(total_payable_kobo)s, %(settlement_amount_kobo)s, %(fee_kobo)s, %(payment_method)s, %(paid_on)s,
              %(payer_account_name)s, %(payer_account_last4)s, %(amount_matches)s, %(raw_verify)s::jsonb)
            ON CONFLICT (transaction_reference) DO NOTHING""", row)
        changed, prev = True, None
    elif before["payment_status"] != status:
        conn.execute("""UPDATE payments SET payment_status = %s, raw_verify = %s::jsonb, verified_at = now()
                        WHERE transaction_reference = %s""", (status, row["raw_verify"], txn_ref))
        changed, prev = True, before["payment_status"]
    else:
        changed, prev = False, status

    if changed:
        conn.execute("""INSERT INTO payment_events (transaction_reference, from_status, to_status, source)
                        VALUES (%s, %s, %s, %s)""", (txn_ref, prev, status, source))
    conn.execute("""UPDATE payment_intents SET status = %s, transaction_reference = COALESCE(transaction_reference, %s),
                    last_checked_at = now() WHERE payment_reference = %s""", (status, txn_ref, pay_ref))
    return {"result": "recorded" if changed else "unchanged", "status": status, "dn": intent["dn"],
            "amount_matches": row["amount_matches"]}


def verify_and_record(client: Monnify, *, source: str, payment_reference: str | None = None,
                      transaction_reference: str | None = None) -> dict:
    """Ask Monnify for the truth about one transaction, then record it.
    The network call happens outside any database transaction."""
    try:
        verified = client.query(payment_reference=payment_reference, transaction_reference=transaction_reference)
    except MonnifyError as e:
        return {"result": "query_failed", "error": str(e)}
    with db.tx() as conn:
        return record_verified(conn, verified, source)
