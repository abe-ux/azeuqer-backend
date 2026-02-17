"""
Azeuqer Backend (V24) — "Void Architect" edition
- FastAPI + Supabase
- Telegram WebApp initData verification
- Email capture + duplicate handling
- Founder whitelist (first 23 users)
- BioLock camera: captures frames from camera (no file uploads UI)
- Game: feed, swipe, monthly factions, stat points allocation
- Boss: guaranteed spawn each scan-cycle (11..20) per user
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from supabase import create_client, Client  # supabase-py


# ==============================
# Config
# ==============================
APP_NAME = "azeuqer-backend"
UTC = timezone.utc

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# If you deploy without Telegram, you can set DEBUG_ALLOW_NO_TELEGRAM=1
DEBUG_ALLOW_NO_TELEGRAM = os.getenv("DEBUG_ALLOW_NO_TELEGRAM", "0") == "1"

# Storage bucket for bio lock images (create in Supabase storage):
BIOLOCK_BUCKET = os.getenv("BIOLOCK_BUCKET", "biolocks")

# Simple anti-bot / rate-limit knobs
RATE_WINDOW_S = int(os.getenv("RATE_WINDOW_S", "60"))
RATE_MAX_REQ = int(os.getenv("RATE_MAX_REQ", "120"))

# Boss defaults
BOSS_BASE_HP = 60
PLAYER_BASE_HP = 40

# Founder whitelist limit
FOUNDER_LIMIT = 23

# CORS (set your WebApp domain here in production)
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    # Let the app boot so Render shows logs; but endpoints will fail fast.
    print("⚠️ Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY env vars.")

sb: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ==============================
# Utilities
# ==============================
def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def month_key(dt: Optional[datetime] = None) -> str:
    d = dt or utc_now()
    return f"{d.year:04d}-{d.month:02d}"


EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> None:
    if not EMAIL_RE.match(email or ""):
        raise HTTPException(status_code=400, detail="Invalid email format.")


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def b64_to_bytes(data_url_or_b64: str) -> bytes:
    """
    Accepts either:
      - "data:image/jpeg;base64,...."
      - plain base64
    """
    s = data_url_or_b64.strip()
    if s.startswith("data:"):
        _, b64 = s.split(",", 1)
        return base64.b64decode(b64)
    return base64.b64decode(s)


# ==============================
# Telegram initData verification
# ==============================
def parse_qs_kv(qs: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in qs.split("&"):
        if not part:
            continue
        if "=" not in part:
            out[part] = ""
            continue
        k, v = part.split("=", 1)
        out[k] = v
    return out


def verify_telegram_init_data(init_data: str, bot_token: str) -> Dict[str, Any]:
    """
    Verifies Telegram WebApp initData according to:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    Returns parsed user object.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData.")

    data = parse_qs_kv(init_data)
    hash_received = data.get("hash")
    if not hash_received:
        raise HTTPException(status_code=401, detail="Invalid initData (no hash).")

    # Build data_check_string: sort keys except hash
    pairs = []
    for k in sorted(k for k in data.keys() if k != "hash"):
        pairs.append(f"{k}={data[k]}")
    data_check_string = "\n".join(pairs)

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    hash_calc = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(hash_calc, hash_received):
        raise HTTPException(status_code=401, detail="Invalid initData signature.")

    # Parse user JSON if present
    user_json = data.get("user")
    if not user_json:
        raise HTTPException(status_code=401, detail="initData missing user.")
    try:
        # Telegram URL-encodes JSON; FastAPI gets raw initData; decode percent-encoding via replace?
        # Many clients pass it already decoded. Attempt both.
        user_str = user_json
        # Percent-decoding minimal:
        user_str = user_str.replace("%22", '"').replace("%7B", "{").replace("%7D", "}")
        user_str = user_str.replace("%3A", ":").replace("%2C", ",").replace("%5B", "[").replace("%5D", "]")
        user_str = user_str.replace("%20", " ").replace("%2B", "+").replace("%2F", "/").replace("%3D", "=")
        user = json.loads(user_str)
    except Exception:
        try:
            user = json.loads(user_json)
        except Exception:
            raise HTTPException(status_code=401, detail="Unable to parse Telegram user.")
    return user


def require_telegram_user(init_data: str) -> Dict[str, Any]:
    if DEBUG_ALLOW_NO_TELEGRAM and (not TELEGRAM_BOT_TOKEN):
        return {"id": 0, "username": "debug", "first_name": "Debug", "last_name": ""}
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Server missing TELEGRAM_BOT_TOKEN.")
    return verify_telegram_init_data(init_data, TELEGRAM_BOT_TOKEN)


# ==============================
# Simple in-memory rate limiter
# ==============================
@dataclass
class RateBucket:
    window_start: float
    count: int


_rate: Dict[str, RateBucket] = {}


def rate_key(request: Request, tg_user_id: Optional[int]) -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{ip}:{tg_user_id or 'na'}"


def check_rate_limit(request: Request, tg_user_id: Optional[int]) -> None:
    k = rate_key(request, tg_user_id)
    now = time.time()
    b = _rate.get(k)
    if not b or (now - b.window_start) > RATE_WINDOW_S:
        _rate[k] = RateBucket(window_start=now, count=1)
        return
    b.count += 1
    if b.count > RATE_MAX_REQ:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Slow down.")


# ==============================
# DB helpers
# ==============================
def _sb() -> Client:
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase not configured.")
    return sb


def db_one(table: str, **filters) -> Optional[Dict[str, Any]]:
    q = _sb().table(table).select("*")
    for k, v in filters.items():
        q = q.eq(k, v)
    res = q.limit(1).execute()
    if getattr(res, "data", None):
        return res.data[0]
    return None


def db_count(table: str) -> int:
    res = _sb().table(table).select("id", count="exact").execute()
    return int(res.count or 0)


def public_url_for_path(bucket: str, path: str) -> str:
    # Supabase public URL format:
    # {SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}
    return f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{bucket}/{path}"


# ==============================
# Models
# ==============================
class InitDataPayload(BaseModel):
    initData: str = Field(..., description="Telegram WebApp initData string")


class RegisterPayload(BaseModel):
    initData: str
    email: str
    display_name: str = Field(..., min_length=2, max_length=32)
    # bio_lock_token proves the user passed BioLock
    bio_lock_token: str


class BioLockStartPayload(BaseModel):
    initData: str


class BioLockSubmitPayload(BaseModel):
    initData: str
    token: str
    frames: List[str] = Field(..., min_items=3, max_items=8, description="Base64 JPEG frames captured from camera.")


class FeedPayload(BaseModel):
    initData: str
    limit: int = 12


class SwipePayload(BaseModel):
    initData: str
    target_user_id: str
    direction: str  # "LIGHT" | "SPITE"


class AllocateStatsPayload(BaseModel):
    initData: str
    STR: int = 0
    AGI: int = 0
    INT: int = 0
    VIT: int = 0


class BossActionPayload(BaseModel):
    initData: str
    action: str  # "ATTACK" | "HEAL"


# ==============================
# App
# ==============================
app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ALLOW_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "ts": utc_now().isoformat()}


# ==============================
# BioLock (camera-only, no file-picker uploads)
# ==============================
def make_biolock_token(tg_user_id: int) -> str:
    # short-lived token, bound to tg id and timestamp
    raw = f"{tg_user_id}:{int(time.time())}:{random.randint(100000,999999)}"
    return sha256_hex(raw)


@app.post("/auth/biolock/start")
def biolock_start(p: BioLockStartPayload, request: Request):
    user = require_telegram_user(p.initData)
    tg_id = int(user.get("id") or 0)
    check_rate_limit(request, tg_id)

    token = make_biolock_token(tg_id)
    # store token in supabase for verification (expires in 10 min)
    _sb().table("biolock_challenges").insert(
        {
            "token": token,
            "telegram_user_id": tg_id,
            "created_at": utc_now().isoformat(),
            "expires_at": (utc_now().timestamp() + 600),
            "used": False,
        }
    ).execute()

    return {"token": token, "expires_in": 600}


def biolock_verify_frames(frames: List[bytes]) -> Tuple[bool, str, Optional[bytes]]:
    """
    Server-side sanity checks:
    - Frames decode as images
    - Face-ish validation: we do lightweight checks WITHOUT heavy CV deps (keeps deploy stable).
      If you want stronger validation, enable OpenCV/MediaPipe server side (optional).
    For V24: we accept frames if:
      - at least 3 frames
      - average frame size > threshold
      - frames not identical (motion) -> hashes differ
    Also return one "best" frame to store as bio-lock portrait.
    """
    if len(frames) < 3:
        return False, "Need at least 3 camera frames.", None

    sizes = [len(b) for b in frames]
    if sum(sizes) / len(sizes) < 12_000:  # extremely tiny = probably blank
        return False, "Frames too small. Ensure camera is working and well-lit.", None

    hashes = [hashlib.sha256(b).hexdigest() for b in frames]
    uniq = len(set(hashes))
    if uniq < 2:
        return False, "No motion detected. Move your face slightly and try again.", None

    # Choose the largest frame as portrait
    best = frames[sizes.index(max(sizes))]
    return True, "ok", best


@app.post("/auth/biolock/submit")
def biolock_submit(p: BioLockSubmitPayload, request: Request):
    user = require_telegram_user(p.initData)
    tg_id = int(user.get("id") or 0)
    check_rate_limit(request, tg_id)

    # Validate token
    ch = db_one("biolock_challenges", token=p.token)
    if not ch or int(ch.get("telegram_user_id") or -1) != tg_id:
        raise HTTPException(status_code=401, detail="Invalid BioLock token.")
    if ch.get("used"):
        raise HTTPException(status_code=401, detail="BioLock token already used.")
    if float(ch.get("expires_at") or 0) < time.time():
        raise HTTPException(status_code=401, detail="BioLock token expired.")

    # Decode frames
    frames_bytes: List[bytes] = []
    try:
        for f in p.frames:
            frames_bytes.append(b64_to_bytes(f))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid frame encoding.")

    ok, reason, best = biolock_verify_frames(frames_bytes)
    if not ok:
        raise HTTPException(status_code=400, detail=f"BioLock failed: {reason}")

    # Mark token used (registration will bind it)
    _sb().table("biolock_challenges").update({"used": True}).eq("token", p.token).execute()

    # Store best frame in storage under a "pending" path; later registration moves/links it.
    # We keep the path on the token row so registration can attach it.
    path = f"pending/{tg_id}/{int(time.time())}.jpg"
    try:
        _sb().storage.from_(BIOLOCK_BUCKET).upload(
            path,
            best,
            {"content-type": "image/jpeg", "upsert": True},
        )
    except Exception as e:
        # If storage fails, still allow; but user won't have portrait.
        path = ""

    _sb().table("biolock_challenges").update({"portrait_path": path}).eq("token", p.token).execute()

    return {"ok": True, "token": p.token, "portrait_path": path}


# ==============================
# Registration / Login
# ==============================
def ensure_month_row(user_id: str) -> Dict[str, Any]:
    mk = month_key()
    row = db_one("monthly_stats", user_id=user_id, month=mk)
    if row:
        return row
    ins = {
        "user_id": user_id,
        "month": mk,
        "light": 0,
        "spite": 0,
        "STR": 0,
        "AGI": 0,
        "INT": 0,
        "VIT": 0,
        "created_at": utc_now().isoformat(),
        "updated_at": utc_now().isoformat(),
    }
    _sb().table("monthly_stats").insert(ins).execute()
    return ins


def calc_faction(light: int, spite: int) -> str:
    if light > spite:
        return "EUPHORIA"
    if spite > light:
        return "DISSONANCE"
    return random.choice(["EUPHORIA", "DISSONANCE"])


def refresh_monthly_faction(user_row: Dict[str, Any]) -> str:
    user_id = user_row["id"]
    ms = ensure_month_row(user_id)
    faction = calc_faction(int(ms.get("light") or 0), int(ms.get("spite") or 0))
    _sb().table("users").update({"faction": faction}).eq("id", user_id).execute()
    return faction


def create_or_bind_user(tg_user: Dict[str, Any], email: str, display_name: str, bio_lock_token: str) -> Dict[str, Any]:
    email_n = normalize_email(email)
    validate_email(email_n)

    tg_id = int(tg_user.get("id") or 0)
    tg_username = (tg_user.get("username") or "").strip()

    # Ensure BioLock token used and portrait available
    ch = db_one("biolock_challenges", token=bio_lock_token)
    if not ch or int(ch.get("telegram_user_id") or -1) != tg_id or not ch.get("used"):
        raise HTTPException(status_code=401, detail="BioLock not validated.")
    portrait_path = (ch.get("portrait_path") or "").strip()

    # If user already exists by telegram_user_id
    existing_by_tg = db_one("users", telegram_user_id=tg_id)
    if existing_by_tg:
        # If email mismatch, block (prevents swapping identities)
        if normalize_email(existing_by_tg.get("email") or "") != email_n:
            raise HTTPException(status_code=409, detail="This Telegram account is already bound to a different email.")
        # Update display name / username / portrait if missing
        upd = {
            "display_name": display_name,
            "telegram_username": tg_username,
            "updated_at": utc_now().isoformat(),
        }
        if portrait_path and not existing_by_tg.get("bio_lock_path"):
            upd["bio_lock_path"] = portrait_path
            upd["bio_lock_url"] = public_url_for_path(BIOLOCK_BUCKET, portrait_path)
        _sb().table("users").update(upd).eq("id", existing_by_tg["id"]).execute()
        return db_one("users", id=existing_by_tg["id"]) or existing_by_tg

    # If email already exists, bind Telegram if unbound
    existing_by_email = db_one("users", email=email_n)
    if existing_by_email:
        if existing_by_email.get("telegram_user_id") not in (None, 0):
            raise HTTPException(status_code=409, detail="Email already registered and bound to another Telegram account.")
        upd = {
            "telegram_user_id": tg_id,
            "telegram_username": tg_username,
            "display_name": display_name,
            "updated_at": utc_now().isoformat(),
        }
        if portrait_path:
            upd["bio_lock_path"] = portrait_path
            upd["bio_lock_url"] = public_url_for_path(BIOLOCK_BUCKET, portrait_path)
        _sb().table("users").update(upd).eq("id", existing_by_email["id"]).execute()
        return db_one("users", id=existing_by_email["id"]) or existing_by_email

    # New user: assign founder if within first 23 accounts
    cnt = db_count("users")
    is_founder = cnt < FOUNDER_LIMIT

    # Default faction (will be recalculated monthly once they swipe)
    faction = "UNSORTED"

    ins = {
        "email": email_n,
        "display_name": display_name,
        "telegram_user_id": tg_id,
        "telegram_username": tg_username,
        "faction": faction,
        "is_founder": is_founder,
        "created_at": utc_now().isoformat(),
        "updated_at": utc_now().isoformat(),
        "bio_lock_path": portrait_path or None,
        "bio_lock_url": public_url_for_path(BIOLOCK_BUCKET, portrait_path) if portrait_path else None,
    }
    res = _sb().table("users").insert(ins).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create user.")
    user_row = res.data[0]

    # Create default state
    _sb().table("user_state").insert(
        {
            "user_id": user_row["id"],
            "scans_total": 0,
            "cycle_index": 0,
            "scans_in_cycle": 0,
            "boss_spawn_at": random.randint(11, 20),
            "boss_active": False,
            "boss_defeated_this_cycle": False,
            "boss_hp": BOSS_BASE_HP,
            "boss_hp_max": BOSS_BASE_HP,
            "player_hp": PLAYER_BASE_HP,
            "player_hp_max": PLAYER_BASE_HP,
            "updated_at": utc_now().isoformat(),
        }
    ).execute()

    ensure_month_row(user_row["id"])
    return user_row


@app.post("/auth/register")
def register(p: RegisterPayload, request: Request):
    tg_user = require_telegram_user(p.initData)
    tg_id = int(tg_user.get("id") or 0)
    check_rate_limit(request, tg_id)

    user_row = create_or_bind_user(tg_user, p.email, p.display_name, p.bio_lock_token)
    # Ensure monthly faction calculation (after first interaction they will be sorted)
    return {"ok": True, "user": user_row}


@app.post("/auth/me")
def me(p: InitDataPayload, request: Request):
    tg_user = require_telegram_user(p.initData)
    tg_id = int(tg_user.get("id") or 0)
    check_rate_limit(request, tg_id)

    user_row = db_one("users", telegram_user_id=tg_id)
    if not user_row:
        return {"ok": True, "registered": False}
    st = db_one("user_state", user_id=user_row["id"])
    mk = month_key()
    ms = db_one("monthly_stats", user_id=user_row["id"], month=mk) or ensure_month_row(user_row["id"])
    return {"ok": True, "registered": True, "user": user_row, "state": st, "monthly": ms}


# ==============================
# Game: Feed & Swipe (new users instantly appear)
# ==============================
def get_user_or_401(init_data: str) -> Dict[str, Any]:
    tg_user = require_telegram_user(init_data)
    tg_id = int(tg_user.get("id") or 0)
    user_row = db_one("users", telegram_user_id=tg_id)
    if not user_row:
        raise HTTPException(status_code=401, detail="Not registered.")
    return user_row


def ensure_cycle(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Each user has scan cycles of 20.
    Boss MUST spawn once between scans 11..20 of each cycle.
    """
    scans_total = int(state.get("scans_total") or 0)
    cycle_index = scans_total // 20
    if int(state.get("cycle_index") or 0) != cycle_index:
        # New cycle: reset
        new_state = {
            "cycle_index": cycle_index,
            "scans_in_cycle": scans_total % 20,
            "boss_spawn_at": random.randint(11, 20),
            "boss_active": False,
            "boss_defeated_this_cycle": False,
            "boss_hp_max": BOSS_BASE_HP + cycle_index * 6,
            "boss_hp": BOSS_BASE_HP + cycle_index * 6,
            "player_hp_max": PLAYER_BASE_HP + cycle_index * 4,
            "player_hp": PLAYER_BASE_HP + cycle_index * 4,
            "updated_at": utc_now().isoformat(),
        }
        _sb().table("user_state").update(new_state).eq("user_id", state["user_id"]).execute()
        return db_one("user_state", user_id=state["user_id"]) or {**state, **new_state}
    return state


@app.post("/game/feed")
def feed(p: FeedPayload, request: Request):
    user_row = get_user_or_401(p.initData)
    check_rate_limit(request, int(user_row.get("telegram_user_id") or 0))

    # Pull other users that have a bio_lock_url (only legit faces)
    q = _sb().table("users").select("id,display_name,faction,bio_lock_url,created_at").neq("id", user_row["id"]).not_.is_("bio_lock_url", "null")
    res = q.order("created_at", desc=True).limit(max(4, min(50, p.limit))).execute()
    feed_list = res.data or []

    # If not enough users, return empty (client may fallback to fake)
    return {"ok": True, "feed": feed_list}


@app.post("/game/swipe")
def swipe(p: SwipePayload, request: Request):
    user_row = get_user_or_401(p.initData)
    check_rate_limit(request, int(user_row.get("telegram_user_id") or 0))

    direction = (p.direction or "").upper()
    if direction not in ("LIGHT", "SPITE"):
        raise HTTPException(status_code=400, detail="Invalid direction.")

    target = db_one("users", id=p.target_user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found.")

    # record swipe
    _sb().table("swipes").insert(
        {
            "user_id": user_row["id"],
            "target_user_id": target["id"],
            "direction": direction,
            "created_at": utc_now().isoformat(),
        }
    ).execute()

    # update monthly stats
    mk = month_key()
    ms = db_one("monthly_stats", user_id=user_row["id"], month=mk) or ensure_month_row(user_row["id"])
    light = int(ms.get("light") or 0)
    spite = int(ms.get("spite") or 0)
    if direction == "LIGHT":
        light += 1
    else:
        spite += 1

    _sb().table("monthly_stats").update(
        {"light": light, "spite": spite, "updated_at": utc_now().isoformat()}
    ).eq("user_id", user_row["id"]).eq("month", mk).execute()

    # faction changes monthly: recompute every swipe (cheap)
    faction = refresh_monthly_faction(user_row)

    # update user_state scans and boss triggering
    st = db_one("user_state", user_id=user_row["id"])
    if not st:
        raise HTTPException(status_code=500, detail="Missing user_state.")
    st = ensure_cycle(st)

    scans_total = int(st.get("scans_total") or 0) + 1
    scans_in_cycle = int(st.get("scans_in_cycle") or 0) + 1

    boss_spawn_at = int(st.get("boss_spawn_at") or 11)
    boss_defeated = bool(st.get("boss_defeated_this_cycle"))
    boss_active = bool(st.get("boss_active"))

    # Boss must spawn once per cycle between 11..20
    boss_triggered = False
    if (not boss_defeated) and (not boss_active) and (scans_in_cycle >= boss_spawn_at) and (scans_in_cycle <= 20):
        boss_active = True
        boss_triggered = True

    upd = {
        "scans_total": scans_total,
        "scans_in_cycle": scans_in_cycle,
        "boss_active": boss_active,
        "updated_at": utc_now().isoformat(),
    }
    _sb().table("user_state").update(upd).eq("user_id", user_row["id"]).execute()

    st2 = db_one("user_state", user_id=user_row["id"]) or {**st, **upd}

    return {
        "ok": True,
        "faction": faction,
        "boss_triggered": boss_triggered,
        "state": st2,
    }


# ==============================
# Stats allocation (points = monthly LIGHT+SPITE)
# ==============================
@app.post("/profile/allocate")
def allocate_stats(p: AllocateStatsPayload, request: Request):
    user_row = get_user_or_401(p.initData)
    check_rate_limit(request, int(user_row.get("telegram_user_id") or 0))

    mk = month_key()
    ms = db_one("monthly_stats", user_id=user_row["id"], month=mk) or ensure_month_row(user_row["id"])

    light = int(ms.get("light") or 0)
    spite = int(ms.get("spite") or 0)
    total_points = light + spite

    cur = {
        "STR": int(ms.get("STR") or 0),
        "AGI": int(ms.get("AGI") or 0),
        "INT": int(ms.get("INT") or 0),
        "VIT": int(ms.get("VIT") or 0),
    }
    add = {"STR": max(0, p.STR), "AGI": max(0, p.AGI), "INT": max(0, p.INT), "VIT": max(0, p.VIT)}

    new_vals = {k: cur[k] + add[k] for k in cur.keys()}
    spent = sum(new_vals.values())
    if spent > total_points:
        raise HTTPException(status_code=400, detail="Not enough points. Earn more LIGHT/SPITE.")

    _sb().table("monthly_stats").update({**new_vals, "updated_at": utc_now().isoformat()}).eq("user_id", user_row["id"]).eq("month", mk).execute()
    ms2 = db_one("monthly_stats", user_id=user_row["id"], month=mk) or {**ms, **new_vals}
    return {"ok": True, "monthly": ms2, "unspent": total_points - sum(new_vals.values())}


# ==============================
# Boss fight (server-authoritative)
# ==============================
def get_combat_sheet(user_id: str) -> Tuple[Dict[str, Any], Dict[str, int]]:
    st = db_one("user_state", user_id=user_id)
    if not st:
        raise HTTPException(status_code=500, detail="Missing user_state.")
    st = ensure_cycle(st)

    mk = month_key()
    ms = db_one("monthly_stats", user_id=user_id, month=mk) or ensure_month_row(user_id)

    stats = {
        "STR": int(ms.get("STR") or 0),
        "AGI": int(ms.get("AGI") or 0),
        "INT": int(ms.get("INT") or 0),
        "VIT": int(ms.get("VIT") or 0),
    }
    return st, stats


def clamp(n: int, a: int, b: int) -> int:
    return max(a, min(b, n))


@app.post("/boss/state")
def boss_state(p: InitDataPayload, request: Request):
    user_row = get_user_or_401(p.initData)
    check_rate_limit(request, int(user_row.get("telegram_user_id") or 0))

    st, stats = get_combat_sheet(user_row["id"])
    return {"ok": True, "state": st, "stats": stats, "boss_active": bool(st.get("boss_active"))}


@app.post("/boss/action")
def boss_action(p: BossActionPayload, request: Request):
    user_row = get_user_or_401(p.initData)
    check_rate_limit(request, int(user_row.get("telegram_user_id") or 0))

    st, stats = get_combat_sheet(user_row["id"])
    if not bool(st.get("boss_active")):
        raise HTTPException(status_code=400, detail="Boss is not active.")

    boss_hp = int(st.get("boss_hp") or 1)
    boss_hp_max = int(st.get("boss_hp_max") or 1)
    php = int(st.get("player_hp") or 1)
    php_max = int(st.get("player_hp_max") or 1)
    cycle_index = int(st.get("cycle_index") or 0)

    action = (p.action or "").upper()
    if action not in ("ATTACK", "HEAL"):
        raise HTTPException(status_code=400, detail="Invalid action.")

    def rand(a: int, b: int) -> int:
        return a + random.randint(0, max(0, b - a))

    # Damage formulas (simple but responsive)
    player_dmg = max(1, int(stats["STR"] * 0.7 + stats["INT"] * 0.4) + rand(0, 3))
    boss_dmg = max(1, 2 + cycle_index + int((st.get("scans_in_cycle") or 0) / 6) + rand(0, 2))

    log = []
    if action == "ATTACK":
        boss_hp = clamp(boss_hp - player_dmg, 0, boss_hp_max)
        log.append({"type": "hit", "dmg_to_boss": player_dmg})

        if boss_hp <= 0:
            # Victory: end boss, mark defeated, grant AP and loot
            _sb().table("user_state").update(
                {
                    "boss_hp": 0,
                    "boss_active": False,
                    "boss_defeated_this_cycle": True,
                    "updated_at": utc_now().isoformat(),
                }
            ).eq("user_id", user_row["id"]).execute()

            # Reward AP (stored on users.ap_total)
            ap_total = int(user_row.get("ap_total") or 0) + 10 + cycle_index
            _sb().table("users").update({"ap_total": ap_total, "updated_at": utc_now().isoformat()}).eq("id", user_row["id"]).execute()

            # Loot stub
            loot = random.choice(
                [
                    {"rarity": "COMMON", "name": "Sticker Pack"},
                    {"rarity": "RARE", "name": "Neon Hoodie"},
                    {"rarity": "EPIC", "name": "Void Runner Jacket"},
                    {"rarity": "MYTHIC", "name": "MYTHIC CRATE: DGLITCH"},
                ]
            )
            _sb().table("inventory").insert(
                {"user_id": user_row["id"], "item": loot, "created_at": utc_now().isoformat()}
            ).execute()

            return {"ok": True, "result": "VICTORY", "loot": loot, "ap_total": ap_total}

        # Retaliation
        php = clamp(php - boss_dmg, 0, php_max)
        log.append({"type": "retaliate", "dmg_to_player": boss_dmg})

    elif action == "HEAL":
        heal = max(10, int(php_max * 0.45) + stats["VIT"])
        php = clamp(php + heal, 0, php_max)
        log.append({"type": "heal", "amount": heal})

        # Boss still hits
        php = clamp(php - boss_dmg, 0, php_max)
        log.append({"type": "retaliate", "dmg_to_player": boss_dmg})

    # Persist combat hp
    _sb().table("user_state").update(
        {"boss_hp": boss_hp, "player_hp": php, "updated_at": utc_now().isoformat()}
    ).eq("user_id", user_row["id"]).execute()

    if php <= 0:
        # Defeat: end boss but NOT defeated -> boss remains active? We’ll end boss and require new cycle to re-trigger.
        _sb().table("user_state").update(
            {"boss_active": False, "updated_at": utc_now().isoformat()}
        ).eq("user_id", user_row["id"]).execute()
        return {"ok": True, "result": "DEFEAT", "log": log, "boss_hp": boss_hp, "player_hp": php}

    return {"ok": True, "result": "ONGOING", "log": log, "boss_hp": boss_hp, "player_hp": php, "boss_hp_max": boss_hp_max, "player_hp_max": php_max}


# ==============================
# Error handler (clean JSON)
# ==============================
@app.exception_handler(HTTPException)
def http_exc_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})


# ==============================
# Run local
# ==============================
if __name__ == "__main__":
    uvicorn.run("main_fixed:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)
