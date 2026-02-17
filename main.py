"""
AZEUQER — Backend Core (V23)
FastAPI + Supabase(PostgREST) email capture + Telegram user binding + founder whitelist (first 23)
- No pydantic EmailStr (avoids email-validator dependency)
- Duplicate email protection
- Basic bot protection (rate limit + honeypot)
Deploy: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import os
import re
import time
import json
import hmac
import hashlib
import urllib.parse
from typing import Optional, Dict, Tuple

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -----------------------------
# Config
# -----------------------------
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or ""
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "azeuqer_users")

# Optional: Verify Telegram initData if you have your bot token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")  # strongly recommended in production

# Rate limiting (simple in-memory; good enough for a single Render instance)
RL_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RL_MAX_REQ = int(os.getenv("RATE_LIMIT_MAX_REQ", "12"))

# Founder cap
FOUNDER_CAP = int(os.getenv("FOUNDER_CAP", "23"))

# Email validation (simple, practical)
EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.I)

# -----------------------------
# App
# -----------------------------
app = FastAPI(title="AZEUQER V23 Backend", version="23.0.0")

# CORS — allow your Telegram webapp origin(s)
# For local testing, this is permissive. Tighten in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# -----------------------------
# Utilities
# -----------------------------
_rate_bucket: Dict[str, Tuple[int, float]] = {}  # key -> (count, window_start)

def _rate_key(ip: str, tg_uid: Optional[int]) -> str:
    return f"{ip}:{tg_uid or 'no_tg'}"

def rate_limit(ip: str, tg_uid: Optional[int]) -> None:
    key = _rate_key(ip, tg_uid)
    now = time.time()
    count, start = _rate_bucket.get(key, (0, now))
    if now - start >= RL_WINDOW_SEC:
        _rate_bucket[key] = (1, now)
        return
    if count + 1 > RL_MAX_REQ:
        raise HTTPException(status_code=429, detail="Rate limit hit. Slow down.")
    _rate_bucket[key] = (count + 1, start)

def require_env() -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY",
        )

def is_valid_email(email: str) -> bool:
    email = (email or "").strip()
    if len(email) < 5 or len(email) > 254:
        return False
    return bool(EMAIL_RE.match(email))

def _telegram_secret_key(bot_token: str) -> bytes:
    # Telegram: secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()

def verify_telegram_init_data(init_data: str, bot_token: str) -> bool:
    """
    Verifies Telegram WebApp initData (recommended).
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    """
    if not init_data or not bot_token:
        return False
    parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    data = dict(parsed)
    their_hash = data.pop("hash", None)
    if not their_hash:
        return False

    # data_check_string: sort keys, key=value joined by '\n'
    pairs = [f"{k}={data[k]}" for k in sorted(data.keys())]
    dcs = "\n".join(pairs).encode("utf-8")

    secret = _telegram_secret_key(bot_token)
    calc_hash = hmac.new(secret, dcs, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc_hash, their_hash)

def parse_telegram_user_id(init_data: str) -> Optional[int]:
    """
    Extract Telegram user id from initData's 'user' field (JSON string).
    Works even if you don't verify (but verification is recommended).
    """
    if not init_data:
        return None
    data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        u = json.loads(user_raw)
        uid = u.get("id")
        if isinstance(uid, int):
            return uid
        if isinstance(uid, str) and uid.isdigit():
            return int(uid)
    except Exception:
        return None
    return None

async def sb_request(method: str, path: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None, headers: Optional[dict]=None):
    require_env()
    url = f"{SUPABASE_URL}{path}"
    h = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if headers:
        h.update(headers)
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.request(method, url, params=params, json=json_body, headers=h)
        return r

async def sb_get_user_by_email(email: str):
    r = await sb_request(
        "GET",
        f"/rest/v1/{SUPABASE_TABLE}",
        params={"select": "id,telegram_user_id,email,is_founder,created_at", "email": f"eq.{email}"},
    )
    if r.status_code == 200:
        arr = r.json()
        return arr[0] if arr else None
    raise HTTPException(status_code=502, detail=f"Supabase read failed: {r.status_code}: {r.text[:200]}")

async def sb_get_user_by_tg(tg_uid: int):
    r = await sb_request(
        "GET",
        f"/rest/v1/{SUPABASE_TABLE}",
        params={"select": "id,telegram_user_id,email,is_founder,created_at", "telegram_user_id": f"eq.{tg_uid}"},
    )
    if r.status_code == 200:
        arr = r.json()
        return arr[0] if arr else None
    raise HTTPException(status_code=502, detail=f"Supabase read failed: {r.status_code}: {r.text[:200]}")

async def sb_count_users() -> int:
    # Use head + count=exact to avoid downloading rows
    r = await sb_request(
        "GET",
        f"/rest/v1/{SUPABASE_TABLE}",
        params={"select": "id"},
        headers={"Prefer": "count=exact"},
    )
    if r.status_code == 200:
        cr = r.headers.get("content-range", "")
        if "/" in cr:
            try:
                return int(cr.split("/")[-1])
            except Exception:
                return 0
        return 0
    raise HTTPException(status_code=502, detail=f"Supabase count failed: {r.status_code}: {r.text[:200]}")

async def sb_upsert_user(tg_uid: int, email: str, is_founder: bool):
    # Upsert by telegram_user_id; email stays unique and will throw 409 if taken by another user.
    payload = {
        "telegram_user_id": tg_uid,
        "email": email,
        "is_founder": is_founder,
        "last_seen_at": "now()",
    }
    r = await sb_request(
        "POST",
        f"/rest/v1/{SUPABASE_TABLE}",
        params={"on_conflict": "telegram_user_id"},
        json_body=payload,
        headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )
    if r.status_code in (200, 201):
        arr = r.json()
        return arr[0] if arr else None

    if r.status_code == 409:
        raise HTTPException(status_code=409, detail="Email already bound to another Telegram user.")
    raise HTTPException(status_code=502, detail=f"Supabase upsert failed: {r.status_code}: {r.text[:250]}")

async def sb_set_founder(tg_uid: int, is_founder: bool):
    r = await sb_request(
        "PATCH",
        f"/rest/v1/{SUPABASE_TABLE}",
        params={"telegram_user_id": f"eq.{tg_uid}"},
        json_body={"is_founder": is_founder},
        headers={"Prefer": "return=representation"},
    )
    if r.status_code == 200:
        arr = r.json()
        return arr[0] if arr else None
    raise HTTPException(status_code=502, detail=f"Supabase patch failed: {r.status_code}: {r.text[:250]}")

# -----------------------------
# Schemas
# -----------------------------
class EmailPayload(BaseModel):
    email: str = Field(..., max_length=254)
    initData: Optional[str] = Field(default=None)
    website: Optional[str] = Field(default=None)  # honeypot

class EmailResponse(BaseModel):
    ok: bool
    telegram_user_id: Optional[int] = None
    email: Optional[str] = None
    is_founder: bool = False
    message: str = ""

# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
async def health():
    return {"ok": True, "version": app.version}

@app.post("/profile/email", response_model=EmailResponse)
async def capture_email(payload: EmailPayload, request: Request):
    """
    Stores/binds email -> Telegram user id in Supabase.
    - Requires Telegram initData in payload.initData (from window.Telegram.WebApp.initData)
    - Duplicate protection:
        - If email already exists for another telegram_user_id => 409
        - If same telegram_user_id re-submits, we update/merge
    - Founder whitelist: first 23 unique users become is_founder=True
    """
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "0.0.0.0").split(",")[0].strip()

    if payload.website and payload.website.strip():
        raise HTTPException(status_code=400, detail="Bot detected.")

    init_data = (payload.initData or "").strip()
    tg_uid = parse_telegram_user_id(init_data)

    if tg_uid is None:
        raise HTTPException(status_code=400, detail="Missing Telegram initData. Open inside Telegram WebApp.")

    rate_limit(ip, tg_uid)

    if TELEGRAM_BOT_TOKEN:
        if not verify_telegram_init_data(init_data, TELEGRAM_BOT_TOKEN):
            raise HTTPException(status_code=401, detail="Invalid Telegram initData signature.")

    email = (payload.email or "").strip().lower()
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Invalid email.")

    existing_by_email = await sb_get_user_by_email(email)
    if existing_by_email and int(existing_by_email.get("telegram_user_id") or 0) != int(tg_uid):
        raise HTTPException(status_code=409, detail="Email already bound to another Telegram user.")

    existing_by_tg = await sb_get_user_by_tg(tg_uid)

    is_founder = bool(existing_by_tg.get("is_founder")) if existing_by_tg else False
    if not is_founder:
        count = await sb_count_users()
        projected = count + (0 if existing_by_tg else 1)
        if projected <= FOUNDER_CAP:
            is_founder = True

    row = await sb_upsert_user(tg_uid=tg_uid, email=email, is_founder=is_founder)

    if row and is_founder and not row.get("is_founder"):
        row = await sb_set_founder(tg_uid, True)

    return EmailResponse(
        ok=True,
        telegram_user_id=tg_uid,
        email=email,
        is_founder=bool(row.get("is_founder")) if row else is_founder,
        message="Email stored and bound to your Telegram ID.",
    )
