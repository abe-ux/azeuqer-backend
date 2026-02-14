# AZEUQER TITANIUM — PROMPT 1 (1.1–1.4) + PROMPT 2 (SOCIAL & FEED) + POLISH
# Adds:
# - /game/feed_debug (counts why feed empty)
# Keeps:
# - /game/feed, /game/swipe, /game/ping

import os, json, time, urllib.parse, random
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime, timedelta, timezone

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
    allow_origins=["*"],  # prototype ok
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

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _utc_day_start(dt: Optional[datetime] = None) -> datetime:
    dt = dt or _utc_now()
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)

def validate_auth(init_data: str) -> Dict[str, Any]:
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
    existing = _get_user(user_id)
    if existing:
        patch = {"last_active": "now()"}
        if username and existing.get("username") != username:
            patch["username"] = username
        if existing.get("tg_id") is None:
            patch["tg_id"] = user_id
        if existing.get("visibility_credits") is None:
            patch["visibility_credits"] = 10
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

        "visibility_credits": 10,
        "votes_light": 0,
        "votes_spite": 0,
        "unassigned_stat": 0,

        "energy": starting_energy,
        "energy_updated_at": "now()",

        "body_asset_id": None,
        "vessel_work": 50,
        "vessel_pace": 50,
        "vessel_mind": 50,
        "vessel_vibe": 50,

        "referral_status": "NONE",

        "biolock_status": "GATE",
        "biolock_attempts": 0,
        "biolock_last_error": None,
        "biolock_last_at": None,
    }

    if referred_by and referred_by != user_id:
        new_row["referred_by"] = int(referred_by)
        new_row["referral_status"] = "PENDING"

    supabase.table("users").insert(new_row).execute()

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
        pass

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

def _make_body_asset_id(work: int, pace: int, mind: int, vibe: int) -> str:
    def b(v:int)->str:
        if v <= 33: return "low"
        if v <= 66: return "mid"
        return "high"
    return f"body_{b(work)}_{b(pace)}_{b(mind)}_{b(vibe)}.png"


# --------------------
# PROMPT 2 HELPERS
# --------------------
def _require_verified_and_biolocked(user: Dict[str, Any]) -> Optional[str]:
    if (user.get("biolock_status") != "PASS") or (not user.get("bio_lock_url")):
        return "BIOLOCK_REQUIRED"
    if user.get("verification_status") != "VERIFIED":
        return "NOT_VERIFIED"
    return None

def _clamp_nonneg(n: int) -> int:
    return n if n >= 0 else 0

def _get_swipes_today_count(user_id: int) -> int:
    day = _utc_day_start()
    res = (
        supabase.table("swipes")
        .select("id")
        .eq("swiper_user_id", user_id)
        .gte("created_at", day.isoformat())
        .execute()
    )
    return len(res.data or [])

def _get_swiped_target_ids_today(user_id: int) -> List[int]:
    day = _utc_day_start()
    res = (
        supabase.table("swipes")
        .select("target_user_id")
        .eq("swiper_user_id", user_id)
        .gte("created_at", day.isoformat())
        .execute()
    )
    return [int(r["target_user_id"]) for r in (res.data or []) if r.get("target_user_id") is not None]


# --------------------
# ENDPOINTS
# --------------------
@app.get("/")
def health_check():
    return {"status": "AZEUQER TITANIUM ONLINE", "ts": int(time.time())}

@app.post("/auth/login")
async def login(req: dict):
    try:
        _require_supabase()
        init_data = req.get("initData") or ""
        u_data = validate_auth(init_data)
        user_id = int(u_data["id"])
        username = u_data.get("username", "Citizen")
        start_param = u_data.get("start_param") or (req.get("start_param") or "")
        ref = _parse_referral(start_param)

        user = _ensure_user(user_id, username, ref)
        user = _get_user(user_id) or user
        user["user_id"] = user_id
        user["tg_id"] = user.get("tg_id") or user_id
        return {"status": "ok", "user": user}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
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

        filename = f"{user_id}_{int(time.time())}.jpg"
        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception:
            _biolock_fail(user_id, "BUCKET_FAIL")
            return {"status": "error", "msg": "BUCKET_FAIL"}

        supabase.table("users").update({"bio_lock_url": url}).eq("user_id", user_id).execute()
        _biolock_pass(user_id)

        payout = _trigger_referral_payout(user_id)

        u = _get_user(user_id) or {}
        u["user_id"] = user_id
        u["tg_id"] = u.get("tg_id") or user_id
        return {"status": "success", "url": url, "referral": payout, "user": u}
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

        work = max(0, min(100, _safe_int(req.get("work"), 50)))
        pace = max(0, min(100, _safe_int(req.get("pace"), 50)))
        mind = max(0, min(100, _safe_int(req.get("mind"), 50)))
        vibe = max(0, min(100, _safe_int(req.get("vibe"), 50)))

        body_asset_id = _make_body_asset_id(work, pace, mind, vibe)

        supabase.table("users").update({
            "vessel_work": work,
            "vessel_pace": pace,
            "vessel_mind": mind,
            "vessel_vibe": vibe,
            "body_asset_id": body_asset_id,
            "last_active": "now()"
        }).eq("user_id", user_id).execute()

        u = _get_user(user_id) or {}
        u["user_id"] = user_id
        u["tg_id"] = u.get("tg_id") or user_id
        return {"status": "ok", "user": u}
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
            "body_asset_id": None,
            "vessel_work": 50,
            "vessel_pace": 50,
            "vessel_mind": 50,
            "vessel_vibe": 50,
            "visibility_credits": 10,
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

@app.post("/game/ping")
async def game_ping(req: dict):
    try:
        _require_supabase()
        init_data = req.get("initData") or ""
        u_data = validate_auth(init_data)
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"), referred_by=None)
        supabase.table("users").update({"last_active": "now()"}).eq("user_id", user_id).execute()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/game/feed")
async def game_feed(req: dict):
    try:
        _require_supabase()
        init_data = req.get("initData") or ""
        u_data = validate_auth(init_data)
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"), referred_by=None)

        me = _get_user(user_id)
        if not me:
            return {"status": "error", "msg": "USER_NOT_FOUND", "feed": []}

        supabase.table("users").update({"last_active": "now()"}).eq("user_id", user_id).execute()

        guard = _require_verified_and_biolocked(me)
        if guard:
            return {"status": "error", "msg": guard, "feed": []}

        swiped_ids = set(_get_swiped_target_ids_today(user_id))

        now = _utc_now()
        cutoff_5m = (now - timedelta(minutes=5)).isoformat()
        cutoff_1h = (now - timedelta(hours=1)).isoformat()

        def fetch_candidates(cutoff_iso: str) -> List[Dict[str, Any]]:
            res = (
                supabase.table("users")
                .select("user_id,username,bio_lock_url,body_asset_id,role,verification_status,last_active,visibility_credits")
                .eq("verification_status", "VERIFIED")
                .eq("biolock_status", "PASS")
                .not_.is_("bio_lock_url", "null")
                .gt("visibility_credits", 0)
                .gte("last_active", cutoff_iso)
                .limit(80)
                .execute()
            )
            rows = res.data or []
            rows = [r for r in rows if int(r["user_id"]) != user_id and int(r["user_id"]) not in swiped_ids]
            random.shuffle(rows)
            return rows[:16]

        feed = fetch_candidates(cutoff_5m)
        used_fallback = False
        if len(feed) < 5:
            feed = fetch_candidates(cutoff_1h)
            used_fallback = True

        return {"status": "ok", "feed": feed, "fallback": used_fallback}
    except Exception as e:
        return {"status": "error", "msg": str(e), "feed": []}

@app.post("/game/swipe")
async def game_swipe(req: dict):
    try:
        _require_supabase()
        init_data = req.get("initData") or ""
        direction = (req.get("direction") or "").upper().strip()
        target_id = _safe_int(req.get("target_id"), 0)

        if direction not in ("LIGHT", "SPITE"):
            return {"status": "error", "msg": "BAD_DIRECTION"}
        if target_id <= 0:
            return {"status": "error", "msg": "BAD_TARGET"}

        u_data = validate_auth(init_data)
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"), referred_by=None)

        me = _get_user(user_id)
        if not me:
            return {"status": "error", "msg": "USER_NOT_FOUND"}

        guard = _require_verified_and_biolocked(me)
        if guard:
            return {"status": "error", "msg": guard}

        if user_id == target_id:
            return {"status": "error", "msg": "NO_SELF_SWIPE"}

        target = _get_user(target_id)
        if not target or target.get("verification_status") != "VERIFIED" or target.get("biolock_status") != "PASS" or not target.get("bio_lock_url"):
            return {"status": "error", "msg": "TARGET_INVALID"}

        count_today = _get_swipes_today_count(user_id)
        energy_cost = 0
        if count_today >= 20:
            energy_cost = 1

        me_energy = _safe_int(me.get("energy"), 0)
        if energy_cost > 0 and me_energy < energy_cost:
            return {"status": "error", "msg": "NO_ENERGY", "need": energy_cost, "energy": me_energy, "swipes_today": count_today}

        try:
            supabase.table("swipes").insert({
                "swiper_user_id": user_id,
                "target_user_id": target_id,
                "direction": direction,
                "energy_cost": energy_cost
            }).execute()
        except Exception:
            return {"status": "error", "msg": "ALREADY_SWIPED_TODAY"}

        new_ap = _safe_int(me.get("ap"), 0) + 1
        new_vis = _safe_int(me.get("visibility_credits"), 0) + 1
        new_energy = me_energy - energy_cost

        supabase.table("users").update({
            "ap": new_ap,
            "visibility_credits": new_vis,
            "energy": new_energy,
            "last_active": "now()"
        }).eq("user_id", user_id).execute()

        targ_vis = _clamp_nonneg(_safe_int(target.get("visibility_credits"), 0) - 1)
        targ_light = _safe_int(target.get("votes_light"), 0)
        targ_spite = _safe_int(target.get("votes_spite"), 0)
        targ_unassigned = _safe_int(target.get("unassigned_stat"), 0) + 1

        if direction == "LIGHT":
            targ_light += 1
        else:
            targ_spite += 1

        supabase.table("users").update({
            "visibility_credits": targ_vis,
            "votes_light": targ_light,
            "votes_spite": targ_spite,
            "unassigned_stat": targ_unassigned
        }).eq("user_id", target_id).execute()

        return {
            "status": "ok",
            "direction": direction,
            "energy_cost": energy_cost,
            "swipes_today": count_today + 1,
            "new_ap": new_ap,
            "new_energy": new_energy,
            "new_visibility_credits": new_vis
        }

    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/game/feed_debug")
async def game_feed_debug(req: dict):
    """
    Returns counts showing why feed might be empty.
    """
    try:
        _require_supabase()
        init_data = req.get("initData") or ""
        u_data = validate_auth(init_data)
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"), referred_by=None)

        now = _utc_now()
        cutoff_5m = (now - timedelta(minutes=5)).isoformat()
        cutoff_1h = (now - timedelta(hours=1)).isoformat()

        def count_where(cutoff_iso: str) -> Dict[str, int]:
            # counts among VERIFIED+PASS
            base = (
                supabase.table("users")
                .select("user_id,visibility_credits,last_active", count="exact")
                .eq("verification_status", "VERIFIED")
                .eq("biolock_status", "PASS")
                .not_.is_("bio_lock_url", "null")
                .neq("user_id", user_id)
                .execute()
            )
            rows = base.data or []
            total = len(rows)

            active = [r for r in rows if r.get("last_active") and str(r["last_active"]) >= cutoff_iso]
            active_n = len(active)

            vis = [r for r in active if _safe_int(r.get("visibility_credits"), 0) > 0]
            vis_n = len(vis)

            swiped_ids = set(_get_swiped_target_ids_today(user_id))
            after_swipe_filter = [r for r in vis if int(r["user_id"]) not in swiped_ids]
            after_swipe_n = len(after_swipe_filter)

            return {
                "total_verified_pass": total,
                "active_cutoff": active_n,
                "active_and_vis_gt0": vis_n,
                "after_swiped_today_filter": after_swipe_n
            }

        return {
            "status": "ok",
            "cutoff_5m": count_where(cutoff_5m),
            "cutoff_1h": count_where(cutoff_1h),
            "your_swipes_today": _get_swipes_today_count(user_id)
        }

    except Exception as e:
        return {"status": "error", "msg": str(e)}
