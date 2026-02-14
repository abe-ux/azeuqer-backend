# AZEUQER TITANIUM 13.2 - FIXED FOR SUPABASE users.user_id NOT NULL
# Copy-paste this whole file over your main.py

import os, json, time, urllib.parse, random
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
    allow_origins=["*"],  # OK for prototype. Lock down later.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# --------------------
# UTILS
# --------------------
def _require_supabase():
    if supabase is None:
        raise RuntimeError("SUPABASE_NOT_CONFIGURED")

def _now() -> int:
    return int(time.time())

def _safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def validate_auth(init_data: str) -> Dict[str, Any]:
    """
    DEV MODE ONLY: This does NOT verify Telegram initData signatures.
    Production should verify initData hash.
    """
    if init_data == "debug_mode":
        return {"id": 12345, "username": "Architect"}

    try:
        parsed = dict(x.split("=", 1) for x in init_data.split("&"))
        user_json = urllib.parse.unquote(parsed.get("user", "{}"))
        u = json.loads(user_json) if user_json else {}
        uid = u.get("id") or 12345
        uname = u.get("username") or u.get("first_name") or "Citizen"
        return {"id": int(uid), "username": uname}
    except Exception:
        return {"id": 12345, "username": "Debug_User"}

def _loot_table():
    return [
        "NEON_RING", "GLITCH_MASK", "VOID_CAPE",
        "BLOOD_VIAL", "SPARK_CORE", "ECHO_CHIP",
        "SIGIL_BLADE", "PHANTOM_LENS", "RUNE_BATTERY"
    ]

# ---- USERS: IMPORTANT ----
# Your Supabase requires users.user_id NOT NULL.
# We'll use user_id as the canonical Telegram ID and ALSO fill tg_id for compatibility.

def _get_user_by_user_id(user_id: int) -> Optional[Dict[str, Any]]:
    _require_supabase()
    res = supabase.table("users").select("*").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None

def _ensure_user(user_id: int, username: str) -> Dict[str, Any]:
    _require_supabase()
    u = _get_user_by_user_id(user_id)

    if u:
        # keep username fresh
        if username and u.get("username") != username:
            supabase.table("users").update({"username": username}).eq("user_id", user_id).execute()
            u["username"] = username

        # fill defaults if missing
        patch = {}
        if u.get("ap") is None:
            patch["ap"] = 0
        if u.get("faction") is None:
            patch["faction"] = "UNSORTED"
        # ensure tg_id exists too (optional column)
        if u.get("tg_id") is None:
            patch["tg_id"] = user_id

        if patch:
            supabase.table("users").update(patch).eq("user_id", user_id).execute()
            u.update(patch)

        return u

    # Create new user with REQUIRED user_id and optional tg_id
    new_user = {
        "user_id": user_id,                 # ✅ REQUIRED (NOT NULL)
        "tg_id": user_id,                   # ✅ helpful compatibility if column exists
        "username": username or "Citizen",
        "ap": 0,
        "faction": "UNSORTED",
        "equipped_item": None,
        "bio_lock_url": None
    }

    supabase.table("users").insert(new_user).execute()
    return new_user

def _grant_loot(user_id: int, item_id: str, qty: int = 1) -> None:
    """
    inventory table expected (based on your earlier setup):
      tg_id bigint, item_id text, qty int, unique(tg_id,item_id)
    We will use tg_id = user_id when writing inventory.
    """
    _require_supabase()
    tg_id = user_id

    ex = supabase.table("inventory").select("qty").eq("tg_id", tg_id).eq("item_id", item_id).limit(1).execute()
    if ex.data:
        new_qty = _safe_int(ex.data[0].get("qty"), 0) + qty
        supabase.table("inventory").update({"qty": new_qty}).eq("tg_id", tg_id).eq("item_id", item_id).execute()
    else:
        supabase.table("inventory").insert({"tg_id": tg_id, "item_id": item_id, "qty": qty}).execute()

# --------------------
# HEALTH
# --------------------
@app.get("/")
def health_check():
    return {"status": "TITANIUM 13.2 ONLINE", "ts": _now()}

# --------------------
# AUTH
# --------------------
@app.post("/auth/login")
async def login(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])
        username = u_data.get("username", "Citizen")

        u = _ensure_user(user_id, username)

        # ✅ FRONTEND COMPAT ALIASES
        u["user_id"] = user_id
        u["tg_id"] = u.get("tg_id") or user_id

        return {"status": "ok", "user": u}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    try:
        _require_supabase()
        u_data = validate_auth(initData)
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"))

        content = await file.read()
        filename = f"{user_id}_{_now()}.jpg"

        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            return {"status": "error", "msg": "BUCKET_FAIL"}

        supabase.table("users").update({"bio_lock_url": url}).eq("user_id", user_id).execute()
        return {"status": "success", "url": url}

    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        return {"status": "error", "msg": str(e)}

@app.post("/auth/reset")
async def reset_user(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])

        supabase.table("users").update({"bio_lock_url": None, "equipped_item": None}).eq("user_id", user_id).execute()

        # optional: wipe inventory too
        # supabase.table("inventory").delete().eq("tg_id", user_id).execute()

        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

# --------------------
# GAME: FEED
# --------------------
@app.post("/game/feed")
async def game_feed(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"))

        res = (
            supabase.table("users")
            .select("user_id,tg_id,username,bio_lock_url,faction,equipped_item")
            .neq("user_id", user_id)
            .not_.is_("bio_lock_url", "null")
            .limit(25)
            .execute()
        )

        feed = []
        for row in (res.data or []):
            # Always provide user_id for frontend
            uid = row.get("user_id") or row.get("tg_id")
            feed.append({
                "user_id": uid,
                "tg_id": row.get("tg_id") or uid,
                "username": row.get("username"),
                "bio_lock_url": row.get("bio_lock_url"),
                "faction": row.get("faction"),
                "equipped_item": row.get("equipped_item"),
            })

        return {"status": "ok", "feed": feed}
    except Exception as e:
        return {"status": "error", "msg": str(e), "feed": []}

# --------------------
# GAME: SWIPE
# --------------------
@app.post("/game/swipe")
async def game_swipe(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])
        user = _ensure_user(user_id, u_data.get("username", "Citizen"))

        target_id = _safe_int(req.get("target_id"), 0)
        direction = (req.get("direction") or "").upper().strip()
        if direction not in ("LIGHT", "SPITE"):
            return {"status": "error", "msg": "BAD_DIRECTION"}

        # swipes table expected: tg_id, target_tg_id, direction, ts
        try:
            supabase.table("swipes").insert({
                "tg_id": user_id,
                "target_tg_id": target_id,
                "direction": direction,
                "ts": _now()
            }).execute()
        except Exception:
            pass

        new_ap = _safe_int(user.get("ap"), 0) + 1
        supabase.table("users").update({"ap": new_ap}).eq("user_id", user_id).execute()

        loot_drop = None
        roll = random.random()
        threshold = 0.15 if direction == "LIGHT" else 0.05
        if roll < threshold:
            loot_drop = random.choice(_loot_table())
            try:
                _grant_loot(user_id, loot_drop, qty=1)
            except Exception:
                loot_drop = None

        return {"status": "ok", "new_ap": new_ap, "loot_drop": loot_drop}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

# --------------------
# GAME: INVENTORY
# --------------------
@app.post("/game/inventory")
async def game_inventory(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"))

        res = supabase.table("inventory").select("item_id,qty").eq("tg_id", user_id).execute()
        items = res.data or []
        for it in items:
            it["qty"] = _safe_int(it.get("qty"), 0)
        return {"status": "ok", "items": items}
    except Exception as e:
        return {"status": "error", "msg": str(e), "items": []}

# --------------------
# GAME: EQUIP
# --------------------
@app.post("/game/equip")
async def game_equip(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])
        _ensure_user(user_id, u_data.get("username", "Citizen"))

        item_id = (req.get("item_id") or "").strip()
        if not item_id:
            return {"status": "error", "msg": "NO_ITEM_ID"}

        inv = (
            supabase.table("inventory")
            .select("qty")
            .eq("tg_id", user_id)
            .eq("item_id", item_id)
            .limit(1)
            .execute()
        )
        if not inv.data or _safe_int(inv.data[0].get("qty"), 0) <= 0:
            return {"status": "error", "msg": "NOT_OWNED"}

        supabase.table("users").update({"equipped_item": item_id}).eq("user_id", user_id).execute()
        return {"status": "ok", "equipped_item": item_id}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

# --------------------
# GAME: COMBAT
# --------------------
@app.post("/game/combat/info")
async def combat_info(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])
        user = _ensure_user(user_id, u_data.get("username", "Citizen"))

        ap = _safe_int(user.get("ap"), 0)
        base_hp = 25 + min(ap // 5, 25)
        boss = {"name": "AMBUSH DEMON", "hp": base_hp, "max_hp": base_hp, "lvl": 1 + min(ap // 10, 20)}
        return {"status": "ok", "boss": boss}
    except Exception as e:
        return {"status": "error", "msg": str(e), "boss": {"name": "AMBUSH DEMON", "hp": 25, "max_hp": 25, "lvl": 1}}

@app.post("/game/combat/turn")
async def combat_turn(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData") or "")
        user_id = int(u_data["id"])
        user = _ensure_user(user_id, u_data.get("username", "Citizen"))

        action = (req.get("action") or "").upper().strip()
        boss_hp = _safe_int(req.get("boss_hp_current"), -1)
        if boss_hp < 0:
            return {"status": "error", "msg": "BAD_BOSS_HP"}
        boss_hp = max(0, boss_hp)

        if action == "ATTACK":
            ap = _safe_int(user.get("ap"), 0)
            dmg = 3 + random.randint(0, 4) + min(ap // 20, 5)
            new_hp = max(0, boss_hp - dmg)

            if new_hp == 0:
                loot = random.choice(_loot_table())
                try:
                    _grant_loot(user_id, loot, qty=1)
                except Exception:
                    loot = None

                new_ap = ap + 2
                supabase.table("users").update({"ap": new_ap}).eq("user_id", user_id).execute()

                return {"status": "VICTORY", "new_boss_hp": 0, "loot": loot, "new_ap": new_ap}

            return {"status": "ONGOING", "new_boss_hp": new_hp}

        if action == "POTION_HP":
            ap = _safe_int(user.get("ap"), 0)
            if ap <= 0:
                return {"status": "ONGOING", "new_boss_hp": boss_hp, "msg": "NO_AP"}

            new_ap = ap - 1
            supabase.table("users").update({"ap": new_ap}).eq("user_id", user_id).execute()
            return {"status": "ONGOING", "new_boss_hp": boss_hp, "new_ap": new_ap}

        return {"status": "error", "msg": "BAD_ACTION"}

    except Exception as e:
        return {"status": "error", "msg": str(e)}
