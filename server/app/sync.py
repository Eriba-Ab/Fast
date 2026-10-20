"""Load the assessments export (written by build.py) into PostgreSQL.

Safety rule: a demand notice that already has payments recorded against it must
keep its number. If a new workbook would drop or renumber such a notice, the sync
stops and changes nothing, rather than orphaning money."""
import gzip
import json
from datetime import date

from . import db


class SyncError(Exception):
    pass


def load_export(path: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    for key in ("taxYear", "assessmentDate", "dueDate", "months", "heads", "feeHeads", "rows", "source"):
        if key not in data:
            raise SyncError(f"assessments export is missing '{key}'")
    return data


def sync(path: str, zero_collections: bool = False) -> dict:
    """zero_collections: sandbox testing only, loads the notices without the 2025
    collections so that there are balances to pay."""
    data = load_export(path)
    rows = data["rows"]
    year = int(data["taxYear"])
    new_dns = {r["dn"] for r in rows}
    reg_to_dn = {r["reg_id"]: r["dn"] for r in rows}
    if len(new_dns) != len(rows):
        raise SyncError("the export contains duplicate notice numbers")

    with db.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4712)")
        paid = conn.execute("""SELECT DISTINCT a.dn, a.reg_id FROM assessments a
                               JOIN payment_intents i ON i.dn = a.dn WHERE a.tax_year = %s""", (year,)).fetchall()
        problems = []
        for p in paid:
            if p["dn"] not in new_dns:
                moved = reg_to_dn.get(p["reg_id"])
                problems.append(f"{p['dn']} ({p['reg_id']}) has payment records but "
                                + (f"would be renumbered to {moved}" if moved else "is missing from the new workbook"))
        if problems:
            raise SyncError("Sync refused, nothing changed:\n  " + "\n  ".join(problems[:20])
                            + (f"\n  ...and {len(problems) - 20} more" if len(problems) > 20 else ""))

        conn.execute("""INSERT INTO meta (key, value) VALUES ('workbook', %s::jsonb)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                     (json.dumps({k: data[k] for k in ("source", "taxYear", "assessmentDate", "dueDate",
                                                       "months", "heads", "feeHeads")}),))
        a_date, d_date = date.fromisoformat(data["assessmentDate"]), date.fromisoformat(data["dueDate"])
        # notices no longer in the workbook (and with no payments, checked above) go first,
        # so a business that moved to a new number does not collide with its old row
        conn.execute("""DELETE FROM assessments WHERE tax_year = %s AND NOT (dn = ANY(%s))
                        AND NOT EXISTS (SELECT 1 FROM payment_intents i WHERE i.dn = assessments.dn)""",
                     (year, list(new_dns)))
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO assessments (dn, tax_year, reg_id, ttin, name, entity_type, category, lga, business_size,
                  address, phone, heads_kobo, subtotal_fees_kobo, total_assessed_kobo, assessment_date, due_date,
                  source_workbook, synced_at)
                VALUES (%(dn)s, %(year)s, %(reg_id)s, %(ttin)s, %(name)s, %(entity)s, %(category)s, %(lga)s, %(size)s,
                  %(address)s, %(phone)s, %(heads)s::jsonb, %(subtotal)s, %(total)s, %(a_date)s, %(d_date)s,
                  %(source)s, now())
                ON CONFLICT (dn) DO UPDATE SET reg_id = EXCLUDED.reg_id, ttin = EXCLUDED.ttin, name = EXCLUDED.name,
                  entity_type = EXCLUDED.entity_type, category = EXCLUDED.category, lga = EXCLUDED.lga,
                  business_size = EXCLUDED.business_size, address = EXCLUDED.address, phone = EXCLUDED.phone,
                  heads_kobo = EXCLUDED.heads_kobo, subtotal_fees_kobo = EXCLUDED.subtotal_fees_kobo,
                  total_assessed_kobo = EXCLUDED.total_assessed_kobo, assessment_date = EXCLUDED.assessment_date,
                  due_date = EXCLUDED.due_date, source_workbook = EXCLUDED.source_workbook, synced_at = now()""",
                [{"dn": r["dn"], "year": year, "reg_id": r["reg_id"], "ttin": r.get("ttin") or None,
                  "name": r["name"], "entity": r["entity"], "category": r["category"], "lga": r["lga"],
                  "size": r["size"], "address": r["address"], "phone": r.get("phone") or None,
                  "heads": json.dumps(r["heads"]), "subtotal": r["subtotal"], "total": r["total"],
                  "a_date": a_date, "d_date": d_date, "source": data["source"]} for r in rows])

            conn.execute("""DELETE FROM legacy_collections WHERE dn IN
                            (SELECT dn FROM assessments WHERE tax_year = %s)""", (year,))
            periods = [date.fromisoformat(m + "-01") for m in data["months"]]
            cur.executemany("""INSERT INTO legacy_collections (dn, period, amount_kobo, source)
                               VALUES (%s, %s, %s, %s)""",
                            [] if zero_collections else
                            [(r["dn"], periods[i], amt, data["source"])
                             for r in rows for i, amt in enumerate(r["pay"]) if amt])
        count = conn.execute("SELECT count(*) AS n FROM assessments WHERE tax_year = %s", (year,)).fetchone()["n"]
    return {"notices": count, "tax_year": year, "source": data["source"],
            "collections": "omitted (sandbox test data)" if zero_collections else "loaded"}
