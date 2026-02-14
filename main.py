import os, json, time, urllib.parse, random
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

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

# ===== SCALE-SAFE SERVER LAW =====
ENERGY_MAX = 30
ENERGY_REGEN_SECONDS = 180  # 1 energy / 3 minutes
FREE_SWIPES_PER_DAY = 20
ENERGY_COST_AFTER_FREE = 1
AMBUSH_EVERY_N_SWIPES = 10

FEED_ACTIVE_WINDOW_SECONDS = 300
FEED_FALLBACK_WINDOW_SECONDS = 3600
FEED_LIMIT = 20


# ===== TIME/FORMAT =====
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


# ===== AUTH =====
def validate_auth(init_data: str) -> Dict[str, Any]:
    if init_data == "debug_mode":
        return {"id": 12345, "username": "Architect"}
    try:
        parsed = dict(x.split("=", 1) for x in init_data.split("&"))
        user_json = urllib.parse.unquote(parsed.get("user", "{}"))
        return json.loads(user_json)
    except Exception:
        return {"id": 12345, "username": "Debug_User"}


# ===== DEFAULT USER STATE =====
def normalize_user_state(user: Dict[str, Any]) -> Dict[str, Any]:
    user.setdefault("ap", 0)
    user.setdefault("energy", ENERGY_MAX)
    user.setdefault("energy_updated_at", iso(now_utc()))
    user.setdefault("visibility_credits", 10)
    user.setdefault("swipes_day_key", day_key_utc())
    user.setdefault("swipes_today", 0)
    user.setdefault("last_active", iso(now_utc()))
    user.setdefault("verification_status", "VERIFIED")
    user.setdefault("biolock_status", "GATE")
    user.setdefault("role", "CITIZEN")
    user.setdefault("faction", "UNSORTED")
    user.setdefault("votes_light", 0)
    user.setdefault("votes_spite", 0)
    user.setdefault("init_light", 0)
    user.setdefault("init_spite", 0)
    user.setdefault("faction_assigned", False)
    user.setdefault("combat_lock_until", None)
    return user


# ===== LAZY ENERGY REGEN =====
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


# ===== DAILY RESET =====
def apply_daily_reset(user: Dict[str, Any]) -> Dict[str, Any]:
    today = day_key_utc()
    k = user.get("swipes_day_key") or today
    if k != today:
        user["swipes_day_key"] = today
        user["swipes_today"] = 0
        user["init_light"] = 0
        user["init_spite"] = 0
        user["faction_assigned"] = user.get("faction_assigned", False)
    return user


# ===== DEV IMAGES (SVG) =====
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
</defs>
<rect width="100%" height="100%" fill="url(#g)"/>
<circle cx="450" cy="520" r="240" fill="rgba(0,0,0,0.30)" stroke="{neon}" stroke-opacity="0.55" stroke-width="6"/>
<circle cx="360" cy="470" r="28" fill="{eye}"/><circle cx="540" cy="470" r="28" fill="{eye}"/>
<rect x="420" y="530" width="60" height="16" rx="8" fill="{eye}" opacity="0.6"/>
<path d="M340 600 C 390 680, 510 680, 560 600" stroke="{neon}" stroke-opacity="0.55" stroke-width="10" fill="none" stroke-linecap="round"/>
<text x="60" y="92" fill="{neon}" font-size="44" font-family="monospace" opacity="0.9">{name}</text>
<text x="60" y="1090" fill="{neon}" font-size="120" font-family="monospace" opacity="0.22">{glyph}</text>
</svg>"""

@app.get("/dev/fake_image/{seed}.svg")
def dev_fake(seed: int):
    return Response(content=make_fake_svg(seed, "CITIZEN"), media_type="image/svg+xml")

@app.get("/dev/boss/{boss_id}.svg")
def dev_boss(boss_id: int):
    return Response(content=make_fake_svg(1000 + boss_id, "BOSS"), media_type="image/svg+xml")


# ===== HEALTH =====
@app.get("/")
def health():
    return {"status": "AZEUQER BACKEND ONLINE", "ts": iso(now_utc())}


# ===== LOGIN =====
@app.post("/auth/login")
async def login(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))
    username = u.get("username", "Citizen")

    if not supabase:
        me = normalize_user_state({
            "user_id": uid,
            "username": username,
            "bio_lock_url": None,
        })
        me = apply_daily_reset(apply_energy_regen(me))
        me["last_active"] = iso(now_utc())
        return {"status": "ok", "user": me, "note": "SUPABASE_DISABLED"}

    try:
        res = supabase.table("users").select("*").eq("user_id", uid).limit(1).execute()
        if res.data:
            me = normalize_user_state(res.data[0])
        else:
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
                "faction": "UNSORTED",
                "votes_light": 0,
                "votes_spite": 0,
                "init_light": 0,
                "init_spite": 0,
                "faction_assigned": False,
                "last_active": iso(now_utc()),
            })
            supabase.table("users").insert(me).execute()

        before = {
            "energy": safe_int(me.get("energy"), ENERGY_MAX),
            "energy_updated_at": me.get("energy_updated_at"),
            "swipes_day_key": me.get("swipes_day_key"),
            "swipes_today": safe_int(me.get("swipes_today"), 0),
        }

        me = apply_energy_regen(me)
        me = apply_daily_reset(me)
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


# ===== EMAIL =====
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
        return {"status": "error", "msg": str(e)}


# ===== BIOLOCK =====
@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    u_data = validate_auth(initData)
    uid = safe_int(u_data.get("id"))
    content = await file.read()

    if not supabase:
        return {"status": "success", "url": f"/dev/fake_image/{uid % 20 + 1}.svg"}

    filename = f"{uid}_{int(time.time())}.jpg"
    try:
        # Upload to storage
        supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
        url = supabase.storage.from_("bio-locks").get_public_url(filename)

        # Mark pass + set last_active
        supabase.table("users").update({
            "bio_lock_url": url,
            "biolock_status": "PASS",
            "last_active": iso(now_utc()),
        }).eq("user_id", uid).execute()

        return {"status": "success", "url": url}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ===== RESET =====
@app.post("/auth/reset")
async def reset_user(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    if not supabase:
        return {"status": "RESET_COMPLETE", "note": "SUPABASE_DISABLED"}

    try:
        supabase.table("users").update({
            "bio_lock_url": None,
            "biolock_status": "GATE",
            "swipes_today": 0,
            "swipes_day_key": day_key_utc(),
            "init_light": 0,
            "init_spite": 0,
            "faction": "UNSORTED",
            "faction_assigned": False,
            "last_active": iso(now_utc()),
        }).eq("user_id", uid).execute()
        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ===== PING =====
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


# ===== TOP IMAGES =====
@app.post("/game/top_images")
async def top_images(req: dict):
    bosses = [{"boss_id": i, "name": f"BOSS_{i:02d}", "image_url": f"/dev/boss/{i}.svg"} for i in range(1, 7)]

    if not supabase:
        fake = [{"user_id": 900001+i, "username": f"Citizen_{i:02d}", "bio_lock_url": f"/dev/fake_image/{i}.svg", "votes_light": random.randint(0,40), "votes_spite": random.randint(0,40)} for i in range(1,21)]
        top_e = max(fake, key=lambda x: x["votes_light"])
        top_d = max(fake, key=lambda x: x["votes_spite"])
        return {"status": "ok", "top_euphoria": top_e, "top_dissonance": top_d, "bosses": bosses, "fake": True}

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
            fake = [{"user_id": 900001+i, "username": f"Citizen_{i:02d}", "bio_lock_url": f"/dev/fake_image/{i}.svg", "votes_light": random.randint(0,40), "votes_spite": random.randint(0,40)} for i in range(1,21)]
            top_e = max(fake, key=lambda x: x["votes_light"])
            top_d = max(fake, key=lambda x: x["votes_spite"])
            return {"status": "ok", "top_euphoria": top_e, "top_dissonance": top_d, "bosses": bosses, "fake": True}

        return {"status":"ok","top_euphoria": e.data[0], "top_dissonance": d.data[0], "bosses": bosses, "fake": False}
    except Exception:
        fake = [{"user_id": 900001+i, "username": f"Citizen_{i:02d}", "bio_lock_url": f"/dev/fake_image/{i}.svg", "votes_light": random.randint(0,40), "votes_spite": random.randint(0,40)} for i in range(1,21)]
        top_e = max(fake, key=lambda x: x["votes_light"])
        top_d = max(fake, key=lambda x: x["votes_spite"])
        return {"status":"ok","top_euphoria": top_e, "top_dissonance": top_d, "bosses": bosses, "fake": True}


# ===== FEED =====
@app.post("/game/feed")
async def feed(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    # Always keep the caller "active"
    if supabase:
        try:
            supabase.table("users").update({"last_active": iso(now_utc())}).eq("user_id", uid).execute()
        except Exception:
            pass

    if not supabase:
        fake = [{"user_id": 900001+i, "username": f"Citizen_{i:02d}", "bio_lock_url": f"/dev/fake_image/{i}.svg", "body_asset_id":"body_dev"} for i in range(1,21)]
        return {"status": "ok", "feed": fake, "fallback": True, "fake": True}

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

        if rows:
            return {"status":"ok","feed": rows[:FEED_LIMIT], "fallback": fallback, "fake": False}

        # if nobody eligible, fallback to fake
        fake = [{"user_id": 900001+i, "username": f"Citizen_{i:02d}", "bio_lock_url": f"/dev/fake_image/{i}.svg", "body_asset_id":"body_dev"} for i in range(1,21)]
        return {"status":"ok","feed": fake, "fallback": True, "fake": True}
    except Exception as e:
        fake = [{"user_id": 900001+i, "username": f"Citizen_{i:02d}", "bio_lock_url": f"/dev/fake_image/{i}.svg", "body_asset_id":"body_dev"} for i in range(1,21)]
        return {"status":"ok","feed": fake, "fallback": True, "fake": True, "note": str(e)}


# ===== SWIPE =====
@app.post("/game/swipe")
async def swipe(req: dict):
    init_data = req.get("initData") or "debug_mode"
    direction = (req.get("direction") or "").upper()
    target_id = safe_int(req.get("target_id"))
    if direction not in ("LIGHT","SPITE"):
        return {"status":"error","msg":"BAD_DIRECTION"}

    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    if not supabase:
        swipes_today = 1
        ambush = (swipes_today % AMBUSH_EVERY_N_SWIPES == 0)
        return {"status":"ok","new_ap":1,"new_energy":ENERGY_MAX,"new_visibility_credits":10,"swipes_today":swipes_today,"ambush":ambush}

    try:
        me_res = supabase.table("users").select("*").eq("user_id", uid).limit(1).execute()
        if not me_res.data:
            return {"status":"error","msg":"NO_USER"}
        me = normalize_user_state(me_res.data[0])

        # regen + reset
        me = apply_energy_regen(me)
        me = apply_daily_reset(me)

        if not me.get("bio_lock_url"):
            return {"status":"error","msg":"BIOLOCK_REQUIRED"}

        # cost
        swipes_today = safe_int(me.get("swipes_today"), 0)
        burn = 0
        if swipes_today >= FREE_SWIPES_PER_DAY:
            burn = ENERGY_COST_AFTER_FREE

        energy = safe_int(me.get("energy"), ENERGY_MAX)
        if burn and energy <= 0:
            return {"status":"error","msg":"NO_ENERGY"}

        # apply
        swipes_today += 1
        energy = max(0, energy - burn)

        # faction buff
        ap_gain = 1
        if (me.get("faction") == "EUPHORIA"):
            ap_gain = 2 if random.random() < 0.05 else 1  # +5% passive AP gain (cheap implementation)
        ap = safe_int(me.get("ap"), 0) + ap_gain

        vis = safe_int(me.get("visibility_credits"), 0) + 1

        # initiation counters (first 10 swipes decide faction)
        init_light = safe_int(me.get("init_light"), 0)
        init_spite = safe_int(me.get("init_spite"), 0)
        if not bool(me.get("faction_assigned")):
            if direction == "LIGHT":
                init_light += 1
            else:
                init_spite += 1

        ambush = (swipes_today % AMBUSH_EVERY_N_SWIPES == 0)

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

        # On the 10th initiation swipe: assign faction
        if (not bool(me.get("faction_assigned"))) and (init_light + init_spite >= 10):
            if init_light > init_spite:
                faction = "EUPHORIA"
            elif init_spite > init_light:
                faction = "DISSONANCE"
            else:
                faction = random.choice(["EUPHORIA","DISSONANCE"])
            updates["faction"] = faction
            updates["faction_assigned"] = True

        supabase.table("users").update(updates).eq("user_id", uid).execute()

        # target impact (best-effort)
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
                supabase.table("users").update({
                    "visibility_credits": tvis,
                    "votes_light": vL,
                    "votes_spite": vS,
                }).eq("user_id", target_id).execute()
        except Exception:
            pass

        # optional swipe log (never used for logic)
        try:
            supabase.table("swipes").insert({
                "swiper_id": uid,
                "target_id": target_id,
                "direction": direction,
                "created_at": iso(now_utc()),
            }).execute()
        except Exception:
            pass

        return {
            "status":"ok",
            "new_ap": ap,
            "new_energy": energy,
            "new_visibility_credits": vis,
            "swipes_today": swipes_today,
            "ambush": ambush,
            "init_light": init_light,
            "init_spite": init_spite,
            "faction": updates.get("faction", me.get("faction")),
            "faction_assigned": updates.get("faction_assigned", me.get("faction_assigned")),
            "energy_max": ENERGY_MAX,
            "energy_regen_seconds": ENERGY_REGEN_SECONDS,
            "free_swipes_per_day": FREE_SWIPES_PER_DAY
        }

    except Exception as e:
        return {"status":"error","msg": str(e)}


# ===== COMBAT =====
@app.post("/game/combat/info")
async def combat_info(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    # simple boss scaling off AP (cheap, safe)
    if not supabase:
        return {"status":"ok","boss":{"id":1,"name":"DEV_BOSS","hp":100,"img":"/dev/boss/1.svg"}}

    try:
        me = supabase.table("users").select("ap,faction,combat_lock_until").eq("user_id", uid).limit(1).execute().data[0]
        ap = safe_int(me.get("ap"), 0)

        # boss scales mildly
        hp = 80 + min(220, ap * 2)

        # Dissonance buff: +5% damage later; we return it so frontend can show
        faction = me.get("faction") or "UNSORTED"
        dmg_mult = 1.05 if faction == "DISSONANCE" else 1.0

        return {"status":"ok","boss":{"id":1,"name":"AMBUSH_ENTITY","hp":hp,"img":"/dev/boss/1.svg"}, "dmg_mult": dmg_mult}
    except Exception as e:
        return {"status":"error","msg":str(e)}


@app.post("/game/combat/turn")
async def combat_turn(req: dict):
    init_data = req.get("initData") or "debug_mode"
    action = (req.get("action") or "").upper()
    boss_hp_current = safe_int(req.get("boss_hp_current"), 100)

    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    # Minimal turn simulation:
    # - ATTACK: player hits (base 8..16), +5% if DISSONANCE
    # - POTION_HP: heals player (no player HP tracked yet, just flavor)
    # - Boss hits back (base 6..12) -> not tracked yet
    # - Victory: loot inserted into inventory

    if action not in ("ATTACK","POTION_HP"):
        return {"status":"error","msg":"BAD_ACTION"}

    if not supabase:
        new_hp = max(0, boss_hp_current - 12) if action == "ATTACK" else boss_hp_current
        if new_hp <= 0:
            return {"status":"VICTORY","new_boss_hp":0,"loot":"DEV_CRATE"}
        return {"status":"OK","new_boss_hp":new_hp,"boss_hit":8}

    try:
        me = supabase.table("users").select("faction").eq("user_id", uid).limit(1).execute().data[0]
        faction = me.get("faction") or "UNSORTED"
        dmg_mult = 1.05 if faction == "DISSONANCE" else 1.0

        if action == "ATTACK":
            dmg = int(random.randint(8, 16) * dmg_mult)
            new_hp = max(0, boss_hp_current - dmg)
        else:
            new_hp = boss_hp_current

        boss_hit = random.randint(6, 12)

        if new_hp <= 0:
            # drop loot (simple)
            loot_id = random.choice(["SPONSOR_CRATE_COMMON","SPONSOR_CRATE_RARE","POTION_HP","POTION_ENERGY"])
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

            return {"status":"VICTORY","new_boss_hp":0,"loot":loot_id,"boss_hit":boss_hit}

        return {"status":"OK","new_boss_hp":new_hp,"boss_hit":boss_hit}

    except Exception as e:
        return {"status":"error","msg": str(e)}
