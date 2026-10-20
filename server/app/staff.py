"""Staff console behind per-officer sign-in.

The full registry (taraba-billing.html) is only ever served to a signed-in officer.
Passwords are Argon2id hashes; sessions are random tokens stored hashed; every
sign-in, failure, console view and sign-out is written to audit_log."""
import hashlib
import html
import json
import secrets
from pathlib import Path
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError, InvalidHashError
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import db, security

router = APIRouter()
COOKIE = "tirs_staff"
MAX_FAILURES = 5          # per username or per IP within 15 minutes
_ph = PasswordHasher()
_DUMMY = _ph.hash("not-a-real-password")   # keeps timing equal for unknown usernames


def hash_password(password: str) -> str:
    return _ph.hash(password)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _audit(conn, action, user=None, detail=None, iphash=None):
    conn.execute("INSERT INTO audit_log (user_id, username, action, detail, ip_hash) VALUES (%s,%s,%s,%s::jsonb,%s)",
                 (user["id"] if user else None, user["username"] if user else (detail or {}).get("username"),
                  action, json.dumps(detail or {}), iphash))


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with db.tx() as conn:
        return conn.execute("""SELECT u.id, u.username, u.full_name FROM staff_sessions s
                               JOIN staff_users u ON u.id = s.user_id
                               WHERE s.token_hash = %s AND s.expires_at > now() AND u.active""",
                            (_token_hash(token),)).fetchone()


def _same_origin(request: Request) -> bool:
    """Reject sign-in/out posts made from another site. Modern browsers say so directly
    in Sec-Fetch-Site; with Referrer-Policy no-referrer they send Origin: null, so the
    Origin check is only a fallback."""
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return True
    expected = urlparse(request.app.state.settings.public_base_url)
    got = urlparse(origin)
    return (got.scheme, got.netloc) == (expected.scheme, expected.netloc) or got.netloc == request.headers.get("host")


def _login_page(message: str = "", username: str = "") -> HTMLResponse:
    msg = f'<p class="err" role="alert">{html.escape(message)}</p>' if message else ""
    page = f"""<!doctype html><html lang="en-NG"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Staff sign-in · Taraba Central Billing and Collections</title>
<style>
:root{{--band:#0b3d2e;--primary:#275941;--fg:#171717;--fg3:#6b6a67;--border:#d2d0cb;--neg:#b42318}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;font:500 15px/1.5 "Segoe UI",system-ui,sans-serif;
background:radial-gradient(65% 45% at 25% 90%,rgba(0,128,0,.2),transparent),#0b3d2e;color:var(--fg);padding:16px}}
main{{width:100%;max-width:380px;background:#fff;border-radius:12px;padding:28px;box-shadow:0 20px 48px -12px rgba(0,0,0,.45)}}
h1{{font-size:20px;margin:0 0 4px;letter-spacing:-.02em}}p.sub{{margin:0 0 20px;color:var(--fg3);font-size:13.5px;font-weight:600}}
label{{display:block;font-size:13px;font-weight:700;margin:14px 0 6px}}
input{{width:100%;height:42px;border-radius:8px;border:1px solid var(--border);padding:0 12px;font:inherit}}
input:focus{{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(39,89,65,.25)}}
button{{margin-top:20px;width:100%;height:42px;border:0;border-radius:8px;background:var(--primary);color:#fff;font:inherit;font-weight:700;cursor:pointer}}
button:hover{{background:#1d4632}}.err{{background:#fdecea;color:var(--neg);border-radius:8px;padding:10px 12px;font-size:13.5px;font-weight:650}}
</style></head><body><main>
<h1>Staff sign-in</h1><p class="sub">Taraba Central Billing and Collections · TIRS</p>{msg}
<form method="post" action="/staff/login" autocomplete="on">
<label for="u">Username</label><input id="u" name="username" required autocomplete="username" value="{html.escape(username)}">
<label for="p">Password</label><input id="p" name="password" type="password" required autocomplete="current-password">
<button type="submit">Sign in</button></form></main></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/staff/login")
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/staff/", status_code=303)
    return _login_page()


@router.post("/staff/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    s = request.app.state.settings
    if not _same_origin(request):
        return Response(status_code=403)
    username = username.strip().lower()[:64]
    iphash = security.ip_hash(s.app_secret, security.client_ip(request))
    with db.tx() as conn:
        fails = conn.execute("""SELECT count(*) AS n FROM audit_log WHERE action = 'login_failed'
                                AND at > now() - interval '15 minutes' AND (username = %s OR ip_hash = %s)""",
                             (username, iphash)).fetchone()["n"]
        if fails >= MAX_FAILURES:
            _audit(conn, "login_blocked", detail={"username": username}, iphash=iphash)
            return _login_page("Too many failed attempts. Try again in 15 minutes.", username)
        user = conn.execute("SELECT * FROM staff_users WHERE username = %s AND active", (username,)).fetchone()
        ok = False
        try:
            ok = _ph.verify(user["password_hash"] if user else _DUMMY, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            ok = False
        if not (ok and user):
            _audit(conn, "login_failed", detail={"username": username}, iphash=iphash)
            return _login_page("Wrong username or password.", username)
        if _ph.check_needs_rehash(user["password_hash"]):
            conn.execute("UPDATE staff_users SET password_hash = %s WHERE id = %s", (_ph.hash(password), user["id"]))
        token = secrets.token_urlsafe(32)
        conn.execute("""INSERT INTO staff_sessions (token_hash, user_id, expires_at, ip_hash)
                        VALUES (%s, %s, now() + make_interval(hours => %s), %s)""",
                     (_token_hash(token), user["id"], s.session_hours, iphash))
        conn.execute("UPDATE staff_users SET last_login_at = now() WHERE id = %s", (user["id"],))
        conn.execute("DELETE FROM staff_sessions WHERE expires_at < now()")
        _audit(conn, "login", user, iphash=iphash)
    resp = RedirectResponse("/staff/", status_code=303)
    resp.set_cookie(COOKIE, token, max_age=s.session_hours * 3600, httponly=True, secure=s.cookie_secure,
                    samesite="strict", path="/staff")
    return resp


@router.post("/staff/logout")
def logout(request: Request):
    if not _same_origin(request):
        return Response(status_code=403)
    token = request.cookies.get(COOKIE)
    if token:
        with db.tx() as conn:
            user = conn.execute("""SELECT u.id, u.username FROM staff_sessions s JOIN staff_users u ON u.id = s.user_id
                                   WHERE s.token_hash = %s""", (_token_hash(token),)).fetchone()
            conn.execute("DELETE FROM staff_sessions WHERE token_hash = %s", (_token_hash(token),))
            if user:
                _audit(conn, "logout", user)
    resp = RedirectResponse("/staff/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/staff")
    return resp


@router.get("/staff")
def staff_root():
    return RedirectResponse("/staff/", status_code=307)


@router.get("/staff/")
def staff_console(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/staff/login", status_code=303)
    s = request.app.state.settings
    path = Path(s.staff_file)
    if not path.exists():
        return HTMLResponse("<p>The staff console has not been published to this server yet.</p>", status_code=503)
    with db.tx() as conn:
        _audit(conn, "view_console", user,
               iphash=security.ip_hash(s.app_secret, security.client_ip(request)))
    page = path.read_text(encoding="utf-8")
    bar = ('<form method="post" action="/staff/logout" style="position:fixed;right:12px;bottom:12px;z-index:60;margin:0;'
           'display:flex;gap:10px;align-items:center;background:#fff;border-radius:999px;padding:6px 8px 6px 14px;'
           'box-shadow:0 0 0 1px rgba(0,0,0,.08),0 8px 24px -6px rgba(16,24,20,.25);font:600 13px/1 Nunito,system-ui,sans-serif" class="no-print">'
           f'<span>Signed in as <b>{html.escape(user["full_name"])}</b></span>'
           '<button type="submit" style="border:0;border-radius:999px;background:#275941;color:#fff;font:inherit;'
           'font-weight:700;padding:7px 12px;cursor:pointer">Sign out</button></form>')
    # the LAST </body>: the bundled PDF library contains that text inside its own code
    cut = page.rfind("</body>")
    page = page[:cut] + bar + page[cut:] if cut >= 0 else page + bar
    return HTMLResponse(page, headers={"Cache-Control": "no-store, private"})
