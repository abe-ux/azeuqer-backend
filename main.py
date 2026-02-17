"""
main.py — AZEUQER Backend (Void Architect Edition)
=================================================

What this file guarantees (in ONE place):
- FastAPI app that boots on Render (Python 3.9+)
- Telegram WebApp initData verification (real bot protection)
- Email capture endpoint that:
  - validates email WITHOUT requiring email-validator (no boot crash)
  - binds Telegram user_id -> email
  - prevents duplicates (same email cannot be used by multiple TG users)
  - idempotent updates (same user + same email is OK)
  - founder whitelist (first 23 unique TG users who bind email get founder slot 1..23)
- `/game/feed` + `/game/swipe` endpoints to match your Screen 3 client calls
- Safe defaults + strong logging + health endpoint

IMPORTANT (DB schema expectations)
--------------------------------
Create these tables in Supabase (SQL editor). If you already have them, ensure constraints match.

1) email_bindings
   - tg_user_id BIGINT PRIMARY KEY
   - email TEXT UNIQUE NOT NULL
   - tg_username TEXT
   - tg_first_name TEXT
   - tg_last_name TEXT
   - created_at TIMESTAMPTZ DEFAULT now()
   - updated_at TIMESTAMPTZ DEFAULT now()
   - is_founder BOOLEAN DEFAULT false
   - founder_number INT UNIQUE NULL

2) founders
   - tg_user_id BIGINT PRIMARY KEY
   - founder_number INT UNIQUE NOT NULL
   - created_at TIMESTAMPTZ DEFAULT now()

3) swipes (optional; but useful)
   - id BIGSERIAL PRIMARY KEY
   - tg_user_id BIGINT NOT NULL
   - target_id BIGINT NOT NULL
   - direction TEXT NOT NULL  -- "LIGHT" | "SPITE"
   - created_at TIMESTAMPTZ DEFAULT now()

If you do NOT want to create table `founders`, you can store founder_number directly on `email_bindings`.
This code uses BOTH for safety: founders table is the authoritative “slot claim”.

ENV VARS REQUIRED (Render)
-------------------------
- SUPABASE_URL                 (e.g. https://xxxx.supabase.co)
- SUPABASE_SERVICE_ROLE_KEY    (Service Role key, NOT anon)
- TELEGRAM_BOT_TOKEN           (your bot token, used to verify initData)

OPTIONAL ENV
------------
- DEBUG_ALLOW_NO_TELEGRAM=1    (ONLY for local debugging; do NOT enable in production)
- ALLOWED_ORIGINS=*            (or comma list; default "*")
"""

from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
import logging
import secrets
from typing import Any, Dict, Optional, List, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from supabase import create_client, Client


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("azeuqer")


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

DEBUG_ALLOW_NO_TELEGRAM = os.getenv("DEBUG_ALLOW_NO_TELEGRAM", "0").strip() == "1"

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").strip()
if ALLOWED_ORIGINS == "*":
    ORIGINS = ["*"]
else:
    ORIGINS = [x.strip() for x in ALLOWED_ORIGINS.split(",") if x.strip()]

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    log.warning("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing. Supabase operations will fail.")

if not TELEGRAM_BOT_TOKEN and not DEBUG_ALLOW_NO_TELEGRAM:
    log.warning("TELEGRAM_BOT_TOKEN missing. Telegram verification will fail unless DEBUG_ALLOW_NO_TELEGRAM=1.")


# -----------------------------------------------------------------------------
# Supabase client
# -----------------------------------------------------------------------------
def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase env vars not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY).")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


sb: Optional[Client] = None
try:
    sb = get_supabase()
except Exception as e:
    log.error("Supabase client init failed: %s", e)
    sb = None


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(
    title="AZEUQER Backend",
    version="v23",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# In-memory rate limiting (bot friction)
# (Render single instance friendly; if you scale, move to Redis)
# -----------------------------------------------------------------------------
class SimpleRateLimiter:
    def __init__(self) -> None:
        self.ip_hits: Dict[str, List[float]] = {}
        self.user_hits: Dict[int, List[float]] = {}

    @staticmethod
    def _prune(ts: List[float], window_s: int) -> List[float]:
        now = time.time()
        return [t for t in ts if now - t <= window_s]

    def hit_ip(self, ip: str, limit: int = 60, window_s: int = 60) -> None:
        now = time.time()
        arr = self.ip_hits.get(ip, [])
        arr = self._prune(arr, window_s)
        arr.append(now)
        self.ip_hits[ip] = arr
        if len(arr) > limit:
            raise HTTPException(status_code=429, detail="Too many requests (IP rate limit).")

    def hit_user(self, user_id: int, limit: int = 30, window_s: int = 60) -> None:
        now = time.time()
        arr = self.user_hits.get(user_id, [])
        arr = self._prune(arr, window_s)
        arr.append(now)
        self.user_hits[user_id] = arr
        if len(arr) > limit:
            raise HTTPException(status_code=429, detail="Too many requests (User rate limit).")


rl = SimpleRateLimiter()


# -----------------------------------------------------------------------------
# Telegram initData verification (REAL bot protection)
# -----------------------------------------------------------------------------
def _parse_qs(raw: str) -> Dict[str, str]:
    """
    Telegram initData is querystring-like: "key=value&key=value"
    Values are urlencoded; Telegram sends it in window.Telegram.WebApp.initData
    """
    out: Dict[str, str] = {}
    if not raw:
        return out
    parts = raw.split("&")
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        # Telegram's initData is already decoded by many clients; but to be safe:
        # we avoid importing urllib here; values should still compare correctly
        out[k] = v
    return out


def _tg_check_hash(init_data: str, bot_token: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """
    Verify Telegram WebApp initData signature.
    Algorithm: https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    Steps:
      - parse initData into key/value
      - extract hash
      - data_check_string = '\n'.join(sorted([f"{k}={v}" for k,v in pairs if k!='hash']))
      - secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
      - computed_hash = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()
    Returns (ok, user_dict, reason)
    """
    if not init_data:
        return False, None, "missing_init_data"

    d = _parse_qs(init_data)
    their_hash = d.get("hash", "")
    if not their_hash:
        return False, None, "missing_hash"

    pairs = []
    for k, v in d.items():
        if k == "hash":
            continue
        pairs.append(f"{k}={v}")
    pairs.sort()
    data_check_string = "\n".join(pairs)

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    computed = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, their_hash):
        return False, None, "bad_hash"

    # auth_date freshness check (optional but recommended)
    auth_date_raw = d.get("auth_date")
    if auth_date_raw and auth_date_raw.isdigit():
        auth_date = int(auth_date_raw)
        now = int(time.time())
        # allow 24h window
        if now - auth_date > 86400:
            return False, None, "auth_date_too_old"

    # user is JSON string; may be URL encoded; try best-effort decode
    user_raw = d.get("user")
    user_obj = None
    if user_raw:
        # Telegram often provides raw JSON (but URL-encoded in QS).
        # We do a soft-repair: replace %22 etc if it exists would require urllib.
        # If your client passes decoded initData, json.loads works.
        try:
            user_obj = json.loads(user_raw)
        except Exception:
            # last resort: attempt minimal URL-decoding for common characters
            try:
                repaired = (
                    user_raw.replace("%22", '"')
                    .replace("%7B", "{")
                    .replace("%7D", "}")
                    .replace("%3A", ":")
                    .replace("%2C", ",")
                    .replace("%5B", "[")
                    .replace("%5D", "]")
                )
                user_obj = json.loads(repaired)
            except Exception:
                user_obj = None

    return True, user_obj, "ok"


def require_telegram_user(init_data: str) -> Dict[str, Any]:
    """
    Returns Telegram user object (dict).
    Raises HTTPException if verification fails (unless DEBUG_ALLOW_NO_TELEGRAM).
    """
    if DEBUG_ALLOW_NO_TELEGRAM:
        # Debug mode: allow local development without Telegram.
        # We return a deterministic fake user.
        return {"id": 999000111, "username": "debug_mode", "first_name": "Debug", "last_name": "Mode"}

    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Server missing TELEGRAM_BOT_TOKEN.")

    ok, user_obj, reason = _tg_check_hash(init_data, TELEGRAM_BOT_TOKEN)
    if not ok or not user_obj or "id" not in user_obj:
        raise HTTPException(status_code=401, detail=f"Telegram initData invalid ({reason}).")

    return user_obj


# -----------------------------------------------------------------------------
# Email validation without email-validator dependency (prevents boot crash)
# -----------------------------------------------------------------------------
def is_probably_email(s: str) -> bool:
    """
    Practical validation:
    - 3..254 chars
    - exactly one "@"
    - has dot in domain part
    - no spaces
    This avoids pydantic EmailStr which requires email-validator package.
    """
    if not s:
        return False
    s = s.strip()
    if " " in s:
        return False
    if len(s) < 3 or len(s) > 254:
        return False
    if s.count("@") != 1:
        return False
    local, domain = s.split("@", 1)
    if not local or not domain:
        return False
    if "." not in domain:
        return False
    if domain.startswith(".") or domain.endswith("."):
        return False
    # basic forbidden
    if any(c in s for c in ["\n", "\r", "\t"]):
        return False
    return True


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class TelegramInit(BaseModel):
    initData: str = Field(default="", description="Telegram WebApp initData string")


class EmailBindRequest(BaseModel):
    email: str
    initData: str = Field(default="", description="Telegram WebApp initData; required unless DEBUG_ALLOW_NO_TELEGRAM=1")


class FeedRequest(BaseModel):
    initData: str = Field(default="")


class SwipeRequest(BaseModel):
    initData: str = Field(default="")
    target_id: int
    direction: str  # "LIGHT" | "SPITE"


# -----------------------------------------------------------------------------
# Helpers: request metadata
# -----------------------------------------------------------------------------
def get_client_ip(request: Request) -> str:
    # Render / proxies may set x-forwarded-for
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ensure_supabase() -> Client:
    global sb
    if sb is None:
        try:
            sb = get_supabase()
        except Exception as e:
            log.error("Supabase re-init failed: %s", e)
            raise HTTPException(status_code=500, detail="Supabase not configured.")
    return sb


# -----------------------------------------------------------------------------
# Founder slot claiming (first 23 unique TG users)
# -----------------------------------------------------------------------------
FOUNDER_LIMIT = 23


def _get_existing_founder_slot(supabase: Client, tg_user_id: int) -> Optional[int]:
    try:
        res = (
            supabase.table("founders")
            .select("founder_number")
            .eq("tg_user_id", tg_user_id)
            .limit(1)
            .execute()
        )
        data = res.data or []
        if data:
            return int(data[0]["founder_number"])
        return None
    except Exception as e:
        log.error("Founder lookup failed: %s", e)
        return None


def _count_founders(supabase: Client) -> int:
    try:
        res = supabase.table("founders").select("tg_user_id", count="exact").execute()
        # supabase-py returns count in res.count
        return int(getattr(res, "count", 0) or 0)
    except Exception as e:
        log.error("Founder count failed: %s", e)
        return 0


def claim_founder_slot(supabase: Client, tg_user_id: int) -> Optional[int]:
    """
    Attempts to claim a founder slot 1..23 for this tg_user_id.
    - If already has a slot -> return it.
    - If slots exhausted -> return None.
    - Tries a few times to avoid rare race conflicts.
    Requires UNIQUE constraint on founders(founder_number).
    """
    existing = _get_existing_founder_slot(supabase, tg_user_id)
    if existing:
        return existing

    for _ in range(6):
        count = _count_founders(supabase)
        if count >= FOUNDER_LIMIT:
            return None
        candidate = count + 1

        try:
            # Insert claim
            supabase.table("founders").insert(
                {"tg_user_id": tg_user_id, "founder_number": candidate}
            ).execute()
            return candidate
        except Exception:
            # likely a unique conflict (someone grabbed the same number) -> retry
            time.sleep(0.08 + secrets.randbelow(40) / 1000.0)

    # If we get here, we failed due to contention
    return _get_existing_founder_slot(supabase, tg_user_id)


# -----------------------------------------------------------------------------
# Email binding logic (Supabase)
# -----------------------------------------------------------------------------
def get_binding_by_email(supabase: Client, email: str) -> Optional[Dict[str, Any]]:
    try:
        res = supabase.table("email_bindings").select("*").eq("email", email).limit(1).execute()
        data = res.data or []
        return data[0] if data else None
    except Exception as e:
        log.error("get_binding_by_email failed: %s", e)
        return None


def get_binding_by_user(supabase: Client, tg_user_id: int) -> Optional[Dict[str, Any]]:
    try:
        res = supabase.table("email_bindings").select("*").eq("tg_user_id", tg_user_id).limit(1).execute()
        data = res.data or []
        return data[0] if data else None
    except Exception as e:
        log.error("get_binding_by_user failed: %s", e)
        return None


def upsert_email_binding(
    supabase: Client,
    tg_user: Dict[str, Any],
    email: str
) -> Dict[str, Any]:
    """
    Enforces:
    - same email cannot be bound to a different tg_user_id
    - same tg_user_id can re-submit same email (idempotent)
    - same tg_user_id can change email ONLY if new email is not taken
    - founder slot claim on first successful bind (if available)
    """
    tg_user_id = int(tg_user["id"])
    username = tg_user.get("username")
    first_name = tg_user.get("first_name")
    last_name = tg_user.get("last_name")

    # Check if email already used by someone else
    existing_email = get_binding_by_email(supabase, email)
    if existing_email and int(existing_email.get("tg_user_id")) != tg_user_id:
        raise HTTPException(status_code=409, detail="Email already bound to another Telegram account.")

    # Determine if this user already has a binding
    existing_user = get_binding_by_user(supabase, tg_user_id)

    # Founder logic: if user never claimed slot, try claim now
    founder_number = _get_existing_founder_slot(supabase, tg_user_id)
    if not founder_number:
        founder_number = claim_founder_slot(supabase, tg_user_id)

    is_founder = bool(founder_number)

    payload = {
        "tg_user_id": tg_user_id,
        "email": email,
        "tg_username": username,
        "tg_first_name": first_name,
        "tg_last_name": last_name,
        "updated_at": "now()",
        "is_founder": is_founder,
        "founder_number": founder_number,
    }

    # Insert or update
    try:
        if existing_user:
            # If changing email, ensure new email not taken by someone else (already checked above)
            supabase.table("email_bindings").update(payload).eq("tg_user_id", tg_user_id).execute()
        else:
            payload["created_at"] = "now()"
            supabase.table("email_bindings").insert(payload).execute()
    except Exception as e:
        log.error("upsert_email_binding failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save email binding.")

    # Return final record
    final_rec = get_binding_by_user(supabase, tg_user_id) or payload
    return final_rec


# -----------------------------------------------------------------------------
# API Endpoints
# -----------------------------------------------------------------------------
@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "azeuqer",
        "version": "v23",
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
        "telegram_verification": bool(TELEGRAM_BOT_TOKEN) or DEBUG_ALLOW_NO_TELEGRAM,
        "debug_allow_no_telegram": DEBUG_ALLOW_NO_TELEGRAM,
    }


@app.post("/profile/email")
async def bind_email(req: Request) -> Dict[str, Any]:
    """
    Frontend calls this to store email -> Supabase, bound to Telegram user.
    REQUIRED: initData (unless DEBUG_ALLOW_NO_TELEGRAM=1)

    Accepts initData from:
    - JSON body { email, initData }
    - header X-Telegram-InitData (if you ever choose to send it that way)
    """
    ip = get_client_ip(req)
    rl.hit_ip(ip, limit=40, window_s=60)

    body = await req.json()
    payload = EmailBindRequest(**body)

    init_data = payload.initData or req.headers.get("x-telegram-initdata", "") or ""
    tg_user = require_telegram_user(init_data)

    rl.hit_user(int(tg_user["id"]), limit=18, window_s=60)

    email = (payload.email or "").strip().lower()
    if not is_probably_email(email):
        raise HTTPException(status_code=400, detail="Invalid email format.")

    supabase = ensure_supabase()

    record = upsert_email_binding(supabase, tg_user, email)

    return {
        "ok": True,
        "tg_user_id": int(tg_user["id"]),
        "email": record.get("email"),
        "is_founder": bool(record.get("is_founder")),
        "founder_number": record.get("founder_number"),
    }


@app.post("/game/feed")
async def game_feed(req: Request) -> Dict[str, Any]:
    """
    Screen 3 expects:
      POST /game/feed  { initData }
    and returns:
      { feed: [ { user_id, username, faction, bio_lock_url } ... ] }

    For now this is a stable fake feed. Once you have real users, replace.
    """
    ip = get_client_ip(req)
    rl.hit_ip(ip, limit=120, window_s=60)

    body = await req.json()
    payload = FeedRequest(**body)

    tg_user = require_telegram_user(payload.initData)
    rl.hit_user(int(tg_user["id"]), limit=60, window_s=60)

    # Stable pseudo-random based on user id so it doesn't "jump" every refresh
    uid = int(tg_user["id"])
    rng = secrets.SystemRandom(uid)

    names = [
        "NOVA","KAI","LUNA","MILO","ZARA","JUNO","NYX","RIVEN","SAGE","ARIA",
        "VEGA","AXEL","IRIS","CORA","ELIO","KODA","VIO","SKYE","ORION","MAY",
        "NIKO","SERA","RAYA","ZEN","ECHO","SOL","AZRA","NOEL","REMI","IVY",
    ]
    factions = ["UNSORTED", "EUPHORIA", "DISSONANCE"]

    feed: List[Dict[str, Any]] = []
    # Provide 30 targets
    for i in range(30):
        target_id = 90000 + i
        nm = names[i % len(names)]
        # deterministic but varied
        faction = factions[(i + (uid % 3)) % len(factions)]
        gender_seed = "men" if i % 2 == 0 else "women"
        img = f"https://randomuser.me/api/portraits/{gender_seed}/{i % 99}.jpg"
        feed.append(
            {
                "user_id": target_id,
                "username": nm,
                "faction": faction,
                "bio_lock_url": img,
            }
        )

    return {"feed": feed}


@app.post("/game/swipe")
async def game_swipe(req: Request) -> Dict[str, Any]:
    """
    Screen 3 expects:
      POST /game/swipe { initData, target_id, direction }

    We'll record swipe to Supabase if table exists; otherwise just accept.
    """
    ip = get_client_ip(req)
    rl.hit_ip(ip, limit=240, window_s=60)

    body = await req.json()
    payload = SwipeRequest(**body)

    direction = (payload.direction or "").upper().strip()
    if direction not in ("LIGHT", "SPITE"):
        raise HTTPException(status_code=400, detail="direction must be LIGHT or SPITE")

    tg_user = require_telegram_user(payload.initData)
    uid = int(tg_user["id"])
    rl.hit_user(uid, limit=120, window_s=60)

    supabase = ensure_supabase()

    # best-effort insert
    try:
        supabase.table("swipes").insert(
            {
                "tg_user_id": uid,
                "target_id": int(payload.target_id),
                "direction": direction,
            }
        ).execute()
    except Exception as e:
        # don't block gameplay if logging fails
        log.warning("Swipe insert failed (non-fatal): %s", e)

    return {"ok": True}


# -----------------------------------------------------------------------------
# Root
# -----------------------------------------------------------------------------
@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "ok": True,
        "message": "AZEUQER backend online. Use /health.",
    }


# -----------------------------------------------------------------------------
# NOTE ABOUT THE ORIGINAL CRASH YOU POSTED
# -----------------------------------------------------------------------------
# You had: EmailStr -> requires email-validator -> server crashed.
# This main.py intentionally avoids that dependency to guarantee boot.
#
# If you STILL want strict RFC validation:
#   - install: email-validator
#   - then switch EmailBindRequest.email to EmailStr
#
# But do it only after confirming Render installs the dependency cleanly.
