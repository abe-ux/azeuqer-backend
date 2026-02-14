# AZEUQER TITANIUM 13.x - MODULES: IDENTITY + PROFILE + INVENTORY + COMBAT
import os, json, time, urllib.parse, random
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client

# --- INIT ---
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
    allow_origins=["*"],  # for production you might want to restrict this
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# ---------- UTILS ----------
def validate_auth(init_data: str) -> Dict[str, Any]:
    """
    DEV MODE ONLY:
    - In production, you should validate Telegram initData signature.
    - For now, this parses Telegram's `user` JSON if present, else debug user.
    """
    if init_data == "debug_mode":
        return {"id": 12345, "username": "Architect"}

    try:
        parsed = dict(x.split("=", 1) for x in init_data.split("&"))
        user_json = urllib.parse.unquote(parsed.get("user", "{}"))
        u = json.loads(user_json) if user_json else {}
        # Telegram uses `id` and `username` / `first_name`
        uid = u.get("id") or 12345
        uname = u.get("username") or u.get("first_name") or "Citizen"
        return {"id": uid, "username": uname}
    except Exception:
        return {"id": 12345, "username": "Debug_User"}


def _require_supabase():
    if supabase is None:
        raise RuntimeError("SUPABASE_NOT_CONFIGURED")


def _now() -> int:
    return int(time.time())


def _loot_table() -> List[str]:
    # Keep item ids simple strings for your UI
    return [
        "NEON_RING", "GLITCH_MASK", "VOID_CAPE",
        "BLOOD_VIAL", "SPARK_CORE", "ECHO_CHIP",
        "SIGIL_BLADE", "PHANTOM_LENS", "RUNE_BATTERY"
    ]


def _grant_loot(uid: int, item_id: str, qty: int = 1) -> None:
    """
    Upsert into inventory: (user_id, item_id) with qty accumulation.
    Requires unique constraint on (user_id, item_id) OR manual merge.
    We'll do manual merge for compatibility.
    """
    _require_supabase()

    # read existing qty
    existing = supabase.table("inventory").select("qty").eq("user_id", uid).eq("item_id", item_id).execute()
    if existing.data and len(existing.data) > 0:
        new_qty = int(existing.data[0].get("qty") or 0) + qty
        supabase.table("inventory").update({"qty": new_qty}).eq("user_id", uid).eq("item_id", item_id).execute()
    else:
        supabase.table("inventory").insert({"user_id": uid, "item_id": item_id, "qty": qty}).execute()


def _get_user(uid: int) -> Optional[Dict[str, Any]]:
    _require_supabase()
    res = supabase.table("users").select("*").eq("user_id", uid).execute()
    if res.data:
        return res.data[0]
    return None


def _ensure_user(uid: int, username: str) -> Dict[str, Any]:
    _require_supabase()
    u = _get_user(uid)
    if u:
        # optionally update username if changed
        if username and u.get("username") != username:
            supabase.table("users").update({"username": username}).eq("user_id", uid).execute()
            u["username"] = username
        return u

    new_user = {
        "user_id": uid,
        "username": username or "Citizen",
        "ap": 0,
        "faction": "UNSORTED",
        "equipped_item": None,
        "bio_lock_url": None
    }
    supabase.table("users").insert(new_user).execute()
    return new_user


# ---------- HEALTH ----------
@app.get("/")
def health_check():
    return {"status": "TITANIUM ONLINE", "ts": _now()}


# ---------- AUTH ----------
@app.post("/auth/login")
async def login(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData"))
        uid = int(u_data["id"])
        username = u_data.get("username", "Citizen")

        u = _ensure_user(uid, username)
        return {"status": "ok", "user": u}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    try:
        _require_supabase()
        u_data = validate_auth(initData)
        uid = int(u_data["id"])

        content = await file.read()
        filename = f"{uid}_{_now()}.jpg"

        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            return {"status": "error", "msg": "BUCKET_FAIL"}

        supabase.table("users").update({"bio_lock_url": url}).eq("user_id", uid).execute()
        return {"status": "success", "url": url}

    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        return {"status": "error", "msg": str(e)}


@app.post("/auth/reset")
async def reset_user(req: dict):
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData"))
        uid = int(u_data["id"])

        supabase.table("users").update({"bio_lock_url": None, "equipped_item": None}).eq("user_id", uid).execute()
        # optionally wipe inventory too:
        # supabase.table("inventory").delete().eq("user_id", uid).execute()
        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ---------- GAME: FEED ----------
@app.post("/game/feed")
async def game_feed(req: dict):
    """
    Returns other users with bio_lock_url for swipe feed.
    """
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData"))
        uid = int(u_data["id"])
        _ensure_user(uid, u_data.get("username", "Citizen"))

        # get users with bio_lock_url not null and not self
        res = (
            supabase.table("users")
            .select("user_id,username,bio_lock_url,faction,equipped_item")
            .neq("user_id", uid)
            .not_.is_("bio_lock_url", "null")
            .limit(25)
            .execute()
        )

        return {"status": "ok", "feed": res.data or []}
    except Exception as e:
        return {"status": "error", "msg": str(e), "feed": []}


# ---------- GAME: SWIPE ----------
@app.post("/game/swipe")
async def game_swipe(req: dict):
    """
    Records swipe, increments AP, and sometimes drops loot.
    Expects: target_id, direction in {"LIGHT","SPITE"}.
    Returns: new_ap, optional loot_drop.
    """
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData"))
        uid = int(u_data["id"])
        user = _ensure_user(uid, u_data.get("username", "Citizen"))

        target_id = int(req.get("target_id") or 0)
        direction = (req.get("direction") or "").upper().strip()
        if direction not in ("LIGHT", "SPITE"):
            return {"status": "error", "msg": "BAD_DIRECTION"}

        # record swipe (optional table)
        try:
            supabase.table("swipes").insert({
                "user_id": uid,
                "target_id": target_id,
                "direction": direction,
                "ts": _now()
            }).execute()
        except Exception:
            # If swipes table doesn't exist yet, ignore (frontend still works)
            pass

        # AP gain
        new_ap = int(user.get("ap") or 0) + 1
        supabase.table("users").update({"ap": new_ap}).eq("user_id", uid).execute()

        # loot drop chance: 15% on LIGHT, 5% on SPITE
        loot_drop = None
        roll = random.random()
        threshold = 0.15 if direction == "LIGHT" else 0.05
        if roll < threshold:
            loot_drop = random.choice(_loot_table())
            _grant_loot(uid, loot_drop, qty=1)

        return {"status": "ok", "new_ap": new_ap, "loot_drop": loot_drop}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ---------- GAME: INVENTORY ----------
@app.post("/game/inventory")
async def game_inventory(req: dict):
    """
    Returns items for the requesting user.
    """
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData"))
        uid = int(u_data["id"])
        _ensure_user(uid, u_data.get("username", "Citizen"))

        res = supabase.table("inventory").select("item_id,qty").eq("user_id", uid).execute()
        items = res.data or []
        # normalize
        for it in items:
            it["qty"] = int(it.get("qty") or 0)
        return {"status": "ok", "items": items}
    except Exception as e:
        return {"status": "error", "msg": str(e), "items": []}


# ---------- GAME: EQUIP ----------
@app.post("/game/equip")
async def game_equip(req: dict):
    """
    Equips an item by setting users.equipped_item.
    Expects: item_id
    """
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData"))
        uid = int(u_data["id"])
        _ensure_user(uid, u_data.get("username", "Citizen"))

        item_id = (req.get("item_id") or "").strip()
        if not item_id:
            return {"status": "error", "msg": "NO_ITEM_ID"}

        # verify user owns it
        inv = supabase.table("inventory").select("qty").eq("user_id", uid).eq("item_id", item_id).execute()
        if not inv.data or int(inv.data[0].get("qty") or 0) <= 0:
            return {"status": "error", "msg": "NOT_OWNED"}

        supabase.table("users").update({"equipped_item": item_id}).eq("user_id", uid).execute()
        return {"status": "ok", "equipped_item": item_id}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ---------- GAME: COMBAT ----------
@app.post("/game/combat/info")
async def combat_info(req: dict):
    """
    Returns a boss "instance" (simple). Frontend keeps boss_hp_current, and
    sends it back to /turn.

    You can later expand to persistent combat sessions, boss types, etc.
    """
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData"))
        uid = int(u_data["id"])
        user = _ensure_user(uid, u_data.get("username", "Citizen"))

        ap = int(user.get("ap") or 0)
        # scale HP with AP, but keep it playable
        base = 25 + min(ap // 5, 25)  # up to +25
        boss = {
            "name": "AMBUSH DEMON",
            "hp": base,
            "max_hp": base,
            "lvl": 1 + min(ap // 10, 20)
        }
        return {"status": "ok", "boss": boss}
    except Exception as e:
        return {"status": "error", "msg": str(e), "boss": {"name":"AMBUSH DEMON","hp":25,"max_hp":25,"lvl":1}}


@app.post("/game/combat/turn")
async def combat_turn(req: dict):
    """
    Expects:
      - action: "ATTACK" or "POTION_HP"
      - boss_hp_current: number
    Returns:
      - new_boss_hp
      - status: "ONGOING" or "VICTORY"
      - loot (on victory)
    """
    try:
        _require_supabase()
        u_data = validate_auth(req.get("initData"))
        uid = int(u_data["id"])
        user = _ensure_user(uid, u_data.get("username", "Citizen"))

        action = (req.get("action") or "").upper().strip()
        boss_hp_current = req.get("boss_hp_current")
        if boss_hp_current is None:
            return {"status": "error", "msg": "MISSING_BOSS_HP"}

        try:
            boss_hp = int(boss_hp_current)
        except Exception:
            return {"status": "error", "msg": "BAD_BOSS_HP"}

        boss_hp = max(0, boss_hp)

        if action == "ATTACK":
            # damage scales a bit with AP
            ap = int(user.get("ap") or 0)
            dmg = 3 + random.randint(0, 4) + min(ap // 20, 5)  # small scaling
            new_hp = max(0, boss_hp - dmg)

            if new_hp == 0:
                loot = random.choice(_loot_table())
                _grant_loot(uid, loot, qty=1)

                # reward AP too
                new_ap = int(user.get("ap") or 0) + 2
                supabase.table("users").update({"ap": new_ap}).eq("user_id", uid).execute()

                return {"status": "VICTORY", "new_boss_hp": 0, "loot": loot, "new_ap": new_ap}

            return {"status": "ONGOING", "new_boss_hp": new_hp}

        elif action == "POTION_HP":
            # simple heal effect: costs 1 AP if available, no boss change
            ap = int(user.get("ap") or 0)
            if ap <= 0:
                return {"status": "ONGOING", "new_boss_hp": boss_hp, "msg": "NO_AP"}
            new_ap = ap - 1
            supabase.table("users").update({"ap": new_ap}).eq("user_id", uid).execute()
            return {"status": "ONGOING", "new_boss_hp": boss_hp, "new_ap": new_ap}

        else:
            return {"status": "error", "msg": "BAD_ACTION"}

    except Exception as e:
        return {"status": "error", "msg": str(e)}
