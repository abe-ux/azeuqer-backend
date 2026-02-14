# AZEUQER TITANIUM — PROMPT 1.1 + 1.2 (+ keeps 1.3 strict Bio-Lock + /profile/vessel)
# - Referral Engine (deep link start_param)
# - Pioneer Protocol (first N users auto-VERIFIED)
# - Referral payout triggers AFTER Bio-Lock PASS
# - Anti-double pay via referral_ledger
# - Strict Bio-Lock via MediaPipe FaceDetection

import os, json, time, urllib.parse, re
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client

from PIL import Image
import numpy as np
import mediapipe as mp


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
# MediaPipe Face Detector
# --------------------
mp_face = mp.solutions.face_detection
FACE_DETECTOR = mp_face.FaceDetection(model_selection=1, min_detection_confidence=0.6)


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
    Accepts: ref_12345 OR ref12345
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
    try:
        res = supabase.table("users").select("user_id", count="exact").limit(1).execute()
        return int(res.count or 0)
    except Exception:
        res = supabase.table("users").select("user_id").execute()
        return len(res.data or [])

def _ensure_user(user_id: int, username: str, referred_by: Optional[int]) -> Dict[str, Any]:
    """
    Creates user if missing.
    Referral is stored ONLY at creation time (immutable entry vector).
    Pioneer Protocol:
      - if count < pioneer_cap => role=PIONEER, verification_status=VERIFIED
      - else role=CITIZEN, verification_status=PENDING
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

        # Bio-lock tracking (Prompt 1.3)
        "biolock_status": "GATE",
        "biolock_attempts": 0,
        "biolock_last_error": None,
        "biolock_last_at": None,
    }

    if referred_by and referred_by != user_id:
        new_row["referred_by"] = int(referred_by)
        new_row["referral_status"] = "PENDING"

    supabase.table("users").insert(new_row).execute()

    # create referral ledger row if table exists
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
    Pays inviter + invitee only when invitee has bio_lock_url and face scan PASS.
    Prevents double-pay with referral_ledger if available.
    """
    invitee = _get_user(invitee_user_id)
    if not invitee:
        return {"status": "error", "msg": "INVITEE_NOT_FOUND"}

    inviter_id = invitee.get("referred_by")
    if not inviter_id:
        return {"status": "noop", "msg": "NO_REFERRAL"}

    if not invitee.get("bio_lock_url") or invitee.get("biolock_status") != "PASS":
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
    if not asset:
        return None
    asset = asset.strip()
    if len(asset) > 80:
        return None
    if re.fullmatch(r"body_(low|mid|high)_(low|mid|high)_(low|mid|high)_(low|mid|high)\.png", asset):
        return asset
    return None

def _decode_image_bytes(content: bytes) -> Tuple[Optional[np.ndarray], Optional[str]]:
    from io import BytesIO
    try:
        img = Image.open(BytesIO(content))
        img = img.convert("RGB")
        arr = np.array(img)
        if arr.ndim != 3 or arr.shape[2] != 3:
            return None, "BAD_IMAGE"
        h, w, _ = arr.shape
        if h < 240 or w < 240:
            return None, "TOO_SMALL"
        return arr, None
    except Exception:
        return None, "BAD_IMAGE"

def _face_scan(rgb_np: np.ndarray) -> Tuple[bool, str]:
    results = FACE_DETECTOR.process(rgb_np)
    if not results.detections:
        return False, "NO_FACE"
    dets = results.detections
    if len(dets) > 1:
        return False, "MULTI_FACE"
    det = dets[0]
    score = float(det.score[0]) if det.score else 0.0
    if score < 0.6:
        return False, "LOW_CONF"
    bbox = det.location_data.relative_bounding_box
    w_rel = float(bbox.width or 0.0)
    h_rel = float(bbox.height or 0.0)
    if (w_rel * h_rel) < 0.12:
        return False, "FACE_TOO_SMALL"
    return True, "PASS"

def _biolock_fail(user_id: int, err_code: str):
    try:
        u = _get_user(user_id) or {}
        attempts = _safe_int(u.get("biolock_attempts"), 0) + 1
        supabase.table("users").update({
            "biolock_status": "FAIL",
            "biolock_attempts": attempts,
            "biolock_last_error": err_code,
            "biolock_last_at": "now()",
            "last_active": "now()"
        }).eq("user_id", user_id).execute()
    except Exception:
        pass

def _biolock_pass(user_id: int):
    try:
        u = _get_user(user_id) or {}
        attempts = _safe_int(u.get("biolock_attempts"), 0) + 1
        supabase.table("users").update({
            "biolock_status": "PASS",
            "biolock_attempts": attempts,
            "biolock_last_error": None,
            "biolock_last_at": "now()",
            "last_active": "now()"
        }).eq("user_id", user_id).execute()
    except Exception:
        pass


# --------------------
# ENDPOINTS
# --------------------
@app.get("/")
def health_check():
    return {"status": "AZEUQER TITANIUM ONLINE", "ts": int(time.time())}

@app.post("/auth/login")
async def login(req: dict):
    """
    Referral Engine:
      - reads start_param from initData OR req.start_param
      - if ref exists and user is new, stores referred_by and referral_status=PENDING
    Pioneer Protocol:
      - first N users => PIONEER + VERIFIED
    """
    try:
        _require_supabase()

        init_data = req.get("initData") or ""
        u_data = validate_auth(init_data)

        user_id = int(u_data["id"])
        username = u_data.get("username", "Citizen")

        # referral sources:
        start_param = u_data.get("start_param") or (req.get("start_param") or "")
        ref = _parse_referral(start_param)

        user = _ensure_user(user_id, username, ref)

        # response helpers
        user = _get_user(user_id) or user
        user["user_id"] = user_id
        user["tg_id"] = user.get("tg_id") or user_id

        return {"status": "ok", "user": user}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    """
    Strict Bio-Lock:
      - decode image
      - face scan (exactly 1 face, big enough)
      - PASS => upload storage + bio_lock_url + biolock_status=PASS
      - triggers referral payout if referral_status pending
    """
    try:
        _require_supabase()

        u_data = validate_auth(initData)
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"), referred_by=None)

        content = await file.read()

        rgb, decode_err = _decode_image_bytes(content)
        if decode_err:
            _biolock_fail(user_id, decode_err)
            return {"status": "error", "msg": decode_err}

        ok, scan_code = _face_scan(rgb)
        if not ok:
            _biolock_fail(user_id, scan_code)
            return {"status": "error", "msg": scan_code}

        # PASS -> upload
        filename = f"{user_id}_{int(time.time())}.jpg"
        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            _biolock_fail(user_id, "BUCKET_FAIL")
            return {"status": "error", "msg": "BUCKET_FAIL"}

        # save + mark pass
        supabase.table("users").update({"bio_lock_url": url}).eq("user_id", user_id).execute()
        _biolock_pass(user_id)

        payout = _trigger_referral_payout(user_id)

        u = _get_user(user_id) or {}
        u["user_id"] = user_id
        u["tg_id"] = u.get("tg_id") or user_id

        return {"status": "success", "url": url, "referral": payout, "user": u}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/reset")
async def reset_user(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])

        supabase.table("users").update({
            "bio_lock_url": None,
            "equipped_item": None,
            "referral_status": "BROKEN",
            "biolock_status": "GATE",
            "biolock_last_error": None,
            "biolock_last_at": "now()",
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
    try:
        _require_supabase()

        init_data = req.get("initData") or ""
        u_data = validate_auth(init_data)
        user_id = int(u_data["id"])
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
