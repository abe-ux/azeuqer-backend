# AZEUQER TITANIUM — SCALE-SAFE PROMPT 2 (Backend-authoritative timers + swipe counters + 10th swipe ambush)
import os, json, time, urllib.parse, random
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from supabase import create_client

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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UTC = timezone.utc

# =========================
# CONFIG (server law)
# =========================
ENERGY_MAX = 30
ENERGY_REGEN_SECONDS = 180  # 1 energy / 3 mins
FREE_SWIPES_PER_DAY = 20
ENERGY_COST_PER_SWIPE_AFTER_FREE = 1
AMBUSH_EVERY_N_SWIPES = 10

FEED_ACTIVE_WINDOW_SECONDS = 300   # 5 min (Ghost Protocol)
FEED_FALLBACK_WINDOW_SECONDS = 3600  # 1 hour fallback
FEED_LIMIT = 20

# =========================
# UTILS
# =========================
def now_utc() -> datetime:
    return datetime.now(tz=UTC)

def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()

def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # python can parse isoformat with timezone
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
    """
    Ensure key fields exist with safe defaults.
    We do NOT force schema changes here — just safe values in runtime.
    """
    user.setdefault("ap", 0)
    user.setdefault("energy", ENERGY_MAX)
    user.setdefault("energy_updated_at", iso(now_utc()))
    user.setdefault("visibility_credits", 10)
    user.setdefault("swipes_day_key", day_key_utc())
    user.setdefault("swipes_today", 0)
    user.setdefault("last_active", iso(now_utc()))
    user.setdefault("verification_status", "VERIFIED")
    user.setdefault("biolock_status", "GATE")
    user.setdefault("votes_light", 0)
    user.setdefault("votes_spite", 0)
    user.setdefault("faction", "UNSORTED")
    return user

def apply_energy_regen(user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Server-authoritative energy regen.
    Updates user.energy based on elapsed time since energy_updated_at.
    Does not write to DB here (caller decides).
    """
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
    # Move the timestamp forward by the consumed regen intervals
    new_updated_at = updated_at + timedelta(seconds=gained * ENERGY_REGEN_SECONDS)

    user["energy"] = new_energy
    user["energy_updated_at"] = iso(new_updated_at)
    return user

def apply_daily_reset(user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resets swipes_today if date changed (UTC).
    """
    k = user.get("swipes_day_key") or day_key_utc()
    today = day_key_utc()
    if k != today:
        user["swipes_day_key"] = today
        user["swipes_today"] = 0
    return user

# =========================
# DEV SVG IMAGES (cheap, no storage)
# =========================
def make_fake_svg(seed: int, label: str = "CITIZEN") -> str:
    rnd = random.Random(seed)
    bg1 = f"#{rnd.randrange(0x111111, 0xFFFFFF):06x}"
    bg2 = f"#{rnd.randrange(0x111111, 0xFFFFFF):06x}"
    neon = f"#{rnd.randrange(0x00FFFF, 0xFFFFFF):06x}"
    eye = f"#{rnd.randrange(0x000000, 0x222222):06x}"
    name = f"{label}-{seed:02d}"
    glyph = rnd.choice(["∆","Ø","Ψ","Λ","Σ","⊕","⋈","⟁","⟡","⟢","⟣","⟐"])

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1200">
<defs>
  <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="{bg1}"/>
    <stop offset="100%" stop-color="{bg2}"/>
  </linearGradient>
  <radialGradient id="a" cx="30%" cy="20%" r="70%">
    <stop offset="0%" stop-color="{neon}" stop-opacity="0.35"/>
    <stop offset="65%" stop-color="#000" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect width="100%" height="100%" fill="url(#g)"/>
<rect width="100%" height="100%" fill="url(#a)"/>

<circle cx="450" cy="520" r="240" fill="rgba(0,0,0,0.28)" stroke="{neon}" stroke-opacity="0.55" stroke-width="6"/>
<circle cx="360" cy="470" r="28" fill="{eye}"/><circle cx="540" cy="470" r="28" fill="{eye}"/>
<rect x="420" y="530" width="60" height="16" rx="8" fill="{eye}" opacity="0.6"/>
<path d="M340 600 C 390 680, 510 680, 560 600" stroke="{neon}" stroke-opacity="0.55" stroke-width="10" fill="none" stroke-linecap="round"/>

<rect x="0" y="120" width="900" height="18" fill="rgba(255,255,255,0.06)"/>
<rect x="0" y="860" width="900" height="22" fill="rgba(0,0,0,0.18)"/>
<rect x="0" y="900" width="900" height="8" fill="rgba(0,255,204,0.10)"/>

<text x="60" y="92" fill="{neon}" font-size="44" font-family="monospace" opacity="0.9">{name}</text>
<text x="60" y="1040" fill="#ffffff" font-size="28" font-family="monospace" opacity="0.82">AZEUQER /// GHOST PROTOCOL</text>
<text x="60" y="1090" fill="{neon}" font-size="120" font-family="monospace" opacity="0.22">{glyph}</text>
</svg>"""

def fake_targets(count: int = 20) -> List[Dict[str, Any]]:
    out = []
    for i in range(1, count + 1):
        out.append({
            "user_id": 900000 + i,
            "username": f"Citizen_{i:02d}",
            "bio_lock_url": f"/dev/fake_image/{i}.svg",
            "body_asset_id": random.choice(["body_diligent_fast","body_lazy_slow","body_smart_tough","body_cool_nerd"]),
            "votes_light": random.randint(0, 40),
            "votes_spite": random.randint(0, 40),
            "faction": "EUPHORIA" if i % 2 == 0 else "DISSONANCE",
        })
    return out

def fake_bosses() -> List[Dict[str, Any]]:
    return [{"boss_id": i, "name": f"BOSS_{i:02d}", "image_url": f"/dev/boss/{i}.svg"} for i in range(1, 9)]


@app.get("/")
def health():
    return {"status": "AZEUQER BACKEND ONLINE", "ts": iso(now_utc())}

@app.get("/dev/fake_image/{seed}.svg")
def dev_fake(seed: int):
    return Response(content=make_fake_svg(seed, "CITIZEN"), media_type="image/svg+xml")

@app.get("/dev/boss/{boss_id}.svg")
def dev_boss(boss_id: int):
    return Response(content=make_fake_svg(1000 + boss_id, "BOSS"), media_type="image/svg+xml")


# =========================
# AUTH / PROFILE CORE
# =========================
@app.post("/auth/login")
async def login(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))
    username = u.get("username", "Citizen")

    # dev mode (no db)
    if not supabase:
        user_obj = normalize_user_state({
            "user_id": uid,
            "username": username,
            "bio_lock_url": None,
            "biolock_status": "GATE",
            "verification_status": "VERIFIED",
            "role": "CITIZEN",
        })
        user_obj = apply_daily_reset(apply_energy_regen(user_obj))
        user_obj["last_active"] = iso(now_utc())
        return {"status": "ok", "user": user_obj, "note": "SUPABASE_DISABLED"}

    try:
        res = supabase.table("users").select("*").eq("user_id", uid).execute()
        if res.data:
            me = normalize_user_state(res.data[0])
        else:
            # Create minimal row; extra columns may exist safely
            me = normalize_user_state({
                "user_id": uid,
                "username": username,
                "ap": 0,
                "energy": ENERGY_MAX,
                "energy_updated_at": iso(now_utc()),
                "visibility_credits": 10,
                "swipes_day_key": day_key_utc(),
                "swipes_today": 0,
                "biolock_status": "GATE",
                "verification_status": "VERIFIED",
                "role": "CITIZEN",
                "votes_light": 0,
                "votes_spite": 0,
                "last_active": iso(now_utc()),
            })
            supabase.table("users").insert(me).execute()

        # Apply regen + daily reset and persist only if changed
        before_energy = safe_int(me.get("energy"), ENERGY_MAX)
        before_energy_ts = me.get("energy_updated_at")
        before_day_key = me.get("swipes_day_key")
        before_swipes = safe_int(me.get("swipes_today"), 0)

        me = apply_energy_regen(me)
        me = apply_daily_reset(me)
        me["last_active"] = iso(now_utc())

        updates = {}
        if me.get("energy") != before_energy:
            updates["energy"] = me["energy"]
        if me.get("energy_updated_at") != before_energy_ts:
            updates["energy_updated_at"] = me["energy_updated_at"]
        if me.get("swipes_day_key") != before_day_key:
            updates["swipes_day_key"] = me["swipes_day_key"]
        if safe_int(me.get("swipes_today"), 0) != before_swipes:
            updates["swipes_today"] = me["swipes_today"]
        updates["last_active"] = me["last_active"]

        if updates:
            supabase.table("users").update(updates).eq("user_id", uid).execute()

        return {"status": "ok", "user": me}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.post("/auth/email")
async def set_email(req: dict):
    init_data = req.get("initData") or "debug_mode"
    email = (req.get("email") or "").strip().lower()
    if "@" not in email or "." not in email or len(email) < 6:
        return {"status": "error", "msg": "INVALID_EMAIL"}

    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    if not supabase:
        return {"status": "ok", "email": email, "note": "SUPABASE_DISABLED"}

    try:
        supabase.table("users").update({"email": email}).eq("user_id", uid).execute()
        return {"status": "ok", "email": email}
    except Exception as e:
        msg = str(e)
        if "column" in msg and "email" in msg:
            return {"status": "error", "msg": "EMAIL_COLUMN_MISSING"}
        return {"status": "error", "msg": msg}


@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    u_data = validate_auth(initData)
    uid = safe_int(u_data.get("id"))
    content = await file.read()

    if not supabase:
        return {"status": "success", "url": f"/dev/fake_image/{uid % 20 + 1}.svg"}

    filename = f"{uid}_{int(time.time())}.jpg"
    try:
        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            return {"status": "error", "msg": "BUCKET_FAIL"}

        # mark pass
        try:
            supabase.table("users").update({
                "bio_lock_url": url,
                "biolock_status": "PASS",
                "last_active": iso(now_utc())
            }).eq("user_id", uid).execute()
        except Exception:
            supabase.table("users").update({"bio_lock_url": url}).eq("user_id", uid).execute()

        return {"status": "success", "url": url}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.post("/auth/reset")
async def reset_user(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u_data = validate_auth(init_data)
    uid = safe_int(u_data.get("id"))

    if not supabase:
        return {"status": "RESET_COMPLETE", "note": "SUPABASE_DISABLED"}

    try:
        supabase.table("users").update({
            "bio_lock_url": None,
            "biolock_status": "GATE",
            "swipes_today": 0,
            "swipes_day_key": day_key_utc(),
            "last_active": iso(now_utc())
        }).eq("user_id", uid).execute()
        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# =========================
# ALIVE: PING (Ghost Protocol)
# =========================
@app.post("/game/ping")
async def ping(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    if not supabase:
        return {"status": "ok", "ts": iso(now_utc()), "note": "SUPABASE_DISABLED"}

    try:
        supabase.table("users").update({"last_active": iso(now_utc())}).eq("user_id", uid).execute()
        return {"status": "ok", "ts": iso(now_utc())}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# =========================
# TOP IMAGES (Order/Chaos)
# =========================
@app.post("/game/top_images")
async def top_images(req: dict):
    if not supabase:
        f = fake_targets(20)
        top_e = max(f, key=lambda x: x.get("votes_light", 0))
        top_d = max(f, key=lambda x: x.get("votes_spite", 0))
        return {"status": "ok", "top_euphoria": top_e, "top_dissonance": top_d, "bosses": fake_bosses(), "fake": True}

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
            f = fake_targets(20)
            top_e = max(f, key=lambda x: x.get("votes_light", 0))
            top_d = max(f, key=lambda x: x.get("votes_spite", 0))
            return {"status": "ok", "top_euphoria": top_e, "top_dissonance": top_d, "bosses": fake_bosses(), "fake": True}

        return {"status": "ok", "top_euphoria": e.data[0], "top_dissonance": d.data[0], "bosses": fake_bosses(), "fake": False}
    except Exception:
        f = fake_targets(20)
        top_e = max(f, key=lambda x: x.get("votes_light", 0))
        top_d = max(f, key=lambda x: x.get("votes_spite", 0))
        return {"status": "ok", "top_euphoria": top_e, "top_dissonance": top_d, "bosses": fake_bosses(), "fake": True}


# =========================
# FEED (Scale-safe Ghost Protocol)
# =========================
@app.post("/game/feed")
async def feed(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    if not supabase:
        return {"status": "ok", "feed": fake_targets(20), "fallback": True, "fake": True}

    try:
        cutoff_5m = iso(now_utc() - timedelta(seconds=FEED_ACTIVE_WINDOW_SECONDS))
        cutoff_1h = iso(now_utc() - timedelta(seconds=FEED_FALLBACK_WINDOW_SECONDS))

        def query(cutoff_iso: str):
            # IMPORTANT FOR SCALE:
            # - limit
            # - indexed filters (verification_status, biolock_status, last_active, visibility_credits)
            # - no ORDER BY random()
            return (supabase.table("users")
                    .select("user_id,username,bio_lock_url,body_asset_id,votes_light,votes_spite,faction")
                    .eq("verification_status", "VERIFIED")
                    .eq("biolock_status", "PASS")
                    .gt("last_active", cutoff_iso)
                    .gt("visibility_credits", 0)
                    .neq("user_id", uid)
                    .order("last_active", desc=True)
                    .limit(FEED_LIMIT).execute())

        r = query(cutoff_5m)
        rows = r.data or []
        fallback = False

        if len(rows) < 5:
            fallback = True
            r2 = query(cutoff_1h)
            rows = r2.data or []

        if not rows:
            return {"status": "ok", "feed": fake_targets(20), "fallback": True, "fake": True}

        return {"status": "ok", "feed": rows[:FEED_LIMIT], "fallback": fallback}
    except Exception as e:
        return {"status": "ok", "feed": fake_targets(20), "fallback": True, "fake": True, "note": str(e)}


# =========================
# SWIPE (O(1) logic, server law, ambush trigger)
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

    if not supabase:
        # simulate
        sw = 1
        ambush = (sw % AMBUSH_EVERY_N_SWIPES == 0)
        return {"status": "ok", "new_ap": 1, "new_energy": ENERGY_MAX, "new_visibility_credits": 10, "swipes_today": sw, "ambush": ambush, "note": "SUPABASE_DISABLED"}

    try:
        me_res = supabase.table("users").select("*").eq("user_id", uid).limit(1).execute()
        if not me_res.data:
            return {"status": "error", "msg": "NO_USER"}
        me = normalize_user_state(me_res.data[0])

        # Server law: regen + daily reset
        before = {
            "energy": safe_int(me.get("energy"), ENERGY_MAX),
            "energy_updated_at": me.get("energy_updated_at"),
            "swipes_day_key": me.get("swipes_day_key"),
            "swipes_today": safe_int(me.get("swipes_today"), 0),
            "ap": safe_int(me.get("ap"), 0),
            "visibility_credits": safe_int(me.get("visibility_credits"), 0),
        }

        me = apply_energy_regen(me)
        me = apply_daily_reset(me)

        # gates
        if not me.get("bio_lock_url"):
            return {"status": "error", "msg": "BIOLOCK_REQUIRED"}
        if me.get("verification_status") not in (None, "VERIFIED"):
            return {"status": "error", "msg": "NOT_VERIFIED"}

        swipes_today = safe_int(me.get("swipes_today"), 0)

        # cost logic
        burn = 0
        if swipes_today >= FREE_SWIPES_PER_DAY:
            burn = ENERGY_COST_PER_SWIPE_AFTER_FREE

        energy = safe_int(me.get("energy"), ENERGY_MAX)
        if burn and energy <= 0:
            return {"status": "error", "msg": "NO_ENERGY"}

        # apply swipe
        swipes_today += 1
        energy = energy - burn
        ap = safe_int(me.get("ap"), 0) + 1
        vis = safe_int(me.get("visibility_credits"), 0) + 1

        # deterministic ambush on exact Nth swipe
        ambush = (swipes_today % AMBUSH_EVERY_N_SWIPES == 0)

        # Persist minimal writes (single update)
        updates = {
            "swipes_day_key": me.get("swipes_day_key"),
            "swipes_today": swipes_today,
            "energy": energy,
            "energy_updated_at": me.get("energy_updated_at"),  # may have changed from regen
            "ap": ap,
            "visibility_credits": vis,
            "last_active": iso(now_utc()),
        }
        supabase.table("users").update(updates).eq("user_id", uid).execute()

        # Target updates best-effort (do NOT block swipe if target fails)
        try:
            tgt_res = supabase.table("users").select("user_id,visibility_credits,votes_light,votes_spite").eq("user_id", target_id).limit(1).execute()
            if tgt_res.data:
                t = tgt_res.data[0]
                tvis = max(0, safe_int(t.get("visibility_credits"), 0) - 1)
                vL = safe_int(t.get("votes_light"), 0)
                vS = safe_int(t.get("votes_spite"), 0)
                if direction == "LIGHT":
                    vL += 1
                else:
                    vS += 1
                supabase.table("users").update({
                    "visibility_credits": tvis,
                    "votes_light": vL,
                    "votes_spite": vS
                }).eq("user_id", target_id).execute()
        except Exception:
            pass

        # Optional: store swipe log (not used for logic, so it can't DOS you)
        try:
            supabase.table("swipes").insert({
                "swiper_id": uid,
                "target_id": target_id,
                "direction": direction,
                "created_at": iso(now_utc())
            }).execute()
        except Exception:
            pass

        return {
            "status": "ok",
            "new_ap": ap,
            "new_energy": energy,
            "new_visibility_credits": vis,
            "swipes_today": swipes_today,
            "ambush": ambush,
            "free_swipes_per_day": FREE_SWIPES_PER_DAY,
            "energy_max": ENERGY_MAX,
            "energy_regen_seconds": ENERGY_REGEN_SECONDS
        }

    except Exception as e:
        return {"status": "error", "msg": str(e)}
