# main.py — AZEUQER Backend (V24, "Void Architect" hardened)
# FastAPI backend for Telegram WebApp game:
# - Email capture stored in Supabase
# - Telegram user_id binding
# - Founder assignment (first 23)
# - Bio-lock face capture verification (camera-only; no uploads UI-side)
# - Scan feed includes newly registered users immediately
# - Duplicate email handling + basic bot protection (rate limiting + honeypot + initData verification)
# - Monthly faction rollover + stat points from LIGHT/SPITE actions
#
# Deploy: uvicorn main:app --host 0.0.0.0 --port $PORT

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Supabase (python client)
from supabase import create_client, Client

# Face detection (MediaPipe)
import numpy as np
from PIL import Image

try:
    import mediapipe as mp  # type: ignore
except Exception:  # pragma: no cover
    mp = None


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

APP_NAME = "AZEUQER"
API_PREFIX = "/api"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# If you want to allow local testing without Telegram initData verification:
ALLOW_DEBUG_INITDATA = os.getenv("ALLOW_DEBUG_INITDATA", "0").strip() == "1"

# Founder logic
FOUNDER_LIMIT = int(os.getenv("FOUNDER_LIMIT", "23"))

# Rate limiting (very lightweight, process-memory)
RL_WINDOW_SEC = int(os.getenv("RL_WINDOW_SEC", "60"))
RL_MAX_REQS = int(os.getenv("RL_MAX_REQS", "120"))

# CORS
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

# Basic email pattern (avoids pydantic EmailStr -> email_validator dependency)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def month_key(dt: Optional[datetime] = None) -> str:
    d = dt or utc_now()
    return f"{d.year:04d}-{d.month:02d}"


# -----------------------------------------------------------------------------
# Supabase client
# -----------------------------------------------------------------------------

_sb: Optional[Client] = None


def supabase() -> Client:
    global _sb
    if _sb is not None:
        return _sb
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase env not set: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")
    _sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _sb


# -----------------------------------------------------------------------------
# Telegram initData verification
# -----------------------------------------------------------------------------

def _parse_init_data(init_data: str) -> Dict[str, str]:
    # initData is querystring-like: key=value&key2=value2...
    out: Dict[str, str] = {}
    for part in init_data.split("&"):
        if not part:
            continue
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k] = v
    return out


def verify_telegram_init_data(init_data: str) -> Tuple[int, Dict[str, Any]]:
    """
    Returns (telegram_user_id, user_obj)
    Raises HTTPException if invalid.
    Telegram verification:
      secret_key = sha256(bot_token)
      data_check_string = "\n".join(sorted(k=v for k,v in pairs if k != 'hash'))
      hash = hmac_sha256(secret_key, data_check_string).hexdigest()
    """
    if not init_data:
        raise HTTPException(status_code=400, detail="Missing initData.")

    if init_data == "debug_mode":
        if not ALLOW_DEBUG_INITDATA:
            raise HTTPException(status_code=400, detail="Debug initData not allowed.")
        # In debug, caller must provide X-Debug-TgId
        # (we validate in request handler)
        return -1, {"id": -1}

    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Server misconfigured: TELEGRAM_BOT_TOKEN not set.")

    params = _parse_init_data(init_data)
    their_hash = params.get("hash", "")
    if not their_hash:
        raise HTTPException(status_code=400, detail="initData missing hash.")

    # Telegram recommends checking auth_date freshness (optional). We'll allow 24h by default.
    try:
        auth_date = int(params.get("auth_date", "0"))
    except Exception:
        auth_date = 0
    if auth_date:
        if abs(int(time.time()) - auth_date) > 24 * 3600:
            raise HTTPException(status_code=400, detail="initData expired.")

    # Build data_check_string
    kv = [f"{k}={v}" for k, v in params.items() if k != "hash"]
    kv.sort()
    data_check_string = "\n".join(kv)

    secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, their_hash):
        raise HTTPException(status_code=401, detail="Invalid initData hash.")

    # Parse user JSON (urlencoded)
    import urllib.parse, json
    user_raw = params.get("user", "")
    if not user_raw:
        raise HTTPException(status_code=400, detail="initData missing user.")
    user_json = urllib.parse.unquote(user_raw)
    try:
        user_obj = json.loads(user_json)
    except Exception:
        raise HTTPException(status_code=400, detail="initData user json invalid.")

    tg_id = int(user_obj.get("id", 0) or 0)
    if tg_id <= 0:
        raise HTTPException(status_code=400, detail="Telegram user id missing/invalid.")
    return tg_id, user_obj


# -----------------------------------------------------------------------------
# Basic in-memory rate limiter (per IP)
# -----------------------------------------------------------------------------

_rl: Dict[str, Tuple[int, float]] = {}  # ip -> (count, window_start)


def rate_limit(ip: str) -> None:
    now = time.time()
    count, start = _rl.get(ip, (0, now))
    if now - start > RL_WINDOW_SEC:
        _rl[ip] = (1, now)
        return
    count += 1
    _rl[ip] = (count, start)
    if count > RL_MAX_REQS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")


# -----------------------------------------------------------------------------
# Face verification
# -----------------------------------------------------------------------------

def _decode_data_url(data_url: str) -> bytes:
    # Accept "data:image/jpeg;base64,..." or raw base64
    if not data_url:
        raise HTTPException(status_code=400, detail="Missing face_image.")
    if data_url.startswith("data:"):
        try:
            b64 = data_url.split(",", 1)[1]
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid data URL.")
    else:
        b64 = data_url
    try:
        return base64.b64decode(b64, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 face_image.")


def verify_face_image(image_bytes: bytes) -> bool:
    """
    Returns True if at least one face is detected.
    This is not liveness. It's a strict "real face present" gate.
    """
    # If mediapipe not installed, fail closed (safer)
    if mp is None:
        return False

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)
    except Exception:
        return False

    try:
        face_detection = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.6)
        res = face_detection.process(arr)
        face_detection.close()
        if not res.detections:
            return False
        # Optionally require exactly one face (reduces spoof/abuse)
        if len(res.detections) != 1:
            return False
        return True
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------

class RegisterPayload(BaseModel):
    initData: str = Field(..., description="Telegram WebApp initData (querystring)")
    email: str = Field(..., description="User email")
    face_image: str = Field(..., description="Camera-captured frame as dataURL base64")
    hp: str = Field("", description="Honeypot field; must be empty")  # bot trap


class SwipePayload(BaseModel):
    initData: str
    target_tg_id: int
    direction: str  # LIGHT | SPITE


class AllocatePayload(BaseModel):
    initData: str
    STR: int = 0
    AGI: int = 0
    INT: int = 0
    VIT: int = 0


# -----------------------------------------------------------------------------
# Supabase helpers
# -----------------------------------------------------------------------------

def sb_one(resp) -> Dict[str, Any]:
    data = resp.data
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def get_user(tg_id: int) -> Optional[Dict[str, Any]]:
    r = supabase().table("azeuqer_users").select("*").eq("telegram_user_id", tg_id).limit(1).execute()
    row = sb_one(r)
    return row or None


def count_users() -> int:
    r = supabase().table("azeuqer_users").select("telegram_user_id", count="exact").limit(1).execute()
    # supabase-py puts count in r.count
    return int(getattr(r, "count", 0) or 0)


def ensure_month_rollover(user: Dict[str, Any]) -> Dict[str, Any]:
    """
    If month changed, compute faction based on last month light/spite,
    convert last month's actions into unspent_points, reset monthly counters.
    """
    cur = month_key()
    if (user.get("month_key") or "") == cur:
        return user

    light = int(user.get("light_month") or 0)
    spite = int(user.get("spite_month") or 0)
    unspent = int(user.get("unspent_points") or 0)

    # Decide faction for new month based on last month totals
    if light > spite:
        faction = "EUPHORIA"
    elif spite > light:
        faction = "DISSONANCE"
    else:
        # tie -> keep last faction unless UNSORTED
        prev = (user.get("faction") or "UNSORTED").upper()
        faction = prev if prev in ("EUPHORIA", "DISSONANCE") else ("EUPHORIA" if (hash(user["telegram_user_id"]) % 2 == 0) else "DISSONANCE")

    # Convert last month actions to points
    unspent += (light + spite)

    upd = {
        "month_key": cur,
        "faction": faction,
        "light_month": 0,
        "spite_month": 0,
        "unspent_points": unspent,
        "updated_at": utc_now().isoformat(),
    }
    r = supabase().table("azeuqer_users").update(upd).eq("telegram_user_id", user["telegram_user_id"]).execute()
    return sb_one(r) or {**user, **upd}


def assign_founder_flag_if_needed(tg_id: int) -> bool:
    """
    First 23 users are founders. Determined at registration time by count before insert.
    """
    n = count_users()
    return n < FOUNDER_LIMIT


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------

app = FastAPI(title=APP_NAME, version="24.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if "*" in CORS_ORIGINS else CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"ok": True, "name": APP_NAME, "ts": utc_now().isoformat()}


@app.get(f"{API_PREFIX}/health")
def health():
    return {"ok": True, "ts": utc_now().isoformat()}


def _get_ip(req: Request) -> str:
    # Render / proxies may set X-Forwarded-For
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


def _resolve_tg(req: Request, init_data: str) -> Tuple[int, Dict[str, Any]]:
    tg_id, user_obj = verify_telegram_init_data(init_data)
    if init_data == "debug_mode" and ALLOW_DEBUG_INITDATA:
        dbg = req.headers.get("x-debug-tgid", "").strip()
        if not dbg:
            raise HTTPException(status_code=400, detail="Missing x-debug-tgid for debug_mode.")
        try:
            tg_id = int(dbg)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid x-debug-tgid.")
        user_obj = {"id": tg_id, "username": req.headers.get("x-debug-username", "debugger")}
    return tg_id, user_obj


@app.post(f"{API_PREFIX}/register")
def register(payload: RegisterPayload, req: Request):
    ip = _get_ip(req)
    rate_limit(ip)

    # Bot trap
    if payload.hp:
        raise HTTPException(status_code=400, detail="Bot rejected.")

    if not EMAIL_RE.match(payload.email.strip()):
        raise HTTPException(status_code=400, detail="Invalid email format.")

    tg_id, tg_user = _resolve_tg(req, payload.initData)

    # Verify face (camera frame)
    img_bytes = _decode_data_url(payload.face_image)
    if not verify_face_image(img_bytes):
        raise HTTPException(status_code=400, detail="Bio-lock failed: no single face detected.")

    sb = supabase()

    # Duplicate email check
    existing_email = sb.table("azeuqer_users").select("telegram_user_id").eq("email", payload.email.lower()).limit(1).execute()
    if sb_one(existing_email):
        raise HTTPException(status_code=409, detail="Email already registered.")

    # If user exists by tg_id, update email + flags
    existing = get_user(tg_id)
    now = utc_now().isoformat()

    if existing:
        upd = {
            "email": payload.email.lower(),
            "bio_lock_passed": True,
            "bio_lock_captured_at": now,
            "last_seen_at": now,
            "tg_username": tg_user.get("username"),
            "tg_first_name": tg_user.get("first_name"),
            "tg_last_name": tg_user.get("last_name"),
            "updated_at": now,
        }
        r = sb.table("azeuqer_users").update(upd).eq("telegram_user_id", tg_id).execute()
        row = sb_one(r)
        row = ensure_month_rollover(row)
        return {"ok": True, "user": _public_user(row)}

    # New user insert
    is_founder = assign_founder_flag_if_needed(tg_id)

    row = {
        "telegram_user_id": tg_id,
        "email": payload.email.lower(),
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "is_founder": is_founder,
        "faction": "UNSORTED",
        "month_key": month_key(),
        "light_month": 0,
        "spite_month": 0,
        "unspent_points": 0,
        "stat_str": 0,
        "stat_agi": 0,
        "stat_int": 0,
        "stat_vit": 0,
        "hp_base": 40,
        "ap": 0,
        "scans_total": 0,
        "bio_lock_passed": True,
        "bio_lock_captured_at": now,
        "tg_username": tg_user.get("username"),
        "tg_first_name": tg_user.get("first_name"),
        "tg_last_name": tg_user.get("last_name"),
    }

    try:
        ins = sb.table("azeuqer_users").insert(row).execute()
    except Exception as e:
        # If insert fails due to constraints, surface a clean message
        raise HTTPException(status_code=500, detail=f"Supabase insert failed: {str(e)}")

    user = sb_one(ins)
    user = ensure_month_rollover(user)
    return {"ok": True, "user": _public_user(user)}


def _public_user(u: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "telegram_user_id": u.get("telegram_user_id"),
        "email": u.get("email"),
        "is_founder": bool(u.get("is_founder")),
        "faction": (u.get("faction") or "UNSORTED"),
        "month_key": (u.get("month_key") or month_key()),
        "light_month": int(u.get("light_month") or 0),
        "spite_month": int(u.get("spite_month") or 0),
        "unspent_points": int(u.get("unspent_points") or 0),
        "stats": {
            "STR": int(u.get("stat_str") or 0),
            "AGI": int(u.get("stat_agi") or 0),
            "INT": int(u.get("stat_int") or 0),
            "VIT": int(u.get("stat_vit") or 0),
        },
        "hp_base": int(u.get("hp_base") or 40),
        "ap": int(u.get("ap") or 0),
        "scans_total": int(u.get("scans_total") or 0),
        "bio_lock_passed": bool(u.get("bio_lock_passed")),
        "created_at": u.get("created_at"),
    }


@app.post(f"{API_PREFIX}/me")
def me(payload: Dict[str, str], req: Request):
    ip = _get_ip(req)
    rate_limit(ip)

    init_data = payload.get("initData", "")
    tg_id, _ = _resolve_tg(req, init_data)

    user = get_user(tg_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered.")
    user = ensure_month_rollover(user)

    # Update last_seen
    supabase().table("azeuqer_users").update({"last_seen_at": utc_now().isoformat()}).eq("telegram_user_id", tg_id).execute()
    return {"ok": True, "user": _public_user(user)}


@app.post(f"{API_PREFIX}/feed")
def feed(payload: Dict[str, str], req: Request):
    ip = _get_ip(req)
    rate_limit(ip)

    init_data = payload.get("initData", "")
    tg_id, _ = _resolve_tg(req, init_data)

    user = get_user(tg_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered.")
    user = ensure_month_rollover(user)

    # Fetch most recent users (new registrants appear immediately)
    # Exclude self; show last 50, then shuffle client-side if desired.
    r = supabase().table("azeuqer_users").select(
        "telegram_user_id,tg_username,tg_first_name,tg_last_name,faction,created_at"
    ).neq("telegram_user_id", tg_id).order("created_at", desc=True).limit(50).execute()

    others = r.data or []
    # Map to feed items
    feed_items = []
    for o in others:
        name = o.get("tg_username") or " ".join([x for x in [o.get("tg_first_name"), o.get("tg_last_name")] if x]) or "TARGET"
        feed_items.append({
            "telegram_user_id": int(o["telegram_user_id"]),
            "username": name,
            "faction": (o.get("faction") or "UNSORTED"),
            # For now: frontend uses placeholders; later: serve bio-lock blurred thumbnails from storage.
            "bio_lock_url": f"https://picsum.photos/seed/az_{int(o['telegram_user_id'])}/900/1100",
        })

    return {"ok": True, "me": _public_user(user), "feed": feed_items}


@app.post(f"{API_PREFIX}/swipe")
def swipe(payload: SwipePayload, req: Request):
    ip = _get_ip(req)
    rate_limit(ip)

    tg_id, _ = _resolve_tg(req, payload.initData)

    direction = (payload.direction or "").upper().strip()
    if direction not in ("LIGHT", "SPITE"):
        raise HTTPException(status_code=400, detail="direction must be LIGHT or SPITE")

    user = get_user(tg_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered.")
    user = ensure_month_rollover(user)

    # Validate target exists (optional; if missing, still allow as a "ghost")
    tgt = get_user(int(payload.target_tg_id))
    if not tgt:
        # allow but mark as unknown in scans table
        pass

    # Apply rewards and monthly counters:
    # - +1 AP each swipe
    # - +1 scans_total
    # - +1 unspent_points (stat point)
    upd = {
        "ap": int(user.get("ap") or 0) + 1,
        "scans_total": int(user.get("scans_total") or 0) + 1,
        "unspent_points": int(user.get("unspent_points") or 0) + 1,
        "updated_at": utc_now().isoformat(),
    }
    if direction == "LIGHT":
        upd["light_month"] = int(user.get("light_month") or 0) + 1
    else:
        upd["spite_month"] = int(user.get("spite_month") or 0) + 1

    r = supabase().table("azeuqer_users").update(upd).eq("telegram_user_id", tg_id).execute()
    user2 = sb_one(r)
    user2 = ensure_month_rollover(user2)

    # Record swipe (best-effort; ignore failures)
    try:
        supabase().table("azeuqer_swipes").insert({
            "scanner_tg_id": tg_id,
            "target_tg_id": int(payload.target_tg_id),
            "direction": direction,
            "created_at": utc_now().isoformat(),
        }).execute()
    except Exception:
        pass

    return {"ok": True, "user": _public_user(user2)}


@app.post(f"{API_PREFIX}/allocate")
def allocate(payload: AllocatePayload, req: Request):
    ip = _get_ip(req)
    rate_limit(ip)

    tg_id, _ = _resolve_tg(req, payload.initData)

    user = get_user(tg_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered.")
    user = ensure_month_rollover(user)

    add = {
        "STR": max(0, int(payload.STR)),
        "AGI": max(0, int(payload.AGI)),
        "INT": max(0, int(payload.INT)),
        "VIT": max(0, int(payload.VIT)),
    }
    total_add = sum(add.values())
    unspent = int(user.get("unspent_points") or 0)
    if total_add <= 0:
        return {"ok": True, "user": _public_user(user)}
    if total_add > unspent:
        raise HTTPException(status_code=400, detail="Not enough unspent points.")

    upd = {
        "stat_str": int(user.get("stat_str") or 0) + add["STR"],
        "stat_agi": int(user.get("stat_agi") or 0) + add["AGI"],
        "stat_int": int(user.get("stat_int") or 0) + add["INT"],
        "stat_vit": int(user.get("stat_vit") or 0) + add["VIT"],
        "unspent_points": unspent - total_add,
        "updated_at": utc_now().isoformat(),
    }
    r = supabase().table("azeuqer_users").update(upd).eq("telegram_user_id", tg_id).execute()
    user2 = sb_one(r)
    user2 = ensure_month_rollover(user2)
    return {"ok": True, "user": _public_user(user2)}


# NOTE: Boss combat is currently client-sim. If you want server authoritative boss runs,
# we can add /boss/start, /boss/attack, /boss/heal endpoints and persist runs in Supabase.
