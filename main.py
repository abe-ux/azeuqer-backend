# AZEUQER TITANIUM — PROMPT 1 (FULL BACKEND)
# Includes:
# - /auth/login (referral capture + pioneer + verification)
# - /auth/biolock (stores bio_lock_url + triggers referral payout)
# - /auth/reset
# - /profile/vessel (stores body_asset_id)  ✅ FIXES YOUR MESSAGE
#
# Requirements:
# - users.user_id NOT NULL (your current schema)
# - users has columns added from Prompt 1 SQL (tg_id, role, verification_status, referral_status, body_asset_id, etc.)
# - game_config table exists (key,value)
# - optional referral_ledger table (recommended)

import os, json, time, urllib.parse, re
from typing import Any, Dict, Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client


# --------------------
# INIT
# --------------------
app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("CRITICAL: SUPABASE KEYS MISSING")
    supabase = None
else:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # prototype ok; lock down later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------
# UTILS
# --------------------
def _require_supabase():
    if supabase is None:
        raise RuntimeError("SUPABASE_NOT_CONFIGURED")

def _safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def validate_auth(init_data: str) -> Dict[str, Any]:
    """
    DEV MODE ONLY: does NOT verify Telegram initData signature.
    Extracts Telegram user JSON + start_param if present.
    """
    if init_data == "debug_mode":
        return {"id": 12345, "username": "Architect", "start_param": ""}

    try:
        parsed = dict(x.split("=", 1) for x in init_data.split("&"))
        user_json = urllib.parse.unquote(parsed.get("user", "{}"))
        u = json.loads(user_json) if user_json else {}
        uid = _safe_int(u.get("id"), 12345)
        uname = u.get("username") or u.get("first_name") or "Citizen"
        start_param = parsed.get("start_param") or parsed.get("startapp") or ""
        return {"id": uid, "username": uname, "start_param": start_param}
    except Exception:
        return {"id": 12345, "username": "Debug_User", "start_param": ""}

def _parse_referral(start_param: str) -> Optional[int]:
    """
    Accepts:
      ref_12345
      ref12345
    """
    if not start_param:
        return None
    s = start_param.strip()
    if s.startswith("ref_"):
        v = _safe_int(s.split("ref_", 1)[1], 0)
        return v if v > 0 else None
    if s.startswith("ref"):
        v = _safe_int(s.split("ref", 1)[1], 0)
        return v if v > 0 else None
    return None

def _get_config_int(key: str, default: int) -> int:
    """
    Uses public.game_config (key,value).
    """
    try:
        res = supabase.table("game_config").select("value").eq("key", key).limit(1).execute()
        if res.data:
            return _safe_int(res.data[0].get("value"), default)
    except Exception:
        pass
    return default

def _get_user(user_id: int) -> Optional[Dict[str, Any]]:
    res = supabase.table("users").select("*").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None

def _count_users_exact() -> int:
    """
    Uses PostgREST count=exact when available.
    """
    try:
        res = supabase.table("users").select("user_id", count="exact").limit(1).execute()
        return int(res.count or 0)
    except Exception:
        res = supabase.table("users").select("user_id").execute()
        return len(res.data or [])

def _ensure_user(user_id: int, username: str, referred_by: Optional[int]) -> Dict[str, Any]:
    """
    Creates user if missing.
    - user_id required NOT NULL
    - tg_id mirrored to user_id
    - pioneer protocol + verification_status
    - referral stored only at creation (immutable entry vector)
    """
    existing = _get_user(user_id)
    if existing:
        patch = {"last_active": "now()"}
        if username and existing.get("username") != username:
            patch["username"] = username
        if existing.get("tg_id") is None:
            patch["tg_id"] = user_id
        supabase.table("users").update(patch).eq("user_id", user_id).execute()
        return _get_user(user_id) or existing

    pioneer_cap = _get_config_int("pioneer_cap", 100)
    starting_energy = _get_config_int("starting_energy", 30)

    total = _count_users_exact()
    role = "PIONEER" if total < pioneer_cap else "CITIZEN"
    verification_status = "VERIFIED" if role == "PIONEER" else "PENDING"

    new_row: Dict[str, Any] = {
        "user_id": user_id,
        "tg_id": user_id,
        "username": username or "Citizen",

        "ap": 0,
        "faction": "UNSORTED",
        "equipped_item": None,
        "bio_lock_url": None,

        "role": role,
        "verification_status": verification_status,

        "last_active": "now()",
        "visibility_credits": 0,
        "votes_light": 0,
        "votes_spite": 0,
        "unassigned_stat": 0,

        "energy": starting_energy,
        "energy_updated_at": "now()",

        "body_asset_id": None,
        "referral_status": "NONE",
    }

    if referred_by and referred_by != user_id:
        new_row["referred_by"] = int(referred_by)
        new_row["referral_status"] = "PENDING"

    supabase.table("users").insert(new_row).execute()

    # create referral ledger if table exists
    if new_row.get("referred_by"):
        bonus = _get_config_int("referral_bonus_ap", 100)
        try:
            supabase.table("referral_ledger").insert({
                "inviter_user_id": int(new_row["referred_by"]),
                "invitee_user_id": user_id,
                "bonus_ap": bonus,
                "status": "PENDING"
            }).execute()
        except Exception:
            pass

    return _get_user(user_id) or new_row

def _award_ap(user_id: int, amount: int) -> int:
    u = _get_user(user_id)
    if not u:
        return 0
    new_ap = _safe_int(u.get("ap"), 0) + amount
    supabase.table("users").update({"ap": new_ap}).eq("user_id", user_id).execute()
    return new_ap

def _trigger_referral_payout(invitee_user_id: int) -> Dict[str, Any]:
    """
    Pays inviter + invitee only when invitee has bio_lock_url.
    Prevents double pay via referral_ledger when available.
    """
    invitee = _get_user(invitee_user_id)
    if not invitee:
        return {"status": "error", "msg": "INVITEE_NOT_FOUND"}

    inviter_id = invitee.get("referred_by")
    if not inviter_id:
        return {"status": "noop", "msg": "NO_REFERRAL"}

    if not invitee.get("bio_lock_url"):
        return {"status": "blocked", "msg": "BIOLOCK_NOT_DONE"}

    inviter_id = int(inviter_id)
    bonus = _get_config_int("referral_bonus_ap", 100)

    # ledger check
    try:
        led = (
            supabase.table("referral_ledger")
            .select("*")
            .eq("inviter_user_id", inviter_id)
            .eq("invitee_user_id", invitee_user_id)
            .limit(1)
            .execute()
        )
        if led.data and led.data[0].get("status") == "PAID":
            return {"status": "noop", "msg": "ALREADY_PAID"}
    except Exception:
        led = None

    inviter_ap = _award_ap(inviter_id, bonus)
    invitee_ap = _award_ap(invitee_user_id, bonus)

    supabase.table("users").update({"referral_status": "PAID"}).eq("user_id", invitee_user_id).execute()

    try:
        supabase.table("referral_ledger").update({"status": "PAID", "paid_at": "now()"}).eq("inviter_user_id", inviter_id).eq("invitee_user_id", invitee_user_id).execute()
    except Exception:
        pass

    return {
        "status": "PAID",
        "bonus": bonus,
        "inviter_user_id": inviter_id,
        "invitee_user_id": invitee_user_id,
        "inviter_ap": inviter_ap,
        "invitee_ap": invitee_ap
    }

def _sanitize_body_asset_id(asset: str) -> Optional[str]:
    """
    Allow only safe filenames like: body_low_mid_high_low.png
    Prevents path traversal and weird injection.
    """
    if not asset:
        return None
    asset = asset.strip()
    if len(asset) > 80:
        return None
    # strict pattern
    if re.fullmatch(r"body_(low|mid|high)_(low|mid|high)_(low|mid|high)_(low|mid|high)\.png", asset):
        return asset
    return None


# --------------------
# ENDPOINTS
# --------------------
@app.get("/")
def health_check():
    return {"status": "AZEUQER TITANIUM ONLINE", "ts": int(time.time())}

@app.post("/auth/login")
async def login(req: dict):
    """
    Prompt 1:
    - capture referral from Telegram start_param OR req.ref
    - pioneer assignment + verification status
    - updates last_active
    """
    try:
        _require_supabase()

        init_data = req.get("initData") or ""
        u_data = validate_auth(init_data)

        user_id = int(u_data["id"])
        username = u_data.get("username", "Citizen")

        # referral sources:
        start_param = u_data.get("start_param") or ""
        ref1 = _parse_referral(start_param)
        ref2 = _safe_int(req.get("ref"), 0) or None
        referred_by = ref1 or ref2

        user = _ensure_user(user_id, username, referred_by)

        # compatibility aliases
        user["user_id"] = user_id
        user["tg_id"] = user.get("tg_id") or user_id

        return {"status": "ok", "user": user}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    """
    Prompt 1:
    - stores bio_lock_url
    - triggers referral payout if pending
    """
    try:
        _require_supabase()

        u_data = validate_auth(initData)
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"), referred_by=None)

        content = await file.read()
        filename = f"{user_id}_{int(time.time())}.jpg"

        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            return {"status": "error", "msg": "BUCKET_FAIL"}

        supabase.table("users").update({"bio_lock_url": url, "last_active": "now()"}).eq("user_id", user_id).execute()

        payout = _trigger_referral_payout(user_id)

        u = _get_user(user_id) or {}
        u["user_id"] = user_id
        u["tg_id"] = u.get("tg_id") or user_id

        return {"status": "success", "url": url, "referral": payout, "user": u}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/reset")
async def reset_user(req: dict):
    """
    resets biometric + equipped.
    marks referral as BROKEN (anti-fraud baseline)
    """
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])

        supabase.table("users").update({
            "bio_lock_url": None,
            "equipped_item": None,
            "referral_status": "BROKEN"
        }).eq("user_id", user_id).execute()

        try:
            supabase.table("referral_ledger").update({"status": "BROKEN"}).eq("invitee_user_id", user_id).execute()
        except Exception:
            pass

        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/profile/vessel")
async def profile_vessel(req: dict):
    """
    ✅ FIX: This endpoint is required by Prompt 1 HTML.
    Saves body_asset_id to users.body_asset_id.
    """
    try:
        _require_supabase()

        init_data = req.get("initData") or ""
        u_data = validate_auth(init_data)
        user_id = int(u_data["id"])

        # Ensure user exists
        _ensure_user(user_id, u_data.get("username", "Citizen"), referred_by=None)

        body_asset_id = _sanitize_body_asset_id(req.get("body_asset_id") or "")
        if not body_asset_id:
            return {"status": "error", "msg": "BAD_BODY_ASSET_ID"}

        supabase.table("users").update({
            "body_asset_id": body_asset_id,
            "last_active": "now()"
        }).eq("user_id", user_id).execute()

        u = _get_user(user_id) or {}
        u["user_id"] = user_id
        u["tg_id"] = u.get("tg_id") or user_id

        return {"status": "ok", "user": u}
    except Exception as e:
        return {"status": "error", "msg": str(e)}
