"""Taraba Central Billing and Collections: web server.

Serves the public site (one notice at a time), the payment API, the Monnify webhook
and the staff console behind sign-in. Run with:
    uvicorn app.main:app --proxy-headers --forwarded-allow-ips='*'"""
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import config, db, public_api, staff, webhook
from .monnify import Monnify

log = logging.getLogger("tirs")

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
       "font-src data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.load()
    db.init(s.database_url)
    db.apply_schema()
    app.state.settings = s
    app.state.log = log
    lock, holder = threading.Lock(), {}

    def monnify() -> Monnify:
        with lock:
            if "c" not in holder:
                holder["c"] = Monnify(s.monnify_base_url, s.monnify_api_key, s.monnify_secret_key,
                                      s.monnify_contract_code)
            return holder["c"]

    app.state.monnify = monnify
    log.info("server started: payments %s, Monnify %s", "ENABLED" if s.payments_enabled else "disabled",
             s.monnify_env)
    yield
    if "c" in holder:
        holder["c"].close()
    db.close()


app = FastAPI(title="TIRS billing", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(public_api.router)
app.include_router(webhook.router)
app.include_router(staff.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    h.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
    if request.url.path.startswith(("/api/", "/staff")):
        h["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health():
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM assessments").fetchone()["n"]
    return {"ok": True, "notices": n}


@app.get("/robots.txt")
def robots():
    return HTMLResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/")
def public_site(request: Request):
    index = Path(request.app.state.settings.site_dir) / "index.html"
    if not index.exists():
        return HTMLResponse("<p>The public site has not been published to this server yet.</p>", status_code=503)
    return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"error": "Something went wrong. Please try again."}, status_code=500)
