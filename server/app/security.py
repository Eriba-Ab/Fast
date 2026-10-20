"""Client IPs, hashed IPs, rate limits on misses, signed receipt tokens."""
import hashlib
import hmac
from datetime import datetime, timezone

from fastapi import Request

from . import db

# Limits on FAILED public lookups per hashed IP (a real taxpayer succeeds in one or two tries).
MISS_LIMIT_MINUTE = 10
MISS_LIMIT_HOUR = 50
# Limits on payment starts per hashed IP.
PAY_START_LIMIT_HOUR = 20
# Global circuit breaker on misses across all IPs.
GLOBAL_MISS_LIMIT_MINUTE = 600


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers behind Caddy, so request.client is the real client
    return request.client.host if request.client else "0.0.0.0"


def ip_hash(secret: str, ip: str) -> str:
    """Daily-salted hash: lets us rate-limit and audit without storing IP addresses."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hmac.new(secret.encode(), f"{day}|{ip}".encode(), hashlib.sha256).hexdigest()[:32]


def record_lookup(conn, iphash: str, kind: str, hit: bool) -> None:
    conn.execute("INSERT INTO lookup_audit (ip_hash, kind, hit) VALUES (%s, %s, %s)", (iphash, kind, hit))


def lookup_blocked(conn, iphash: str) -> bool:
    row = conn.execute("""
        SELECT count(*) FILTER (WHERE at > now() - interval '1 minute') AS m,
               count(*) AS h
        FROM lookup_audit WHERE ip_hash = %s AND kind = 'notice' AND NOT hit AND at > now() - interval '1 hour'""",
                       (iphash,)).fetchone()
    if row["m"] >= MISS_LIMIT_MINUTE or row["h"] >= MISS_LIMIT_HOUR:
        return True
    g = conn.execute("""SELECT count(*) AS n FROM lookup_audit
                        WHERE kind = 'notice' AND NOT hit AND at > now() - interval '1 minute'""").fetchone()
    return g["n"] >= GLOBAL_MISS_LIMIT_MINUTE


def pay_start_blocked(conn, iphash: str) -> bool:
    n = conn.execute("""SELECT count(*) AS n FROM lookup_audit
                        WHERE ip_hash = %s AND kind = 'pay_start' AND at > now() - interval '1 hour'""",
                     (iphash,)).fetchone()["n"]
    return n >= PAY_START_LIMIT_HOUR


def receipt_token(secret: str, transaction_reference: str) -> str:
    return hmac.new(secret.encode(), f"receipt|{transaction_reference}".encode(), hashlib.sha256).hexdigest()[:32]


def receipt_token_ok(secret: str, transaction_reference: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(receipt_token(secret, transaction_reference), token)


def prune_audit() -> None:
    """Keep 90 days of lookup audit."""
    with db.tx() as conn:
        conn.execute("DELETE FROM lookup_audit WHERE at < now() - interval '90 days'")
