# AZEUQER TITANIUM — Prompt 2 Systemic Polish (Feed Alive + Top Images + Email + Fake Targets + Boss SVG)
import os, json, time, urllib.parse, random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
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
# UTILS
# =========================
def now_utc() -> datetime:
    return datetime.now(tz=UTC)

def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()

def day_key_utc(dt: Optional[datetime] = None) -> str:
    dt = dt or now_utc()
    return dt.strftime("%Y-%m-%d")

def validate_auth(init_data: str) -> Dict[str, Any]:
    """
    Telegram initData parsing (minimal).
    debug_mode returns a static user.
    """
    if init_data == "debug_mode":
        return {"id": 12345, "username": "Architect"}

    try:
        parsed = dict(x.split("=", 1) for x in init_data.split("&"))
        user_json = urllib.parse.unquote(parsed.get("user", "{}"))
        return json.loads(user_json)
    except Exception:
        return {"id": 12345, "username": "Debug_User"}

def safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default

def make_fake_svg(seed: int, label: str = "CITIZEN") -> str:
    """
    Deterministic fake image (SVG) — no storage needed.
    """
    rnd = random.Random(seed)
    bg1 = f"#{rnd.randrange(0x111111, 0xFFFFFF):06x}"
    bg2 = f"#{rnd.randrange(0x111111, 0xFFFFFF):06x}"
    neon = f"#{rnd.randrange(0x00FFFF, 0xFFFFFF):06x}"
    eye = f"#{rnd.randrange(0x000000, 0x222222):06x}"
    name = f"{label}-{seed:02d}"
    glyph = rnd.choice(["∆","Ø","Ψ","Λ","Σ","⊕","⋈","⟁","⟡","⟢","⟣","⟐"])

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1200">
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

<!-- “face-like” abstract -->
<circle cx="450" cy="520" r="240" fill="rgba(0,0,0,0.28)" stroke="{neon}" stroke-opacity="0.55" stroke-width="6"/>
<circle cx="360" cy="470" r="28" fill="{eye}"/><circle cx="540" cy="470" r="28" fill="{eye}"/>
<rect x="420" y="530" width="60" height="16" rx="8" fill="{eye}" opacity="0.6"/>
<path d="M340 600 C 390 680, 510 680, 560 600" stroke="{neon}" stroke-opacity="0.55" stroke-width="10" fill="none" stroke-linecap="round"/>

<!-- glitch bars -->
<rect x="0" y="120" width="900" height="18" fill="rgba(255,255,255,0.06)"/>
<rect x="0" y="860" width="900" height="22" fill="rgba(0,0,0,0.18)"/>
<rect x="0" y="900" width="900" height="8" fill="rgba(0,255,204,0.10)"/>

<!-- text -->
<text x="60" y="92" fill="{neon}" font-size="44" font-family="monospace" opacity="0.9">{name}</text>
<text x="60" y="1040" fill="#ffffff" font-size="28" font-family="monospace" opacity="0.82">AZEUQER /// GHOST PROTOCOL</text>
<text x="60" y="1090" fill="{neon}" font-size="120" font-family="monospace" opacity="0.22">{glyph}</text>

</svg>"""
    return svg

def fake_targets(count: int = 20) -> List[Dict[str, Any]]:
    out = []
    for i in range(1, count + 1):
        out.append({
            "user_id": 900000 + i,
            "username": f"Citizen_{i:02d}",
            "bio_lock_url": f"/dev/fake_image/{i}.svg",
            "body_asset_id": random.choice(["body_diligent_fast", "body_lazy_slow", "body_smart_tough", "body_cool_nerd"]),
            "votes_light": random.randint(0, 40),
            "votes_spite": random.randint(0, 40),
            "faction": "EUPHORIA" if i % 2 == 0 else "DISSONANCE",
        })
    return out

def fake_bosses() -> List[Dict[str, Any]]:
    return [{"boss_id": i, "name": f"BOSS_{i:02d}", "image_url": f"/dev/boss/{i}.svg"} for i in range(1, 9)]


# =========================
# HEALTH
# =========================
@app.get("/")
def health_check():
    return {"status": "AZEUQER BACKEND ONLINE", "ts": iso(now_utc())}


# =========================
# DEV IMAGE ENDPOINTS
# =========================
@app.get("/dev/fake_image/{seed}.svg")
def dev_fake(seed: int):
    svg = make_fake_svg(seed, "CITIZEN")
    return Response(content=svg, media_type="image/svg+xml")

@app.get("/dev/boss/{boss_id}.svg")
def dev_boss(boss_id: int):
    seed = 1000 + boss_id
    svg = make_fake_svg(seed, "BOSS")
    return Response(content=svg, media_type="image/svg+xml")


# =========================
# AUTH / PROFILE
# =========================
@app.post("/auth/login")
async def login(req: dict):
    """
    Returns user, creates if missing.
    Also makes sure last_active is updated (Ghost Protocol).
    """
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))

    # fallback user object
    user_obj = {
        "user_id": uid,
        "username": u.get("username", "Citizen"),
        "ap": 0,
        "energy": 30,
        "visibility_credits": 10,
        "bio_lock_url": None,
        "biolock_status": "GATE",
        "verification_status": "VERIFIED",  # for now; your Tribunal can change later
        "role": "CITIZEN",
        "email": None,
        "votes_light": 0,
        "votes_spite": 0,
        "last_active": iso(now_utc()),
        "swipes_today": 0,
    }

    if not supabase:
        return {"status": "ok", "user": user_obj, "note": "SUPABASE_DISABLED"}

    try:
        # find existing
        res = supabase.table("users").select("*").eq("user_id", uid).execute()
        if res.data:
            user_obj = res.data[0]
            # update last_active best-effort
            try:
                supabase.table("users").update({"last_active": iso(now_utc())}).eq("user_id", uid).execute()
                user_obj["last_active"] = iso(now_utc())
            except Exception:
                pass
            return {"status": "ok", "user": user_obj}

        # create new user (minimal fields; extra columns OK if present)
        new_user = {
            "user_id": uid,
            "username": u.get("username", "Citizen"),
            "ap": 0,
            "energy": 30,
            "visibility_credits": 10,
            "biolock_status": "GATE",
            "verification_status": "VERIFIED",
            "role": "CITIZEN",
            "votes_light": 0,
            "votes_spite": 0,
            "last_active": iso(now_utc()),
        }
        supabase.table("users").insert(new_user).execute()
        return {"status": "ok", "user": new_user}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.post("/auth/email")
async def set_email(req: dict):
    """
    Stores email on the user row (expects users.email column).
    If column missing, it returns EMAIL_COLUMN_MISSING.
    """
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
    """
    Uploads selfie to supabase storage (bio-locks bucket), updates users.bio_lock_url and biolock_status=PASS.
    """
    u_data = validate_auth(initData)
    uid = safe_int(u_data.get("id"))

    content = await file.read()

    # If no supabase, just "pass" with dev image
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

        # update user
        try:
            supabase.table("users").update({"bio_lock_url": url, "biolock_status": "PASS"}).eq("user_id", uid).execute()
        except Exception:
            # if columns missing, still store bio_lock_url
            supabase.table("users").update({"bio_lock_url": url}).eq("user_id", uid).execute()

        return {"status": "success", "url": url}
    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        return {"status": "error", "msg": str(e)}


@app.post("/auth/reset")
async def reset_user(req: dict):
    init_data = req.get("initData") or "debug_mode"
    u_data = validate_auth(init_data)
    uid = safe_int(u_data.get("id"))

    if not supabase:
        return {"status": "RESET_COMPLETE", "note": "SUPABASE_DISABLED"}

    try:
        supabase.table("users").update({"bio_lock_url": None, "biolock_status": "GATE"}).eq("user_id", uid).execute()
        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        # fallback only bio_lock_url
        try:
            supabase.table("users").update({"bio_lock_url": None}).eq("user_id", uid).execute()
            return {"status": "RESET_COMPLETE"}
        except Exception:
            return {"status": "error", "msg": str(e)}


# =========================
# GHOST PROTOCOL (Alive)
# =========================
@app.post("/game/ping")
async def game_ping(req: dict):
    """
    Marks user active. Used for Ghost Protocol.
    """
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
# TOP IMAGES (EUPHORIA / DISSONANCE)
# =========================
@app.post("/game/top_images")
async def top_images(req: dict):
    """
    Returns the current top Euphoria (votes_light) and top Dissonance (votes_spite).
    Falls back to fake targets if missing data.
    """
    if not supabase:
        f = fake_targets(20)
        top_e = max(f, key=lambda x: x.get("votes_light", 0))
        top_d = max(f, key=lambda x: x.get("votes_spite", 0))
        return {"status": "ok", "top_euphoria": top_e, "top_dissonance": top_d, "bosses": fake_bosses(), "fake": True}

    try:
        # These columns should exist for real mode:
        # users: votes_light, votes_spite, verification_status, biolock_status, bio_lock_url
        e = supabase.table("users").select("user_id,username,bio_lock_url,votes_light,votes_spite").eq("verification_status","VERIFIED").eq("biolock_status","PASS").order("votes_light", desc=True).limit(1).execute()
        d = supabase.table("users").select("user_id,username,bio_lock_url,votes_light,votes_spite").eq("verification_status","VERIFIED").eq("biolock_status","PASS").order("votes_spite", desc=True).limit(1).execute()

        # boss images always available
        bosses = fake_bosses()

        if not e.data or not d.data:
            f = fake_targets(20)
            top_e = max(f, key=lambda x: x.get("votes_light", 0))
            top_d = max(f, key=lambda x: x.get("votes_spite", 0))
            return {"status":"ok","top_euphoria":top_e,"top_dissonance":top_d,"bosses":bosses,"fake":True}

        return {"status":"ok","top_euphoria":e.data[0],"top_dissonance":d.data[0],"bosses":bosses,"fake":False}
    except Exception:
        f = fake_targets(20)
        top_e = max(f, key=lambda x: x.get("votes_light", 0))
        top_d = max(f, key=lambda x: x.get("votes_spite", 0))
        return {"status":"ok","top_euphoria":top_e,"top_dissonance":top_d,"bosses":fake_bosses(),"fake":True}


# =========================
# FEED (Ghost Protocol + fake fallback)
# =========================
@app.post("/game/feed")
async def game_feed(req: dict):
    """
    Ghost Protocol feed:
      1) VERIFIED
      2) biolock PASS
      3) last_active within 5 minutes
      4) visibility_credits > 0
      5) exclude already swiped today (best-effort)
    Fallback:
      if <5 results, relax last_active to 1 hour.
      if still empty, return 20 fake targets.
    """
    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))
    today = day_key_utc()

    # default: 0
    swiped_today_ids: List[int] = []

    if not supabase:
        return {"status":"ok","feed":fake_targets(20),"fallback":True,"note":"SUPABASE_DISABLED"}

    try:
        # Try to pull swipes today (requires swipes table)
        try:
            start = iso(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC))
            end = iso((datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)))
            sw = supabase.table("swipes").select("target_id").eq("swiper_id", uid).gte("created_at", start).lt("created_at", end).execute()
            swiped_today_ids = [safe_int(x.get("target_id")) for x in (sw.data or []) if x.get("target_id") is not None]
        except Exception:
            swiped_today_ids = []

        cutoff_5m = iso(now_utc() - timedelta(minutes=5))
        cutoff_1h = iso(now_utc() - timedelta(hours=1))

        def query_feed(cutoff_iso: str, limit: int = 30):
            q = (supabase.table("users")
                    .select("user_id,username,bio_lock_url,body_asset_id,votes_light,votes_spite,faction")
                    .eq("verification_status","VERIFIED")
                    .eq("biolock_status","PASS")
                    .gt("last_active", cutoff_iso)
                    .gt("visibility_credits", 0)
                    .neq("user_id", uid)
                    .limit(limit))
            return q.execute()

        res = query_feed(cutoff_5m, limit=50)
        rows = res.data or []

        # filter swiped
        if swiped_today_ids:
            rows = [r for r in rows if safe_int(r.get("user_id")) not in swiped_today_ids]

        fallback_used = False
        if len(rows) < 5:
            fallback_used = True
            res2 = query_feed(cutoff_1h, limit=80)
            rows = res2.data or []
            if swiped_today_ids:
                rows = [r for r in rows if safe_int(r.get("user_id")) not in swiped_today_ids]

        if not rows:
            # absolute fallback: fake pack
            return {"status":"ok","feed":fake_targets(20),"fallback":True,"fake":True}

        # cap
        return {"status":"ok","feed":rows[:20],"fallback":fallback_used}
    except Exception as e:
        # safest fallback
        return {"status":"ok","feed":fake_targets(20),"fallback":True,"fake":True,"note":str(e)}


@app.post("/game/feed_debug")
async def feed_debug(req: dict):
    """
    Optional: explains why feed might be empty.
    """
    if not supabase:
        return {"status":"ok","msg":"SUPABASE_DISABLED"}

    init_data = req.get("initData") or "debug_mode"
    u = validate_auth(init_data)
    uid = safe_int(u.get("id"))
    today = day_key_utc()
    start = iso(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC))
    end = iso((datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)))
    cutoff_5m = iso(now_utc() - timedelta(minutes=5))
    cutoff_1h = iso(now_utc() - timedelta(hours=1))

    def count_q(cutoff_iso: str):
        out = {"total_verified_pass":0,"active_cutoff":0,"active_and_vis_gt0":0,"after_swiped_today_filter":0}
        try:
            a = supabase.table("users").select("user_id", count="exact").eq("verification_status","VERIFIED").eq("biolock_status","PASS").execute()
            out["total_verified_pass"] = a.count or 0
        except Exception:
            pass
        try:
            b = supabase.table("users").select("user_id", count="exact").eq("verification_status","VERIFIED").eq("biolock_status","PASS").gt("last_active", cutoff_iso).execute()
            out["active_cutoff"] = b.count or 0
        except Exception:
            pass
        try:
            c = supabase.table("users").select("user_id", count="exact").eq("verification_status","VERIFIED").eq("biolock_status","PASS").gt("last_active", cutoff_iso).gt("visibility_credits",0).execute()
            out["active_and_vis_gt0"] = c.count or 0
        except Exception:
            pass
        try:
            sw = supabase.table("swipes").select("target_id").eq("swiper_id", uid).gte("created_at", start).lt("created_at", end).execute()
            swiped = set(safe_int(x.get("target_id")) for x in (sw.data or []) if x.get("target_id") is not None)
            cand = supabase.table("users").select("user_id").eq("verification_status","VERIFIED").eq("biolock_status","PASS").gt("last_active", cutoff_iso).gt("visibility_credits",0).neq("user_id", uid).limit(200).execute()
            rows = cand.data or []
            out["after_swiped_today_filter"] = len([r for r in rows if safe_int(r.get("user_id")) not in swiped])
        except Exception:
            pass
        return out

    try:
        sw = supabase.table("swipes").select("id", count="exact").eq("swiper_id", uid).gte("created_at", start).lt("created_at", end).execute()
        your_swipes = sw.count or 0
    except Exception:
        your_swipes = 0

    return {"status":"ok","your_swipes_today":your_swipes,"cutoff_5m":count_q(cutoff_5m),"cutoff_1h":count_q(cutoff_1h)}


# =========================
# SWIPE (Alive progression)
# =========================
@app.post("/game/swipe")
async def game_swipe(req: dict):
    """
    Minimal swipe transaction:
      - records swipe (if swipes table exists)
      - +1 AP (swiper)
      - +1 visibility_credits (swiper)
      - -1 visibility_credits (target, floor 0)
      - increments target votes_light/spite
      - increments swipe count for today (returned)
      - energy burn after 20 swipes/day (simple)
    """
    init_data = req.get("initData") or "debug_mode"
    direction = (req.get("direction") or "").upper()
    target_id = safe_int(req.get("target_id"))

    if direction not in ("LIGHT","SPITE"):
        return {"status":"error","msg":"BAD_DIRECTION"}

    u = validate_auth(init_data)
    uid = safe_int(R.get("id"))

    # No supabase -> simulated
    if not supabase:
        return {"status":"ok","new_ap":1,"new_energy":30,"new_visibility_credits":10,"swipes_today":1,"note":"SUPABASE_DISABLED"}

    today = day_key_utc()
    start = iso(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC))
    end = iso((datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)))

    try:
        # count swipes today (if swipes table exists)
        swipes_today = 0
        try:
            swc = supabase.table("swipes").select("id", count="exact").eq("swiper_id", uid).gte("created_at", start).lt("created_at", end).execute()
            swipes_today = swc.count or 0
        except Exception:
            swipes_today = 0

        # energy burn after 20
        burn_energy = 1 if swipes_today >= 20 else 0

        # get swiper
        me = supabase.table("users").select("*").eq("user_id", uid).execute()
        if not me.data:
            return {"status":"error","msg":"NO_USER"}
        me_row = me.data[0]

        # gate checks
        if not me_row.get("bio_lock_url"):
            return {"status":"error","msg":"BIOLOCK_REQUIRED"}
        if me_row.get("verification_status") not in (None, "VERIFIED"):
            return {"status":"error","msg":"NOT_VERIFIED"}

        energy = safe_int(me_row.get("energy"), 30)
        if burn_energy and energy <= 0:
            return {"status":"error","msg":"NO_ENERGY"}

        # update swiper
        new_ap = safe_int(me_row.get("ap"), 0) + 1
        new_vis = safe_int(me_row.get("visibility_credits"), 0) + 1
        new_energy = energy - burn_energy

        supabase.table("users").update({
            "ap": new_ap,
            "visibility_credits": new_vis,
            "energy": new_energy,
            "last_active": iso(now_utc())
        }).eq("user_id", uid).execute()

        # update target votes + vis
        try:
            tgt = supabase.table("users").select("*").eq("user_id", target_id).execute()
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

        # record swipe (best effort)
        try:
            supabase.table("swipes").insert({
                "swiper_id": uid,
                "target_id": target_id,
                "direction": direction,
                "created_at": iso(now_utc())
            }).execute()
        except Exception:
            pass

        # recompute swipes today (best effort)
        try:
            swc2 = supabase.table("swipes").select("id", count="exact").eq("swiper_id", uid).gte("created_at", start).lt("created_at", end).execute()
            swipes_today = swc2.count or (swipes_today + 1)
        except Exception:
            swipes_today = swipes_today + 1

        return {
            "status":"ok",
            "new_ap": new_ap,
            "new_energy": new_energy,
            "new_visibility_credits": new_vis,
            "swipes_today": swipes_today
        }
    except Exception as e:
        return {"status":"error","msg":str(e)}
