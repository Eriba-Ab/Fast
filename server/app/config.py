"""Settings, read once from environment variables (see deploy/.env.example)."""
import os
from dataclasses import dataclass


def _bool(value, default=False):
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_secret: str                 # signs receipt links, salts IP hashes
    public_base_url: str            # https://taraba.govpayhub.com
    payments_enabled: bool          # the go-live gate: false until real balances exist
    monnify_env: str                # sandbox | live
    monnify_api_key: str
    monnify_secret_key: str
    monnify_contract_code: str
    monnify_webhook_secret: str     # key for the monnify-signature HMAC (to confirm with Monnify)
    monnify_webhook_ip: str
    site_dir: str                   # public site (index.html)
    staff_file: str                 # the full staff console (taraba-billing.html)
    data_file: str                  # assessments export written by build.py
    cookie_secure: bool
    session_hours: int

    monnify_base_url_override: str = ""   # tests only: point at a local stand-in for Monnify

    @property
    def monnify_base_url(self) -> str:
        if self.monnify_base_url_override:
            return self.monnify_base_url_override
        return "https://api.monnify.com" if self.monnify_env == "live" else "https://sandbox.monnify.com"

    @property
    def monnify_configured(self) -> bool:
        return bool(self.monnify_api_key and self.monnify_secret_key and self.monnify_contract_code)


def load() -> Settings:
    env = os.environ.get
    secret = env("APP_SECRET", "")
    if len(secret) < 32:
        raise SystemExit("APP_SECRET must be set to a random string of at least 32 characters.")
    monnify_env = (env("MONNIFY_ENV", "sandbox") or "sandbox").lower()
    if monnify_env not in ("sandbox", "live"):
        raise SystemExit("MONNIFY_ENV must be 'sandbox' or 'live'.")
    secret_key = env("MONNIFY_SECRET_KEY", "")
    return Settings(
        database_url=env("DATABASE_URL", "postgresql://tirs:tirs@db:5432/tirs"),
        app_secret=secret,
        public_base_url=(env("PUBLIC_BASE_URL", "https://taraba.govpayhub.com") or "").rstrip("/"),
        payments_enabled=_bool(env("PAYMENTS_ENABLED"), False),
        monnify_env=monnify_env,
        monnify_api_key=env("MONNIFY_API_KEY", ""),
        monnify_secret_key=secret_key,
        monnify_contract_code=env("MONNIFY_CONTRACT_CODE", ""),
        monnify_webhook_secret=env("MONNIFY_WEBHOOK_SECRET", "") or secret_key,
        monnify_webhook_ip=env("MONNIFY_WEBHOOK_IP", "35.242.133.146"),
        site_dir=env("SITE_DIR", "/srv/site"),
        staff_file=env("STAFF_FILE", "/srv/staff/taraba-billing.html"),
        data_file=env("DATA_FILE", "/srv/data/assessments.json.gz"),
        cookie_secure=_bool(env("COOKIE_SECURE"), True),
        session_hours=int(env("SESSION_HOURS", "10") or 10),
        monnify_base_url_override=(env("MONNIFY_BASE_URL", "") or "").rstrip("/")
        if monnify_env == "sandbox" else "",       # never overridable in live mode
    )
