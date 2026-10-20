"""Background worker (its own container): turns webhooks into verified ledger rows,
re-checks unfinished checkouts, and reconciles with Monnify every night.

Run: python -m app.worker"""
import json
import logging
import select
import time
from datetime import date, datetime, timedelta

import psycopg

from . import config, db, ledger, security
from .monnify import Monnify, MonnifyError, to_kobo

log = logging.getLogger("tirs.worker")
RECON_AT = (23, 10)          # 23:10 WAT, after Monnify's 22:00 settlement cycle
SWEEP_EVERY = 300            # seconds between checks of unfinished checkouts


def process_webhooks(client: Monnify, limit: int = 50) -> int:
    with db.tx() as conn:
        events = conn.execute("""SELECT id, event_type, transaction_reference, payment_reference FROM webhook_events
                                 WHERE processed_at IS NULL AND attempts < 10
                                 ORDER BY received_at LIMIT %s FOR UPDATE SKIP LOCKED""", (limit,)).fetchall()
        ids = [e["id"] for e in events]
        if ids:
            conn.execute("UPDATE webhook_events SET attempts = attempts + 1 WHERE id = ANY(%s)", (ids,))
    done = 0
    for e in events:
        if e["event_type"] in ("SUCCESSFUL_TRANSACTION", "REJECTED_PAYMENT") and (e["transaction_reference"] or e["payment_reference"]):
            res = ledger.verify_and_record(client, source="webhook",
                                           transaction_reference=e["transaction_reference"],
                                           payment_reference=None if e["transaction_reference"] else e["payment_reference"])
            if res.get("result") == "query_failed":
                with db.tx() as conn:
                    conn.execute("UPDATE webhook_events SET process_result = %s WHERE id = %s",
                                 (res["error"][:500], e["id"]))
                continue        # retried on the next pass (up to 10 attempts)
            outcome = res.get("result", "")
        else:
            outcome = "no_action"   # settlement, refund and other events are stored for audit only
        with db.tx() as conn:
            conn.execute("UPDATE webhook_events SET processed_at = now(), process_result = %s WHERE id = %s",
                         (outcome, e["id"]))
        done += 1
    return done


def sweep_intents(client: Monnify) -> int:
    """Catch payments whose webhook never arrived: ask Monnify about recent checkouts."""
    with db.tx() as conn:
        rows = conn.execute("""SELECT payment_reference, expires_at FROM payment_intents
            WHERE status = 'CREATED' AND transaction_reference IS NOT NULL
              AND created_at < now() - interval '3 minutes' AND created_at > now() - interval '2 days'
              AND (last_checked_at IS NULL OR last_checked_at < now() - interval '10 minutes')
            ORDER BY created_at LIMIT 100""").fetchall()
    for r in rows:
        res = ledger.verify_and_record(client, source="sweep", payment_reference=r["payment_reference"])
        with db.tx() as conn:
            conn.execute("UPDATE payment_intents SET last_checked_at = now() WHERE payment_reference = %s",
                         (r["payment_reference"],))
            if res.get("result") == "not_paid":
                conn.execute("""UPDATE payment_intents SET status = 'ABANDONED'
                                WHERE payment_reference = %s AND status = 'CREATED'
                                  AND expires_at < now() - interval '1 hour'""", (r["payment_reference"],))
    return len(rows)


def reconcile(client: Monnify, day: date) -> dict:
    """Compare one day's Monnify transactions with the ledger, both ways."""
    start = datetime(day.year, day.month, day.day, tzinfo=ledger.WAT)
    from_ms, to_ms = int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)
    seen, counts, problems, settlement = {}, {}, [], 0
    for status in ("PAID", "PARTIALLY_PAID", "OVERPAID", "REVERSED"):
        page = 0
        while True:
            body = client.search(from_ms=from_ms, to_ms=to_ms, status=status, page=page, size=100)
            items = body.get("content") or []
            for t in items:
                ref = t.get("transactionReference")
                if ref:
                    seen[ref] = t
                    settlement += to_kobo(t.get("settlementAmount"))
            counts[status] = counts.get(status, 0) + len(items)
            page += 1
            if page >= int(body.get("totalPages") or 0) or not items:
                break
    with db.tx() as conn:
        ours = {r["transaction_reference"]: r for r in conn.execute(
            """SELECT transaction_reference, amount_paid_kobo, payment_status FROM payments
               WHERE paid_on >= %s AND paid_on < %s""", (start, start + timedelta(days=1))).fetchall()}
    for ref, t in seen.items():
        if not ledger.REF_PATTERN.match(t.get("paymentReference") or ""):
            continue                 # another product on the same Monnify account
        if ref not in ours:
            res = ledger.verify_and_record(client, source="recon", transaction_reference=ref)
            problems.append({"type": "MISSING_IN_DB", "transaction_reference": ref, "action": res.get("result")})
        elif ours[ref]["amount_paid_kobo"] != to_kobo(t.get("amountPaid")):
            problems.append({"type": "AMOUNT_MISMATCH", "transaction_reference": ref,
                             "ledger_kobo": ours[ref]["amount_paid_kobo"], "monnify_kobo": to_kobo(t.get("amountPaid"))})
    for ref in ours:
        if ref not in seen:
            problems.append({"type": "MISSING_AT_MONNIFY", "transaction_reference": ref})
    with db.tx() as conn:
        conn.execute("""INSERT INTO recon_runs (ran_for, counts, discrepancies, expected_settlement_kobo)
                        VALUES (%s, %s::jsonb, %s::jsonb, %s)""",
                     (day, json.dumps(counts), json.dumps(problems), settlement))
    log.info("reconciliation for %s: %s transactions, %s discrepancies", day, sum(counts.values()), len(problems))
    return {"day": day.isoformat(), "counts": counts, "discrepancies": problems}


def _recon_due(last_done: date | None) -> date | None:
    now = datetime.now(ledger.WAT)
    target = now.date() if (now.hour, now.minute) >= RECON_AT else now.date() - timedelta(days=1)
    return target if last_done != target else None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.load()
    db.init(s.database_url, max_size=4)
    db.apply_schema()
    if not s.monnify_configured:
        log.warning("Monnify credentials are not set; the worker will idle until they are.")
    client = Monnify(s.monnify_base_url, s.monnify_api_key, s.monnify_secret_key, s.monnify_contract_code)
    listen = psycopg.connect(s.database_url, autocommit=True)
    listen.execute("LISTEN tirs_webhook")
    with db.tx() as conn:
        last = conn.execute("SELECT max(ran_for) AS d FROM recon_runs").fetchone()["d"]
    last_sweep = recon_retry_at = 0.0
    log.info("worker started (%s)", s.monnify_env)
    while True:
        try:
            if s.monnify_configured:
                while process_webhooks(client):
                    pass
                if time.time() - last_sweep > SWEEP_EVERY:
                    sweep_intents(client)
                    security.prune_audit()
                    last_sweep = time.time()
                due = _recon_due(last)
                if due and time.time() >= recon_retry_at:
                    try:
                        reconcile(client, due)
                        last = due
                    except MonnifyError as e:
                        recon_retry_at = time.time() + 1800      # try again in 30 minutes
                        log.error("reconciliation for %s failed: %s", due, e)
        except Exception:
            log.exception("worker loop error")
        # sleep until a webhook arrives (NOTIFY) or 30 s pass
        if select.select([listen.fileno()], [], [], 30)[0]:
            for _ in listen.notifies(timeout=0):
                pass


if __name__ == "__main__":
    main()
