# AZEUQER TITANIUM — BACKEND (TOP + PROFILE + VESSEL + FAKE FEED + DETERMINISTIC AMBUSH)
# Copy-paste as main.py

import os, json, time, urllib.parse, random
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from supabase import create_client

app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    print("WARNING: SUPABASE keys missing. Running in DEV memory mode.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UTC = timezone.utc

# =========================
# GAME RULES
# =========================
FOUNDERS_LIMIT = 23  # first 23 real joiners auto-accepted (Founders)
ENERGY_MAX = 30
ENERGY_REGEN_SECONDS = 180  # 1 energy / 3 minutes (lazy)
FREE_SWIPES_PER_DAY = 20
ENERGY_COST_AFTER_FREE = 1
AMBUSH_EVERY_N_SWIPES = 10

FEED_ACTIVE_WINDOW_SECONDS = 300
FEED_FALLBACK_WINDOW_SECONDS = 3600
FEED_LIMIT = 20

FAKE_TARGET_COUNT = 25
FAKE_USER_ID_BASE = 900_000
FAKE_BOSS_COUNT = 12

# =========================
# DEV MEMORY MODE STORAGE
# =========================
DEV_USERS: Dict[int, Dict[str, Any]] = {}
DEV_SWIPES: List[Dict[str, Any]] = []
DEV_INVENTORY: Dict[int, List[Dict[str, Any]]] = {}

# =========================
# HELPERS
# =========================
def now_utc() -> datetime:
    return datetime.now(tz=UTC)

def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()

def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def day_key_utc(dt: Optional[datetime] = None) -> str:
    dt = dt or now_utc()
    return dt.strftime("%Y-%m-%d")

def safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default

def validate_auth(init_data: str) -> Dict[str, Any]:
    if init_data == "debug_mode":
        return {"id": 12345, "username": "Architect"}
    try:
        parsed = dict(x.split("=", 1) for x in init_data.split("&"))
        user_json = urllib.parse.unquote(parsed.get("user", "{}"))
        return json.loads(user_json)
    except Exception:
        return {"id": 12345, "username": "Debug_User"}

def normalize_user_state(user: Dict[str, Any]) -> Dict[str, Any]:
    user.setdefault("user_id", user.get("tg_id") or user.get("id") or 0)
    user.setdefault("username", "Citizen")
    user.setdefault("email", None)

    user.setdefault("role", "CITIZEN")
    user.setdefault("is_founder", False)

    user.setdefault("verification_status", "VERIFIED")
    user.setdefault("biolock_status", "GATE")
    user.setdefault("bio_lock_url", None)

    # Vessel / Archetype
    user.setdefault("vessel_work", 0)
    user.setdefault("vessel_pace", 0)
    user.setdefault("vessel_mind", 0)
    user.setdefault("vessel_vibe", 0)
    user.setdefault("body_asset_id", "body_dev")

    user.setdefault("faction", "UNSORTED")
    user.setdefault("faction_assigned", False)
    user.setdefault("init_light", 0)
    user.setdefault("init_spite", 0)

    user.setdefault("votes_light", 0)
    user.setdefault("votes_spite", 0)

    user.setdefault("ap", 0)
    user.setdefault("visibility_credits", 10)

    user.setdefault("energy", ENERGY_MAX)
    user.setdefault("energy_updated_at", iso(now_utc()))

    user.setdefault("swipes_day_key", day_key_utc())
    user.setdefault("swipes_today", 0)

    user.setdefault("last_active", iso(now_utc()))
    return user

def apply_daily_reset(user: Dict[str, Any]) -> Dict[str, Any]:
    today = day_key_utc()
    if (user.get("swipes_day_key") or today) != today:
        user["swipes_day_key"] = today
        user["swipes_today"] = 0
        user["init_light"] = 0
        user["init_spite"] = 0
    return user

def apply_energy_regen(user: Dict[str, Any]) -> Dict[str, Any]:
    energy = safe_int(user.get("energy"), ENERGY_MAX)
    updated_at = parse_iso(user.get("energy_updated_at")) or now_utc()

    if energy >= ENERGY_MAX:
        user["energy"] = ENERGY_MAX
        user["energy_updated_at"] = iso(updated_at)
        return user

    elapsed = (now_utc() - updated_at).total_seconds()
    if elapsed <= 0:
        return user

    gained = int(elapsed // ENERGY_REGEN_SECONDS)
    if gained <= 0:
        return user

    new_energy = min(ENERGY_MAX, energy + gained)
    new_updated_at = updated_at + timedelta(seconds=gained * ENERGY_REGEN_SECONDS)

    user["energy"] = new_energy
    user["energy_updated_at"] = iso(new_updated_at)
    return user

def is_fake_user_id(uid: int) -> bool:
    return uid >= FAKE_USER_ID_BASE and uid < FAKE_USER_ID_BASE + 100_000

def assign_founder_flags_for_new_user(new_user: Dict[str, Any]) -> Dict[str, Any]:
    new_user["is_founder"] = True
    new_user["role"] = "FOUNDER"
    new_user["verification_status"] = "VERIFIED"

    # founders bypass gate
    new_user["biolock_status"] = "PASS"
    if not new_user.get("bio_lock_url"):
        seed = (safe_int(new_user.get("user_id")) % FAKE_TARGET_COUNT) + 1
        new_user["bio_lock_url"] = f"/dev/fake_image/{seed}.svg"
    return new_user

def supabase_count_users() -> Optional[int]:
    try:
        res = supabase.table("users").select("user_id", count="exact").limit(1).execute()
        c = getattr(res, "count", None)
        if c is not None:
            return int(c)
    except Exception:
        pass
    try:
        res2 = supabase.table("users").select("user_id").limit(1000).execute()
        if res2.data is not None:
            return len(res2.data)
    except Exception:
        return None
    return None

# =========================
# SVG GENERATORS (FAKE IMAGES + BOSSES)
# =========================
def make_svg(seed: int, label: str) -> str:
    rnd = random.Random(seed)
    bg1 = f"#{rnd.randrange(0x111111, 0xFFFFFF):06x}"
    bg2 = f"#{rnd.randrange(0x111111, 0xFFFFFF):06x}"
    neon = f"#{rnd.randrange(0x00FFFF, 0xFFFFFF):06x}"
    eye = f"#{rnd.randrange(0x000000, 0x222222):06x}"
    name = f"{label}-{seed:02d}"
    glyph = rnd.choice(["∆","Ø","Ψ","Λ","Σ","⊕","⋈","⟁","⟡","⟢","⟣","⟐","⨳","⧫"])

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1200">
<defs>
  <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="{bg1}"/>
    <stop offset="100%" stop-color="{bg2}"/>
  </linearGradient>
</defs>
<rect width="100%" height="100%" fill="url(#g)"/>
<rect x="40" y="40" width="820" height="1120" rx="40" fill="rgba(0,0,0,0.25)" stroke="{neon}" stroke-opacity="0.35" stroke-width="6"/>
<circle cx="450" cy="520" r="240" fill="rgba(0,0,0,0.28)" stroke="{neon}" stroke-opacity="0.55" stroke-width="6"/>
<circle cx="360" cy="470" r="28" fill="{eye}"/><circle cx="540" cy="470" r="28" fill="{eye}"/>
<rect x="420" y="530" width="60" height="16" rx="8" fill="{eye}" opacity="0.6"/>
<path d="M340 600 C 390 680, 510 680, 560 600" stroke="{neon}" stroke-opacity="0.55" stroke-width="10" fill="none" stroke-linecap="round"/>
<text x="70" y="110" fill="{neon}" font-size="44" font-family="monospace" opacity="0.92">{name}</text>
<text x="70" y="1090" fill="{neon}" font-size="120" font-family="monospace" opacity="0.22">{glyph}</text>
</svg>"""

@app.get("/dev/fake_image/{seed}.svg")
def dev_fake(seed: int):
    return Response(content=make_svg(seed, "CITIZEN"), media_type="image/svg+xml")

@app.get("/dev/boss/{boss_id}.svg")
def dev_boss(boss_id: int):
    seed = 1000 + (boss_id % 10_000)
    return Response(content=make_svg(seed, "BOSS"), media_type="image/svg+xml")

# =========================
# HEALTH
# =========================
@app.get("/")
def health():
    return {"status": "AZEUQER BACKEND ONLINE", "ts": iso(now_utc())}

# =========================
# PROFILE / VESSEL (fixes missing /profile/vessel)
# =========================
@app.get("/profile/vessel")
def vessel_manifest():
    # This is a manifest the frontend can call without needing DB.
    # Later, you can replace this with real asset IDs.
    return {
        "status": "ok",
        "sliders": [
            {"key": "work", "label0": "LAZY", "label1": "DILIGENT"},
            {"key": "pace", "label0": "SLOW", "label1": "FAST"},
            {"key": "mind", "label0": "SMART", "label1": "TOUGH"},
            {"key": "vibe", "label0": "NERD", "label1": "COOL"},
        ],
        "note": "Frontend uses these to generate body_asset_id (cosmetic).",
    }

@app.post("/auth/profile")
async def update_profile(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    nickname = (req.get("username") or "").strip()
    email = (req.get("email") or "").strip().lower()

    vessel_work = safe_int(req.get("vessel_work"), 0)
    vessel_pace = safe_int(req.get("vessel_pace"), 0)
    vessel_mind = safe_int(req.get("vessel_mind"), 0)
    vessel_vibe = safe_int(req.get("vessel_vibe"), 0)

    # clamp 0/1
    vessel_work = 1 if vessel_work else 0
    vessel_pace = 1 if vessel_pace else 0
    vessel_mind = 1 if vessel_mind else 0
    vessel_vibe = 1 if vessel_vibe else 0

    if nickname and (len(nickname) < 2 or len(nickname) > 20):
        return {"status": "error", "msg": "BAD_USERNAME_LEN"}

    if email:
        if ("@" not in email) or ("." not in email) or len(email) < 6 or len(email) > 80:
            return {"status": "error", "msg": "INVALID_EMAIL"}

    body_asset_id = f"body_{vessel_work}{vessel_pace}{vessel_mind}{vessel_vibe}"

    if not supabase:
        me = DEV_USERS.get(uid) or normalize_user_state({"user_id": uid, "username": u.get("username","Citizen")})
        if nickname:
            me["username"] = nickname
        if email:
            me["email"] = email
        me["vessel_work"] = vessel_work
        me["vessel_pace"] = vessel_pace
        me["vessel_mind"] = vessel_mind
        me["vessel_vibe"] = vessel_vibe
        me["body_asset_id"] = body_asset_id
        me["last_active"] = iso(now_utc())
        DEV_USERS[uid] = me
        return {"status": "ok", "user": me, "mode": "DEV_MEMORY"}

    try:
        res = supabase.table("users").select("*").eq("user_id", uid).limit(1).execute()
        if not res.data:
            return {"status": "error", "msg": "NO_USER"}

        updates = {
            "vessel_work": vessel_work,
            "vessel_pace": vessel_pace,
            "vessel_mind": vessel_mind,
            "vessel_vibe": vessel_vibe,
            "body_asset_id": body_asset_id,
            "last_active": iso(now_utc()),
        }
        if nickname:
            updates["username"] = nickname
        if email:
            updates["email"] = email

        supabase.table("users").update(updates).eq("user_id", uid).execute()
        me2 = supabase.table("users").select("*").eq("user_id", uid).limit(1).execute().data[0]
        return {"status": "ok", "user": normalize_user_state(me2)}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

# =========================
# AUTH
# =========================
@app.post("/auth/login")
async def login(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))
    username = u.get("username", "Citizen")

    if not supabase:
        if uid not in DEV_USERS:
            me = normalize_user_state({"user_id": uid, "username": username})
            if len(DEV_USERS) < FOUNDERS_LIMIT:
                me = assign_founder_flags_for_new_user(me)
            DEV_USERS[uid] = me

        me = normalize_user_state(DEV_USERS[uid])
        me = apply_energy_regen(apply_daily_reset(me))
        me["last_active"] = iso(now_utc())
        DEV_USERS[uid] = me
        return {"status": "ok", "user": me, "mode": "DEV_MEMORY"}

    try:
        res = supabase.table("users").select("*").eq("user_id", uid).limit(1).execute()
        if res.data:
            me = normalize_user_state(res.data[0])
        else:
            me = normalize_user_state({"user_id": uid, "username": username})

            count = supabase_count_users()
            if count is None:
                count = 999999

            if count < FOUNDERS_LIMIT:
                me = assign_founder_flags_for_new_user(me)

            supabase.table("users").insert(me).execute()

        before = {
            "energy": safe_int(me.get("energy"), ENERGY_MAX),
            "energy_updated_at": me.get("energy_updated_at"),
            "swipes_day_key": me.get("swipes_day_key"),
            "swipes_today": safe_int(me.get("swipes_today"), 0),
        }

        me = apply_energy_regen(apply_daily_reset(me))
        me["last_active"] = iso(now_utc())

        updates = {"last_active": me["last_active"]}
        if me.get("energy") != before["energy"]:
            updates["energy"] = me["energy"]
        if me.get("energy_updated_at") != before["energy_updated_at"]:
            updates["energy_updated_at"] = me["energy_updated_at"]
        if me.get("swipes_day_key") != before["swipes_day_key"]:
            updates["swipes_day_key"] = me["swipes_day_key"]
        if safe_int(me.get("swipes_today"), 0) != before["swipes_today"]:
            updates["swipes_today"] = me["swipes_today"]

        supabase.table("users").update(updates).eq("user_id", uid).execute()
        return {"status": "ok", "user": me}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    u_data = validate_auth(initData)
    uid = safe_int(u_data.get("id"))
    content = await file.read()

    if not supabase:
        me = DEV_USERS.get(uid) or normalize_user_state({"user_id": uid, "username": u_data.get("username", "Citizen")})
        me["biolock_status"] = "PASS"
        me["bio_lock_url"] = f"/dev/fake_image/{(uid % FAKE_TARGET_COUNT) + 1}.svg"
        me["last_active"] = iso(now_utc())
        DEV_USERS[uid] = me
        return {"status": "success", "url": me["bio_lock_url"], "mode": "DEV_MEMORY"}

    filename = f"{uid}_{int(time.time())}.jpg"
    try:
        supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
        url = supabase.storage.from_("bio-locks").get_public_url(filename)
        supabase.table("users").update({
            "bio_lock_url": url,
            "biolock_status": "PASS",
            "last_active": iso(now_utc()),
        }).eq("user_id", uid).execute()
        return {"status": "success", "url": url}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/reset")
async def reset_user(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    if not supabase:
        me = DEV_USERS.get(uid)
        if not me:
            return {"status": "RESET_COMPLETE", "mode": "DEV_MEMORY"}
        is_founder = bool(me.get("is_founder"))
        role = me.get("role", "CITIZEN")
        me2 = normalize_user_state({"user_id": uid, "username": me.get("username","Citizen")})
        if is_founder:
            me2 = assign_founder_flags_for_new_user(me2)
            me2["role"] = role or "FOUNDER"
        DEV_USERS[uid] = me2
        return {"status": "RESET_COMPLETE", "mode": "DEV_MEMORY"}

    try:
        existing = supabase.table("users").select("is_founder,role").eq("user_id", uid).limit(1).execute()
        is_founder = False
        role = "CITIZEN"
        if existing.data:
            is_founder = bool(existing.data[0].get("is_founder"))
            role = existing.data[0].get("role") or role

        updates = {
            "bio_lock_url": None,
            "biolock_status": "GATE",
            "swipes_today": 0,
            "swipes_day_key": day_key_utc(),
            "init_light": 0,
            "init_spite": 0,
            "faction": "UNSORTED",
            "faction_assigned": False,
            "last_active": iso(now_utc()),
        }

        if is_founder:
            updates["biolock_status"] = "PASS"
            updates["role"] = role or "FOUNDER"
            updates["bio_lock_url"] = f"/dev/fake_image/{(uid % FAKE_TARGET_COUNT) + 1}.svg"

        supabase.table("users").update(updates).eq("user_id", uid).execute()
        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

# =========================
# TOP IMAGES
# =========================
@app.post("/game/top_images")
async def top_images(req: dict):
    bosses = [{"boss_id": i, "name": f"BOSS_{i:02d}", "image_url": f"/dev/boss/{i}.svg"} for i in range(1, 7)]

    def fake_leaders():
        fake = []
        for i in range(1, FAKE_TARGET_COUNT + 1):
            fake.append({
                "user_id": FAKE_USER_ID_BASE + i,
                "username": f"Citizen_{i:02d}",
                "bio_lock_url": f"/dev/fake_image/{i}.svg",
                "votes_light": random.randint(0, 80),
                "votes_spite": random.randint(0, 80),
            })
        top_e = max(fake, key=lambda x: x["votes_light"])
        top_d = max(fake, key=lambda x: x["votes_spite"])
        return {"status": "ok", "top_euphoria": top_e, "top_dissonance": top_d, "bosses": bosses, "fake": True}

    if not supabase:
        return fake_leaders()

    try:
        e = (supabase.table("users")
             .select("user_id,username,bio_lock_url,votes_light,votes_spite")
             .eq("verification_status", "VERIFIED")
             .eq("biolock_status", "PASS")
             .order("votes_light", desc=True)
             .limit(1).execute())
        d = (supabase.table("users")
             .select("user_id,username,bio_lock_url,votes_light,votes_spite")
             .eq("verification_status", "VERIFIED")
             .eq("biolock_status", "PASS")
             .order("votes_spite", desc=True)
             .limit(1).execute())

        if not e.data or not d.data:
            return fake_leaders()

        top_e = e.data[0]
        top_d = d.data[0]
        if not top_e.get("bio_lock_url"):
            top_e["bio_lock_url"] = f"/dev/fake_image/{(safe_int(top_e.get('user_id')) % FAKE_TARGET_COUNT) + 1}.svg"
        if not top_d.get("bio_lock_url"):
            top_d["bio_lock_url"] = f"/dev/fake_image/{(safe_int(top_d.get('user_id')) % FAKE_TARGET_COUNT) + 1}.svg"

        return {"status": "ok", "top_euphoria": top_e, "top_dissonance": top_d, "bosses": bosses, "fake": False}
    except Exception:
        return fake_leaders()

# =========================
# FEED
# =========================
@app.post("/game/feed")
async def feed(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    def fake_feed():
        fake = []
        for i in range(1, FAKE_TARGET_COUNT + 1):
            fake.append({
                "user_id": FAKE_USER_ID_BASE + i,
                "username": f"Citizen_{i:02d}",
                "bio_lock_url": f"/dev/fake_image/{i}.svg",
                "body_asset_id": "body_dev",
            })
        random.shuffle(fake)
        return {"status": "ok", "feed": fake[:FEED_LIMIT], "fallback": True, "fake": True}

    if not supabase:
        return fake_feed()

    try:
        supabase.table("users").update({"last_active": iso(now_utc())}).eq("user_id", uid).execute()
    except Exception:
        pass

    try:
        cutoff_5m = iso(now_utc() - timedelta(seconds=FEED_ACTIVE_WINDOW_SECONDS))
        cutoff_1h = iso(now_utc() - timedelta(seconds=FEED_FALLBACK_WINDOW_SECONDS))

        def query(cutoff_iso: str):
            return (supabase.table("users")
                    .select("user_id,username,bio_lock_url,body_asset_id,votes_light,votes_spite,faction,visibility_credits,last_active")
                    .eq("verification_status", "VERIFIED")
                    .eq("biolock_status", "PASS")
                    .gt("last_active", cutoff_iso)
                    .gt("visibility_credits", 0)
                    .neq("user_id", uid)
                    .order("last_active", desc=True)
                    .limit(FEED_LIMIT)
                    .execute())

        r = query(cutoff_5m)
        rows = r.data or []
        fallback = False
        if len(rows) < 5:
            fallback = True
            r2 = query(cutoff_1h)
            rows = r2.data or []

        for row in rows:
            if not row.get("bio_lock_url"):
                row["bio_lock_url"] = f"/dev/fake_image/{(safe_int(row.get('user_id')) % FAKE_TARGET_COUNT) + 1}.svg"

        if rows:
            return {"status": "ok", "feed": rows[:FEED_LIMIT], "fallback": fallback, "fake": False}

        return fake_feed()
    except Exception:
        return fake_feed()

# =========================
# SWIPE (AP gain always; ambush deterministic on 10th swipe)
# =========================
@app.post("/game/swipe")
async def swipe(req: dict):
    init_data = req.get("initData") or "debug_mode"
    direction = (req.get("direction") or "").upper()
    target_id = safe_int(req.get("target_id"))

    if direction not in ("LIGHT", "SPITE"):
        return {"status": "error", "msg": "BAD_DIRECTION"}

    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    # DEV mode
    if not supabase:
        me = DEV_USERS.get(uid)
        if not me:
            me = normalize_user_state({"user_id": uid, "username": u.get("username","Citizen")})
            if len(DEV_USERS) < FOUNDERS_LIMIT:
                me = assign_founder_flags_for_new_user(me)
            DEV_USERS[uid] = me

        me = normalize_user_state(me)
        me = apply_energy_regen(apply_daily_reset(me))

        if not me.get("is_founder") and me.get("biolock_status") != "PASS":
            return {"status": "error", "msg": "BIOLOCK_REQUIRED"}

        swipes_today = safe_int(me.get("swipes_today"), 0)
        burn = ENERGY_COST_AFTER_FREE if swipes_today >= FREE_SWIPES_PER_DAY else 0
        energy = safe_int(me.get("energy"), ENERGY_MAX)
        if burn and energy <= 0:
            return {"status": "error", "msg": "NO_ENERGY"}

        swipes_today += 1
        energy = max(0, energy - burn)

        ap = safe_int(me.get("ap"), 0) + 1
        vis = safe_int(me.get("visibility_credits"), 0) + 1

        init_light = safe_int(me.get("init_light"), 0)
        init_spite = safe_int(me.get("init_spite"), 0)
        if not bool(me.get("faction_assigned")):
            if direction == "LIGHT":
                init_light += 1
            else:
                init_spite += 1

        updates = {
            "swipes_today": swipes_today,
            "energy": energy,
            "ap": ap,
            "visibility_credits": vis,
            "last_active": iso(now_utc()),
            "init_light": init_light,
            "init_spite": init_spite,
        }

        if (not bool(me.get("faction_assigned"))) and (init_light + init_spite >= 10):
            if init_light > init_spite:
                updates["faction"] = "EUPHORIA"
            elif init_spite > init_light:
                updates["faction"] = "DISSONANCE"
            else:
                updates["faction"] = random.choice(["EUPHORIA", "DISSONANCE"])
            updates["faction_assigned"] = True

        ambush = (swipes_today % AMBUSH_EVERY_N_SWIPES == 0)

        me.update(updates)
        DEV_USERS[uid] = me

        DEV_SWIPES.append({"swiper_id": uid, "target_id": target_id, "direction": direction, "created_at": iso(now_utc())})

        return {
            "status": "ok",
            "new_ap": ap,
            "new_energy": energy,
            "swipes_today": swipes_today,
            "ambush": ambush,
            "energy_max": ENERGY_MAX,
            "free_swipes_per_day": FREE_SWIPES_PER_DAY,
            "fake_target": is_fake_user_id(target_id),
        }

    # Supabase mode
    try:
        me_res = supabase.table("users").select("*").eq("user_id", uid).limit(1).execute()
        if not me_res.data:
            return {"status": "error", "msg": "NO_USER"}
        me = normalize_user_state(me_res.data[0])
        me = apply_energy_regen(apply_daily_reset(me))

        if (not bool(me.get("is_founder"))) and (me.get("biolock_status") != "PASS"):
            return {"status": "error", "msg": "BIOLOCK_REQUIRED"}

        swipes_today = safe_int(me.get("swipes_today"), 0)
        burn = ENERGY_COST_AFTER_FREE if swipes_today >= FREE_SWIPES_PER_DAY else 0
        energy = safe_int(me.get("energy"), ENERGY_MAX)
        if burn and energy <= 0:
            return {"status": "error", "msg": "NO_ENERGY"}

        swipes_today += 1
        energy = max(0, energy - burn)

        ap = safe_int(me.get("ap"), 0) + 1
        vis = safe_int(me.get("visibility_credits"), 0) + 1

        init_light = safe_int(me.get("init_light"), 0)
        init_spite = safe_int(me.get("init_spite"), 0)
        if not bool(me.get("faction_assigned")):
            if direction == "LIGHT":
                init_light += 1
            else:
                init_spite += 1

        updates = {
            "swipes_day_key": me.get("swipes_day_key"),
            "swipes_today": swipes_today,
            "energy": energy,
            "energy_updated_at": me.get("energy_updated_at"),
            "ap": ap,
            "visibility_credits": vis,
            "last_active": iso(now_utc()),
            "init_light": init_light,
            "init_spite": init_spite,
        }

        if (not bool(me.get("faction_assigned"))) and (init_light + init_spite >= 10):
            if init_light > init_spite:
                updates["faction"] = "EUPHORIA"
            elif init_spite > init_light:
                updates["faction"] = "DISSONANCE"
            else:
                updates["faction"] = random.choice(["EUPHORIA", "DISSONANCE"])
            updates["faction_assigned"] = True

        ambush = (swipes_today % AMBUSH_EVERY_N_SWIPES == 0)

        supabase.table("users").update(updates).eq("user_id", uid).execute()

        # update target only if real
        if not is_fake_user_id(target_id):
            try:
                tgt = supabase.table("users").select("user_id,visibility_credits,votes_light,votes_spite").eq("user_id", target_id).limit(1).execute()
                if tgt.data:
                    t = tgt.data[0]
                    tvis = max(0, safe_int(t.get("visibility_credits"), 0) - 1)
                    vL = safe_int(t.get("votes_light"), 0)
                    vS = safe_int(t.get("votes_spite"), 0)
                    if direction == "LIGHT":
                        vL += 1
                    else:
                        vS += 1
                    supabase.table("users").update({"visibility_credits": tvis, "votes_light": vL, "votes_spite": vS}).eq("user_id", target_id).execute()
            except Exception:
                pass

        # best effort swipe log
        try:
            supabase.table("swipes").insert({"swiper_id": uid, "target_id": target_id, "direction": direction, "created_at": iso(now_utc())}).execute()
        except Exception:
            pass

        return {
            "status": "ok",
            "new_ap": ap,
            "new_energy": energy,
            "swipes_today": swipes_today,
            "ambush": ambush,
            "energy_max": ENERGY_MAX,
            "free_swipes_per_day": FREE_SWIPES_PER_DAY,
            "fake_target": is_fake_user_id(target_id),
        }

    except Exception as e:
        return {"status": "error", "msg": str(e)}

# =========================
# COMBAT
# =========================
@app.post("/game/combat/info")
async def combat_info(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    boss_id = safe_int(req.get("boss_id"), 0)
    if boss_id <= 0:
        boss_id = ((uid + int(time.time() // 3600)) % FAKE_BOSS_COUNT) + 1

    ap = 0
    faction = "UNSORTED"
    is_founder = False

    if not supabase:
        me = DEV_USERS.get(uid) or normalize_user_state({"user_id": uid, "username": u.get("username","Citizen")})
        ap = safe_int(me.get("ap"), 0)
        faction = me.get("faction", "UNSORTED")
        is_founder = bool(me.get("is_founder"))
    else:
        try:
            me = supabase.table("users").select("ap,faction,is_founder").eq("user_id", uid).limit(1).execute().data[0]
            ap = safe_int(me.get("ap"), 0)
            faction = me.get("faction") or "UNSORTED"
            is_founder = bool(me.get("is_founder"))
        except Exception:
            pass

    base_hp = 100
    if is_founder and ap < 50:
        base_hp = 80

    hp = base_hp + min(320, ap * 2)
    dmg_mult = 1.05 if faction == "DISSONANCE" else 1.0

    return {
        "status": "ok",
        "boss": {
            "id": boss_id,
            "name": f"AMBUSH_ENTITY_{boss_id:02d}",
            "hp": hp,
            "img": f"/dev/boss/{boss_id}.svg",
        },
        "dmg_mult": dmg_mult,
    }

@app.post("/game/combat/turn")
async def combat_turn(req: dict):
    init_data = req.get("initData") or "debug_mode"
    action = (req.get("action") or "").upper()
    boss_hp_current = safe_int(req.get("boss_hp_current"), 100)
    boss_id = safe_int(req.get("boss_id"), 1)

    if action not in ("ATTACK", "POTION_HP"):
        return {"status": "error", "msg": "BAD_ACTION"}

    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    faction = "UNSORTED"
    if not supabase:
        me = DEV_USERS.get(uid) or normalize_user_state({"user_id": uid, "username": u.get("username","Citizen")})
        faction = me.get("faction", "UNSORTED")
    else:
        try:
            me = supabase.table("users").select("faction").eq("user_id", uid).limit(1).execute().data[0]
            faction = me.get("faction") or "UNSORTED"
        except Exception:
            pass

    dmg_mult = 1.05 if faction == "DISSONANCE" else 1.0

    if action == "ATTACK":
        dmg = int(random.randint(10, 18) * dmg_mult)
        new_hp = max(0, boss_hp_current - dmg)
    else:
        dmg = 0
        new_hp = boss_hp_current

    boss_hit = random.randint(7, 14)

    if new_hp <= 0:
        loot_id = random.choice(["SPONSOR_CRATE_COMMON", "SPONSOR_CRATE_RARE", "POTION_HP", "POTION_ENERGY"])

        if not supabase:
            DEV_INVENTORY.setdefault(uid, []).append({
                "item_id": loot_id,
                "boss_id": boss_id,
                "rarity": "RARE" if "RARE" in loot_id else "COMMON",
                "created_at": iso(now_utc()),
            })
        else:
            try:
                supabase.table("inventory").insert({
                    "user_id": uid,
                    "item_id": loot_id,
                    "item_type": "CRATE" if "CRATE" in loot_id else "POTION",
                    "rarity": "RARE" if "RARE" in loot_id else "COMMON",
                    "durability": 100,
                    "is_equipped": False,
                    "created_at": iso(now_utc()),
                }).execute()
            except Exception:
                pass

        return {"status": "VICTORY", "new_boss_hp": 0, "loot": loot_id, "boss_hit": boss_hit, "boss_id": boss_id}

    return {"status": "OK", "new_boss_hp": new_hp, "damage_done": dmg, "boss_hit": boss_hit, "boss_id": boss_id}
