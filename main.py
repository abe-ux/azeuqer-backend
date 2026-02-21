"""
AZEUQER Backend (V40) — Sovereign Code Architect Edition
-------------------------------------------------------
FastAPI + Supabase backend for Telegram WebApp game.

Key features (server-authoritative):
- Telegram initData verification (DEV override supported)
- Email registration + duplicate handling + tg_user_id binding
- Pillars logic: first 23 registrants become THE PILLARS (permanent)
- Server-side lazy energy (+1 per 5 min, max 30)
- Scan/swipe loop (with 30 fake targets; never appear in rankings)
- Boss trigger: guaranteed once per 20-scan cycle during scans 11–20
- Inventory + equip + stats allocation (Light/Spite => points)
- Monthly faction recalculation (EUPHORIA vs DISSONANCE)
- Clear errors + bootstrap SQL endpoint when schema is missing

ENV VARS (Render/Supabase):
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY   (server-side key; DO NOT expose to client)
- TG_BOT_TOKEN                (Telegram bot token to verify initData)
- DEV_ALLOW_ANON=1            (allow dev header override when no initData)
- ALLOWED_ORIGINS=*           (or comma-separated list for CORS)
- PILLARS_LIMIT=23            (default 23)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from supabase import create_client, Client as SupabaseClient


# ----------------------------
# Config
# ----------------------------

UTC = timezone.utc

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()

DEV_ALLOW_ANON = os.getenv("DEV_ALLOW_ANON", "0").strip() == "1"
PILLARS_LIMIT = int(os.getenv("PILLARS_LIMIT", "23"))

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").strip()
ORIGINS = ["*"] if ALLOWED_ORIGINS == "*" else [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]

ENERGY_MAX = 30
ENERGY_REGEN_SECS = 5 * 60  # +1 every 5 minutes
FREE_SCANS_PER_DAY = 20
BOSS_WINDOW_START = 11
BOSS_WINDOW_END = 20
BOSS_HP_BASE = 60

# 30 fake users (never stored in DB; never included in rankings)
FAKE_TARGETS: List[Dict[str, Any]] = [
    {
        "target_id": -1000 - i,
        "display_name": nm,
        "bio": meta,
        # deterministic placeholder image: the front-end can display these URLs
        "image_url": f"https://picsum.photos/seed/azeuqer_fake_{i}/800/900",
    }
    for i, (nm, meta) in enumerate(
        [
            ("KAI A.", "Ghost Protocol"),
            ("MIRA B.", "Neon Drift"),
            ("NOVA C.", "Signal Runner"),
            ("ZANE D.", "Chrome Vow"),
            ("LUNA E.", "Skyline Echo"),
            ("RYU F.", "Void Relay"),
            ("ARIA G.", "Pulse Artist"),
            ("JAX H.", "Afterglow"),
            ("SAGE I.", "Static Bloom"),
            ("ROXI J.", "Holo Bite"),
            ("VEGA K.", "Night Circuit"),
            ("NYX L.", "Dissonant Muse"),
            ("ELIO M.", "Euphoria Proxy"),
            ("YARA N.", "Prism Hacker"),
            ("KODA O.", "Street Oracle"),
            ("FAYE P.", "Sponsor Shade"),
            ("VIO Q.", "Crystal Panic"),
            ("LEIF R.", "Quiet Riot"),
            ("MOSS S.", "Glitch Garden"),
            ("SKYE T.", "Orbit Thief"),
            ("ZURI U.", "Mirage Lock"),
            ("NOEL V.", "Wavelength"),
            ("REMI W.", "Signal Noir"),
            ("IVY X.", "Aftershock"),
            ("OMNI Y.", "Static Crown"),
            ("AZRA Z.", "Void Bloom"),
            ("SOL A.", "Sunken Neon"),
            ("ECHO B.", "Backfeed"),
            ("RIVEN C.", "Spectral"),
            ("KIMI D.", "Soft Sabotage"),
        ]
    )
]

# in-memory rate limiter (best-effort; stateless platforms may reset)
RATE_LIMIT: Dict[str, List[float]] = {}
RATE_LIMIT_WINDOW_SECS = 10.0
RATE_LIMIT_MAX_REQS = 25


# ----------------------------
# Supabase client
# ----------------------------

def _require_env() -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

def sb() -> SupabaseClient:
    _require_env()
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def _sb_table_exists(client: SupabaseClient, table: str) -> bool:
    """
    Supabase Python client doesn't expose a direct 'table exists' API.
    We'll attempt a very small select and treat 404 / relation errors as missing.
    """
    try:
        client.table(table).select("*").limit(1).execute()
        return True
    except Exception:
        return False


# ----------------------------
# Telegram initData verification
# ----------------------------

def _parse_init_data(init_data: str) -> Dict[str, str]:
    # initData is querystring-like: key=value&key=value
    out: Dict[str, str] = {}
    for part in (init_data or "").split("&"):
        if not part:
            continue
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k] = v
    return out

def _tg_check_hash(init_data: str, bot_token: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Telegram WebApp auth check:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    """
    if not init_data or not bot_token:
        return False, None

    parsed = _parse_init_data(init_data)
    recv_hash = parsed.get("hash")
    if not recv_hash:
        return False, None

    data_check = []
    for k in sorted(parsed.keys()):
        if k == "hash":
            continue
        data_check.append(f"{k}={parsed[k]}")
    data_check_string = "\n".join(data_check)

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    ok = hmac.compare_digest(calc_hash, recv_hash)

    user_json = parsed.get("user")
    user_obj = None
    if user_json:
        try:
            # Telegram encodes JSON with URL encoding; FastAPI gives it decoded in many cases,
            # but we defensively unquote %7B...%7D if still encoded.
            from urllib.parse import unquote
            user_obj = json.loads(unquote(user_json))
        except Exception:
            user_obj = None

    return ok, user_obj

def _get_tg_user_id_from_request(req: Request) -> int:
    """
    Preferred:
      - X-Telegram-InitData: initData string (frontend should send it)
    Dev override:
      - X-Dev-Tg-User-Id: integer, only if DEV_ALLOW_ANON=1
    """
    init_data = req.headers.get("X-Telegram-InitData", "").strip()
    if init_data:
        ok, user_obj = _tg_check_hash(init_data, TG_BOT_TOKEN)
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid Telegram initData signature.")
        if not user_obj or "id" not in user_obj:
            raise HTTPException(status_code=401, detail="Telegram initData valid, but user payload missing.")
        return int(user_obj["id"])

    # dev path
    if DEV_ALLOW_ANON:
        dev_id = req.headers.get("X-Dev-Tg-User-Id", "").strip()
        if dev_id:
            try:
                return int(dev_id)
            except Exception:
                raise HTTPException(status_code=400, detail="DEV user id must be an integer.")
        raise HTTPException(
            status_code=401,
            detail="Missing Telegram initData. Enter a DEV Telegram user id (and ensure DEV_ALLOW_ANON=1).",
        )

    raise HTTPException(status_code=401, detail="Missing Telegram initData.")


# ----------------------------
# Helpers: time, day/month keys, rate limit
# ----------------------------

def _now() -> datetime:
    return datetime.now(tz=UTC)

def _day_key(dt: Optional[datetime] = None) -> str:
    d = dt or _now()
    return d.strftime("%Y-%m-%d")

def _month_key(dt: Optional[datetime] = None) -> str:
    d = dt or _now()
    return d.strftime("%Y-%m")

def _rate_limit(req: Request, tg_user_id: int) -> None:
    # best effort, keyed by tg + ip
    ip = req.client.host if req.client else "unknown"
    key = f"{tg_user_id}:{ip}"
    t = time.time()
    bucket = RATE_LIMIT.get(key, [])
    # drop old
    bucket = [x for x in bucket if (t - x) <= RATE_LIMIT_WINDOW_SECS]
    if len(bucket) >= RATE_LIMIT_MAX_REQS:
        raise HTTPException(status_code=429, detail="Rate limit hit. Slow down.")
    bucket.append(t)
    RATE_LIMIT[key] = bucket


# ----------------------------
# Schema bootstrap (returned to user via endpoint)
# ----------------------------

BOOTSTRAP_SQL = r"""
-- AZEUQER Schema (V40) — run this in Supabase SQL editor
-- NOTE: If you already have public.azeuqer_users from earlier versions, this will extend it safely.

create table if not exists public.azeuqer_users (
  id bigserial primary key,
  telegram_user_id bigint not null unique,
  email text not null unique,
  is_pillar boolean not null default false,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

-- server-authoritative per-user state
create table if not exists public.user_state (
  telegram_user_id bigint primary key references public.azeuqer_users(telegram_user_id) on delete cascade,
  day_key text not null default to_char(now(), 'YYYY-MM-DD'),
  month_key text not null default to_char(now(), 'YYYY-MM'),

  scans_today int not null default 0,
  light_today int not null default 0,
  spite_today int not null default 0,

  light_month int not null default 0,
  spite_month int not null default 0,

  energy int not null default 30,
  last_energy_ts timestamptz not null default now(),

  -- faction shifts monthly based on month totals
  faction text not null default 'UNSORTED',

  -- stat points
  stat_str int not null default 0,
  stat_agi int not null default 0,
  stat_int int not null default 0,
  stat_vit int not null default 0,

  -- lifetime
  kills_lifetime int not null default 0,

  -- boss cycle tracking
  boss_cycle_idx int not null default 0,
  boss_spawned_cycle_idx int not null default -1,
  boss_hp int not null default 60,
  player_hp int not null default 40,
  player_hp_max int not null default 40
);

create table if not exists public.swipes (
  id bigserial primary key,
  telegram_user_id bigint not null references public.azeuqer_users(telegram_user_id) on delete cascade,
  target_id bigint not null,
  direction text not null check (direction in ('LIGHT','SPITE')),
  created_at timestamptz not null default now()
);

-- Items + inventory (atomic, server authoritative)
create table if not exists public.items (
  item_code text primary key,
  name text not null,
  slot text not null, -- e.g. 'HEAD','CHEST','LEGS','WEAPON','ACCESSORY'
  rarity text not null default 'COMMON',
  req_str int not null default 0,
  req_agi int not null default 0,
  req_int int not null default 0,
  req_vit int not null default 0,
  bonus_str int not null default 0,
  bonus_agi int not null default 0,
  bonus_int int not null default 0,
  bonus_vit int not null default 0,
  bonus_hp int not null default 0,
  bonus_dmg int not null default 0
);

create table if not exists public.inventory (
  id bigserial primary key,
  telegram_user_id bigint not null references public.azeuqer_users(telegram_user_id) on delete cascade,
  item_code text not null references public.items(item_code),
  qty int not null default 1,
  equipped_slot text null,
  created_at timestamptz not null default now()
);

-- simple server_state store
create table if not exists public.server_state (
  key text primary key,
  value jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

-- seed a couple items (optional)
insert into public.items(item_code,name,slot,rarity,bonus_hp,bonus_dmg) values
  ('STICKER_PACK','Sticker Pack','ACCESSORY','COMMON',0,0),
  ('NEON_HOODIE','Neon Hoodie','CHEST','RARE',10,0),
  ('VOID_RUNNER_JACKET','Void Runner Jacket','CHEST','EPIC',20,0),
  ('PRISM_BOOTS','Prism Boots','LEGS','EPIC',0,1)
on conflict (item_code) do nothing;
"""


# ----------------------------
# Pydantic models
# ----------------------------

class RegisterPayload(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)

class SwipePayload(BaseModel):
    direction: str = Field(..., pattern="^(LIGHT|SPITE)$")
    target_id: int

class AllocateStatsPayload(BaseModel):
    add_str: int = 0
    add_agi: int = 0
    add_int: int = 0
    add_vit: int = 0

class EquipPayload(BaseModel):
    inventory_id: int
    slot: str = Field(..., min_length=2, max_length=20)

class BossAttackPayload(BaseModel):
    action: str = Field(..., pattern="^(ATTACK|HEAL)$")


# ----------------------------
# Core game math
# ----------------------------

def _lazy_regen_energy(energy: int, last_ts: datetime, now: datetime) -> Tuple[int, datetime]:
    if energy >= ENERGY_MAX:
        return ENERGY_MAX, last_ts
    elapsed = (now - last_ts).total_seconds()
    gained = int(elapsed // ENERGY_REGEN_SECS)
    if gained <= 0:
        return energy, last_ts
    new_energy = min(ENERGY_MAX, energy + gained)
    new_ts = last_ts + timedelta(seconds=gained * ENERGY_REGEN_SECS)
    return new_energy, new_ts

def _ensure_day_month_rollover(state: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    dkey = _day_key(now)
    mkey = _month_key(now)

    # Day rollover: reset today counters + boss cycle counters remain (but scans_today resets)
    if state.get("day_key") != dkey:
        state["day_key"] = dkey
        state["scans_today"] = 0
        state["light_today"] = 0
        state["spite_today"] = 0
        # reset boss cycle daily
        state["boss_cycle_idx"] = 0
        state["boss_spawned_cycle_idx"] = -1
        state["boss_hp"] = BOSS_HP_BASE
        # keep HP within bounds
        state["player_hp"] = min(int(state.get("player_hp", 40)), int(state.get("player_hp_max", 40)))

    # Month rollover: compute new faction from previous month totals (or keep if first)
    if state.get("month_key") != mkey:
        # faction based on prior month counts
        lm = int(state.get("light_month", 0))
        sm = int(state.get("spite_month", 0))
        if lm == 0 and sm == 0:
            faction = "UNSORTED"
        elif lm > sm:
            faction = "EUPHORIA"
        elif sm > lm:
            faction = "DISSONANCE"
        else:
            # tie -> deterministic pick by day
            faction = "EUPHORIA" if (hash(dkey) % 2 == 0) else "DISSONANCE"

        state["faction"] = faction
        state["month_key"] = mkey
        # reset month counters
        state["light_month"] = 0
        state["spite_month"] = 0

    return state

def _pillar_boost(is_pillar: bool) -> Dict[str, float]:
    return {"discount": 0.23, "stat_boost": 0.23} if is_pillar else {"discount": 0.0, "stat_boost": 0.0}

def _available_points(state: Dict[str, Any]) -> int:
    # points earned = light_month + spite_month (current month) + light_today + spite_today (redundant but included)
    # we treat month totals as source of points; day contributes to month too in our updates.
    earned = int(state.get("light_month", 0)) + int(state.get("spite_month", 0))
    spent = int(state.get("stat_str", 0)) + int(state.get("stat_agi", 0)) + int(state.get("stat_int", 0)) + int(state.get("stat_vit", 0))
    return max(0, earned - spent)

def _derive_hp_max(state: Dict[str, Any], is_pillar: bool) -> int:
    vit = int(state.get("stat_vit", 0))
    base = 30 + vit * 6
    # pillar boost affects stats => we treat it as stat multiplier for combat
    if is_pillar:
        base = int(round(base * (1.0 + 0.23)))
    return max(30, base)

def _calc_player_damage(state: Dict[str, Any], is_pillar: bool) -> int:
    s = int(state.get("stat_str", 0))
    i = int(state.get("stat_int", 0))
    base = int(s * 0.6 + i * 0.4)
    if is_pillar:
        base = int(round(base * (1.0 + 0.23)))
    # small randomness using hash of time
    r = int(time.time() * 1000) % 4
    return max(1, base + r)

def _calc_boss_damage(state: Dict[str, Any], scans_today: int) -> int:
    lvl = max(1, scans_today // 10)
    base = 2 + (scans_today // 6) + lvl
    r = int(time.time() * 1000) % 3
    return max(1, base + r)

def _should_spawn_boss(state: Dict[str, Any]) -> bool:
    scans_today = int(state.get("scans_today", 0))
    cycle_idx = scans_today // FREE_SCANS_PER_DAY  # 0 for scans 0-19, 1 for 20-39, etc
    pos = scans_today % FREE_SCANS_PER_DAY

    state["boss_cycle_idx"] = cycle_idx

    spawned_cycle = int(state.get("boss_spawned_cycle_idx", -1))
    if spawned_cycle == cycle_idx:
        return False

    # Boss appears once per cycle during window 11-20 (pos 10-19 if 1-indexed)
    # scans_today counts AFTER swipe; so pos is 1..20 in meaning:
    scan_num_in_cycle = pos if pos != 0 else FREE_SCANS_PER_DAY
    if BOSS_WINDOW_START <= scan_num_in_cycle <= BOSS_WINDOW_END:
        # guarantee by end of window
        if scan_num_in_cycle == BOSS_WINDOW_END:
            return True
        # otherwise probabilistic
        # tuned so it "feels" frequent, but still within window
        chance = 0.35
        return (hash(f"{state.get('day_key')}:{scans_today}:{state.get('telegram_user_id','')}") % 1000) < int(chance * 1000)
    return False


# ----------------------------
# DB access layer
# ----------------------------

def _get_user(client: SupabaseClient, tg_user_id: int) -> Optional[Dict[str, Any]]:
    try:
        res = client.table("azeuqer_users").select("*").eq("telegram_user_id", tg_user_id).limit(1).execute()
        data = res.data or []
        return data[0] if data else None
    except Exception:
        return None

def _count_users(client: SupabaseClient) -> int:
    # count(*) using select with head and count is not always supported by client consistently;
    # simplest: fetch ids (small project) OR use server_state counter.
    try:
        res = client.table("azeuqer_users").select("telegram_user_id").execute()
        return len(res.data or [])
    except Exception:
        return 0

def _upsert_user(client: SupabaseClient, tg_user_id: int, email: str, make_pillar: bool) -> Dict[str, Any]:
    now = _now().isoformat()
    payload = {
        "telegram_user_id": tg_user_id,
        "email": email,
        "last_seen_at": now,
    }
    # schema may or may not have is_pillar; try gracefully
    if make_pillar:
        payload["is_pillar"] = True
    try:
        res = client.table("azeuqer_users").upsert(payload, on_conflict="telegram_user_id").execute()
        if res.data:
            return res.data[0]
    except Exception as e:
        # fallback for older schema using is_founder instead of is_pillar
        payload.pop("is_pillar", None)
        if make_pillar:
            payload["is_founder"] = True
        res = client.table("azeuqer_users").upsert(payload, on_conflict="telegram_user_id").execute()
        if res.data:
            return res.data[0]
        raise e
    return _get_user(client, tg_user_id) or payload

def _ensure_user_state(client: SupabaseClient, tg_user_id: int) -> Dict[str, Any]:
    now = _now()
    # If table missing, raise a clear message
    if not _sb_table_exists(client, "user_state"):
        raise HTTPException(status_code=500, detail="DB schema missing: user_state table not found. Call /api/admin/schema and run it in Supabase.")

    res = client.table("user_state").select("*").eq("telegram_user_id", tg_user_id).limit(1).execute()
    data = res.data or []
    if data:
        st = data[0]
    else:
        st = {
            "telegram_user_id": tg_user_id,
            "day_key": _day_key(now),
            "month_key": _month_key(now),
            "energy": ENERGY_MAX,
            "last_energy_ts": now.isoformat(),
            "boss_hp": BOSS_HP_BASE,
            "player_hp": 40,
            "player_hp_max": 40,
        }
        ins = client.table("user_state").insert(st).execute()
        st = (ins.data or [st])[0]

    # normalize timestamps
    try:
        if isinstance(st.get("last_energy_ts"), str):
            st["last_energy_ts"] = datetime.fromisoformat(st["last_energy_ts"].replace("Z", "+00:00"))
    except Exception:
        st["last_energy_ts"] = now

    return st

def _save_user_state(client: SupabaseClient, st: Dict[str, Any]) -> Dict[str, Any]:
    now = _now()
    # serialize timestamp
    if isinstance(st.get("last_energy_ts"), datetime):
        st["last_energy_ts"] = st["last_energy_ts"].isoformat()

    # keep only known columns (avoid schema mismatch). This prevents Supabase from erroring on extra keys.
    allowed = {
        "telegram_user_id", "day_key", "month_key",
        "scans_today", "light_today", "spite_today",
        "light_month", "spite_month",
        "energy", "last_energy_ts",
        "faction",
        "stat_str", "stat_agi", "stat_int", "stat_vit",
        "kills_lifetime",
        "boss_cycle_idx", "boss_spawned_cycle_idx",
        "boss_hp", "player_hp", "player_hp_max"
    }
    st2 = {k: v for k, v in st.items() if k in allowed}
    res = client.table("user_state").upsert(st2, on_conflict="telegram_user_id").execute()
    return (res.data or [st2])[0]

def _get_inventory(client: SupabaseClient, tg_user_id: int) -> List[Dict[str, Any]]:
    if not _sb_table_exists(client, "inventory"):
        return []
    res = client.table("inventory").select("*, items(*)").eq("telegram_user_id", tg_user_id).order("id", desc=True).execute()
    return res.data or []

def _equip_item(client: SupabaseClient, tg_user_id: int, inventory_id: int, slot: str) -> None:
    if not _sb_table_exists(client, "inventory"):
        raise HTTPException(status_code=500, detail="DB schema missing: inventory table not found.")
    # Ensure the item belongs to the user
    res = client.table("inventory").select("*").eq("id", inventory_id).eq("telegram_user_id", tg_user_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Inventory item not found.")
    # Unequip any currently equipped in that slot
    client.table("inventory").update({"equipped_slot": None}).eq("telegram_user_id", tg_user_id).eq("equipped_slot", slot).execute()
    # Equip this one
    client.table("inventory").update({"equipped_slot": slot}).eq("id", inventory_id).execute()

def _award_loot(client: SupabaseClient, tg_user_id: int) -> Dict[str, Any]:
    # pick from items table if exists; else return virtual loot without storing
    if not _sb_table_exists(client, "items") or not _sb_table_exists(client, "inventory"):
        return {"rarity": "COMMON", "name": "Sticker Pack", "stored": False}

    # rarity roll
    roll = (hash(f"{tg_user_id}:{time.time_ns()}") % 1000) / 1000.0
    if roll < 0.60:
        rarity = "COMMON"
    elif roll < 0.88:
        rarity = "RARE"
    elif roll < 0.98:
        rarity = "EPIC"
    else:
        rarity = "MYTHIC"

    items = sb().table("items").select("*").eq("rarity", rarity).limit(50).execute().data or []
    if not items:
        # fallback any
        items = sb().table("items").select("*").limit(50).execute().data or []

    if not items:
        return {"rarity": rarity, "name": f"{rarity} CRATE", "stored": False}

    it = items[hash(f"{tg_user_id}:{rarity}:{time.time_ns()}") % len(items)]
    inv_row = {
        "telegram_user_id": tg_user_id,
        "item_code": it["item_code"],
        "qty": 1,
        "equipped_slot": None,
    }
    sb().table("inventory").insert(inv_row).execute()
    return {"rarity": it.get("rarity", rarity), "name": it.get("name", it["item_code"]), "stored": True, "item_code": it["item_code"]}


# ----------------------------
# FastAPI app
# ----------------------------

app = FastAPI(title="AZEUQER Backend V40", version="40.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "version": app.version}


@app.get("/api/admin/schema")
def admin_schema():
    # return SQL so user can paste into Supabase SQL editor
    return {"sql": BOOTSTRAP_SQL}


@app.post("/api/register")
def register(payload: RegisterPayload, req: Request):
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)

    email = payload.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Invalid email format.")

    client = sb()

    # Duplicate email check (bind email to first tg_user_id permanently)
    existing_email = client.table("azeuqer_users").select("*").eq("email", email).limit(1).execute().data or []
    if existing_email and int(existing_email[0].get("telegram_user_id")) != tg_user_id:
        raise HTTPException(status_code=409, detail="Email already registered to a different Telegram account.")

    # Pillars: first 23 registrants (based on number of rows BEFORE insert)
    # NOTE: race conditions are possible; acceptable for prototype. For strictness, use Postgres function/transaction.
    user = _get_user(client, tg_user_id)
    if user:
        make_pillar = bool(user.get("is_pillar") or user.get("is_founder"))
    else:
        current_count = _count_users(client)
        make_pillar = current_count < PILLARS_LIMIT

    user = _upsert_user(client, tg_user_id, email, make_pillar)

    # ensure state exists
    st = _ensure_user_state(client, tg_user_id)
    # bump last_seen
    client.table("azeuqer_users").update({"last_seen_at": _now().isoformat()}).eq("telegram_user_id", tg_user_id).execute()

    # compute energy on read
    now = _now()
    st = _ensure_day_month_rollover(st, now)
    e, ts = _lazy_regen_energy(int(st.get("energy", ENERGY_MAX)), st.get("last_energy_ts", now), now)
    st["energy"] = e
    st["last_energy_ts"] = ts
    # hp max recompute
    is_pillar = bool(user.get("is_pillar") or user.get("is_founder"))
    st["player_hp_max"] = _derive_hp_max(st, is_pillar)
    st["player_hp"] = min(int(st.get("player_hp", 40)), int(st["player_hp_max"]))
    _save_user_state(client, st)

    return {
        "ok": True,
        "user": {
            "telegram_user_id": tg_user_id,
            "email": user.get("email"),
            "is_pillar": is_pillar,
            "pillars": _pillar_boost(is_pillar),
            "created_at": user.get("created_at"),
        },
        "state": _public_state(st, is_pillar),
    }


def _public_state(st: Dict[str, Any], is_pillar: bool) -> Dict[str, Any]:
    return {
        "day_key": st.get("day_key"),
        "month_key": st.get("month_key"),
        "energy": int(st.get("energy", ENERGY_MAX)),
        "scans_today": int(st.get("scans_today", 0)),
        "light_today": int(st.get("light_today", 0)),
        "spite_today": int(st.get("spite_today", 0)),
        "light_month": int(st.get("light_month", 0)),
        "spite_month": int(st.get("spite_month", 0)),
        "faction": st.get("faction", "UNSORTED"),
        "stats": {
            "STR": int(st.get("stat_str", 0)),
            "AGI": int(st.get("stat_agi", 0)),
            "INT": int(st.get("stat_int", 0)),
            "VIT": int(st.get("stat_vit", 0)),
            "points_available": _available_points(st),
            "pillar_stat_boost": 0.23 if is_pillar else 0.0,
        },
        "hp": {
            "player_hp": int(st.get("player_hp", 40)),
            "player_hp_max": int(st.get("player_hp_max", 40)),
            "boss_hp": int(st.get("boss_hp", BOSS_HP_BASE)),
        },
        "boss": {
            "boss_cycle_idx": int(st.get("boss_cycle_idx", 0)),
            "boss_spawned_cycle_idx": int(st.get("boss_spawned_cycle_idx", -1)),
        },
        "pillars": _pillar_boost(is_pillar),
    }


@app.get("/api/me")
def me(req: Request):
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)

    client = sb()
    user = _get_user(client, tg_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered. Call /api/register first.")

    is_pillar = bool(user.get("is_pillar") or user.get("is_founder"))

    st = _ensure_user_state(client, tg_user_id)
    now = _now()
    st = _ensure_day_month_rollover(st, now)

    # energy on read
    e, ts = _lazy_regen_energy(int(st.get("energy", ENERGY_MAX)), st.get("last_energy_ts", now), now)
    st["energy"] = e
    st["last_energy_ts"] = ts

    # hp max
    st["player_hp_max"] = _derive_hp_max(st, is_pillar)
    st["player_hp"] = min(int(st.get("player_hp", 40)), int(st["player_hp_max"]))

    st = _save_user_state(client, st)

    inv = _get_inventory(client, tg_user_id)

    return {
        "ok": True,
        "user": {
            "telegram_user_id": tg_user_id,
            "email": user.get("email"),
            "is_pillar": is_pillar,
        },
        "state": _public_state(st, is_pillar),
        "inventory": inv,
        "fake_targets_count": len(FAKE_TARGETS),
    }


@app.get("/api/scan/next")
def scan_next(req: Request):
    """
    Returns the next target for the swiper.
    New registered members are included immediately (real users),
    but fake users also always exist (30).
    """
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)
    client = sb()

    user = _get_user(client, tg_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered. Call /api/register first.")
    is_pillar = bool(user.get("is_pillar") or user.get("is_founder"))

    st = _ensure_user_state(client, tg_user_id)
    now = _now()
    st = _ensure_day_month_rollover(st, now)
    e, ts = _lazy_regen_energy(int(st.get("energy", ENERGY_MAX)), st.get("last_energy_ts", now), now)
    st["energy"], st["last_energy_ts"] = e, ts
    st = _save_user_state(client, st)

    target = _pick_target(client, tg_user_id)
    mode = "INITIATION" if int(st.get("scans_today", 0)) < 10 else "SORTED"

    return {
        "ok": True,
        "mode": mode,
        "target": target,
        "state": _public_state(st, is_pillar),
    }


def _pick_target(client: SupabaseClient, tg_user_id: int) -> Dict[str, Any]:
    """
    Returns a mix of:
      - real users (azeuqer_users) excluding self
      - fake users (FAKE_TARGETS)
    """
    # deterministically alternate fake/real based on time
    t = int(time.time())  # seconds
    use_fake = (t % 2 == 0)

    # If no other real users, always fake
    real = client.table("azeuqer_users").select("telegram_user_id,email,is_pillar,is_founder").neq("telegram_user_id", tg_user_id).limit(50).execute().data or []
    if not real:
        use_fake = True

    if use_fake:
        idx = hash(f"{tg_user_id}:{t}") % len(FAKE_TARGETS)
        return FAKE_TARGETS[idx]

    # pick a real user
    row = real[hash(f"{tg_user_id}:{t}:real") % len(real)]
    # return minimal public profile; do not leak email (use masked)
    em = row.get("email", "")
    masked = em[:2] + "***@" + em.split("@")[-1] if "@" in em else "user***"
    return {
        "target_id": int(row.get("telegram_user_id")),
        "display_name": masked.upper(),
        "bio": "LIVE SIGNAL",
        "image_url": f"https://picsum.photos/seed/azeuqer_real_{row.get('telegram_user_id')}/800/900",
        "is_real_user": True,
    }


@app.post("/api/scan/swipe")
def scan_swipe(payload: SwipePayload, req: Request):
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)
    client = sb()

    user = _get_user(client, tg_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered. Call /api/register first.")
    is_pillar = bool(user.get("is_pillar") or user.get("is_founder"))

    st = _ensure_user_state(client, tg_user_id)
    now = _now()
    st["telegram_user_id"] = tg_user_id  # for hashing
    st = _ensure_day_month_rollover(st, now)

    # energy on read
    e, ts = _lazy_regen_energy(int(st.get("energy", ENERGY_MAX)), st.get("last_energy_ts", now), now)
    st["energy"], st["last_energy_ts"] = e, ts

    scans_before = int(st.get("scans_today", 0))

    # energy spend after 20 free scans
    if scans_before >= FREE_SCANS_PER_DAY:
        if st["energy"] <= 0:
            raise HTTPException(status_code=402, detail="Not enough Energy.")
        st["energy"] = max(0, int(st["energy"]) - 1)

    # record swipe (if swipes table exists)
    if _sb_table_exists(client, "swipes"):
        try:
            client.table("swipes").insert({
                "telegram_user_id": tg_user_id,
                "target_id": int(payload.target_id),
                "direction": payload.direction,
            }).execute()
        except Exception:
            pass

    # update counts
    st["scans_today"] = scans_before + 1
    if payload.direction == "LIGHT":
        st["light_today"] = int(st.get("light_today", 0)) + 1
        st["light_month"] = int(st.get("light_month", 0)) + 1
    else:
        st["spite_today"] = int(st.get("spite_today", 0)) + 1
        st["spite_month"] = int(st.get("spite_month", 0)) + 1

    # initiation at 10: set faction immediately for this month too (but still shifts monthly)
    if int(st["scans_today"]) == 10 and st.get("faction", "UNSORTED") == "UNSORTED":
        lm = int(st.get("light_month", 0))
        sm = int(st.get("spite_month", 0))
        if lm > sm:
            st["faction"] = "EUPHORIA"
        elif sm > lm:
            st["faction"] = "DISSONANCE"
        else:
            st["faction"] = "EUPHORIA" if (hash(f"{tg_user_id}:{st.get('day_key')}") % 2 == 0) else "DISSONANCE"

    # boss spawn logic
    spawn = _should_spawn_boss(st)
    boss_event = None
    if spawn:
        st["boss_spawned_cycle_idx"] = int(st.get("boss_cycle_idx", 0))
        # reset boss hp for the encounter (scaled lightly by scans)
        st["boss_hp"] = BOSS_HP_BASE + int(st["scans_today"]) * 2
        # ensure player hp max
        st["player_hp_max"] = _derive_hp_max(st, is_pillar)
        st["player_hp"] = min(int(st.get("player_hp", 40)), int(st["player_hp_max"]))
        boss_event = {"spawned": True, "boss_hp": int(st["boss_hp"])}

    # save state
    st = _save_user_state(client, st)

    # next target
    target = _pick_target(client, tg_user_id)
    mode = "INITIATION" if int(st.get("scans_today", 0)) < 10 else "SORTED"

    return {
        "ok": True,
        "mode": mode,
        "boss": boss_event or {"spawned": False},
        "target": target,
        "state": _public_state(st, is_pillar),
    }


@app.post("/api/stats/allocate")
def allocate_stats(payload: AllocateStatsPayload, req: Request):
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)
    client = sb()
    user = _get_user(client, tg_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered.")
    is_pillar = bool(user.get("is_pillar") or user.get("is_founder"))

    st = _ensure_user_state(client, tg_user_id)
    now = _now()
    st = _ensure_day_month_rollover(st, now)

    add_total = int(payload.add_str) + int(payload.add_agi) + int(payload.add_int) + int(payload.add_vit)
    if add_total <= 0:
        raise HTTPException(status_code=400, detail="No stat points provided.")

    avail = _available_points(st)
    if add_total > avail:
        raise HTTPException(status_code=400, detail=f"Not enough points. Available: {avail}")

    st["stat_str"] = int(st.get("stat_str", 0)) + int(payload.add_str)
    st["stat_agi"] = int(st.get("stat_agi", 0)) + int(payload.add_agi)
    st["stat_int"] = int(st.get("stat_int", 0)) + int(payload.add_int)
    st["stat_vit"] = int(st.get("stat_vit", 0)) + int(payload.add_vit)

    st["player_hp_max"] = _derive_hp_max(st, is_pillar)
    st["player_hp"] = min(int(st.get("player_hp", 40)), int(st["player_hp_max"]))

    st = _save_user_state(client, st)

    return {"ok": True, "state": _public_state(st, is_pillar)}


@app.get("/api/inventory")
def inventory(req: Request):
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)
    client = sb()
    user = _get_user(client, tg_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered.")
    inv = _get_inventory(client, tg_user_id)
    return {"ok": True, "inventory": inv}


@app.post("/api/equip")
def equip(payload: EquipPayload, req: Request):
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)
    client = sb()
    user = _get_user(client, tg_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered.")
    _equip_item(client, tg_user_id, payload.inventory_id, payload.slot.upper())
    inv = _get_inventory(client, tg_user_id)
    return {"ok": True, "inventory": inv}


@app.post("/api/boss/action")
def boss_action(payload: BossAttackPayload, req: Request):
    """
    Boss combat is server-authoritative.
    Requires that boss has spawned in current cycle.
    """
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)
    client = sb()
    user = _get_user(client, tg_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not registered.")
    is_pillar = bool(user.get("is_pillar") or user.get("is_founder"))

    st = _ensure_user_state(client, tg_user_id)
    now = _now()
    st = _ensure_day_month_rollover(st, now)

    # must have boss spawned in current cycle
    scans_today = int(st.get("scans_today", 0))
    cycle_idx = scans_today // FREE_SCANS_PER_DAY
    if int(st.get("boss_spawned_cycle_idx", -1)) != cycle_idx:
        raise HTTPException(status_code=409, detail="Boss not active in this cycle.")

    # ensure hp max
    st["player_hp_max"] = _derive_hp_max(st, is_pillar)
    st["player_hp"] = min(int(st.get("player_hp", 40)), int(st["player_hp_max"]))

    boss_hp = int(st.get("boss_hp", BOSS_HP_BASE))
    php = int(st.get("player_hp", 40))
    php_max = int(st.get("player_hp_max", 40))

    if payload.action == "HEAL":
        heal = max(10, int(php_max * 0.45))
        php = min(php_max, php + heal)
        st["player_hp"] = php
        st = _save_user_state(client, st)
        return {"ok": True, "result": {"heal": heal}, "state": _public_state(st, is_pillar)}

    # ATTACK
    dmg = _calc_player_damage(st, is_pillar)
    boss_hp = max(0, boss_hp - dmg)

    if boss_hp <= 0:
        # victory
        st["boss_hp"] = 0
        st["kills_lifetime"] = int(st.get("kills_lifetime", 0)) + 1

        # rewards
        loot = _award_loot(client, tg_user_id)

        # reset boss for next cycle (do not respawn immediately)
        st["boss_hp"] = BOSS_HP_BASE
        st["boss_spawned_cycle_idx"] = int(st.get("boss_spawned_cycle_idx", cycle_idx))  # keep marked

        st = _save_user_state(client, st)
        return {"ok": True, "result": {"victory": True, "dmg": dmg, "loot": loot}, "state": _public_state(st, is_pillar)}

    # boss retaliates
    bd = _calc_boss_damage(st, scans_today)
    php = max(0, php - bd)

    st["boss_hp"] = boss_hp
    st["player_hp"] = php
    st = _save_user_state(client, st)

    if php <= 0:
        return {"ok": True, "result": {"defeat": True, "dmg": dmg, "boss_dmg": bd}, "state": _public_state(st, is_pillar)}

    return {"ok": True, "result": {"dmg": dmg, "boss_dmg": bd}, "state": _public_state(st, is_pillar)}


@app.get("/api/hall")
def hall(req: Request):
    """
    Hall of Fame rankings. Excludes fake users by design (fake users never stored).
    Ranked by kills_lifetime desc, scans_today desc.
    """
    tg_user_id = _get_tg_user_id_from_request(req)
    _rate_limit(req, tg_user_id)
    client = sb()

    if not _sb_table_exists(client, "user_state"):
        raise HTTPException(status_code=500, detail="DB schema missing: user_state table not found.")

    # join in two steps: fetch states, then map users
    states = client.table("user_state").select("telegram_user_id,kills_lifetime,scans_today,faction,stat_str,stat_agi,stat_int,stat_vit").order("kills_lifetime", desc=True).order("scans_today", desc=True).limit(50).execute().data or []
    if not states:
        return {"ok": True, "rankings": []}

    user_ids = [int(s["telegram_user_id"]) for s in states if int(s.get("telegram_user_id", 0)) > 0]
    users = client.table("azeuqer_users").select("telegram_user_id,is_pillar,is_founder").in_("telegram_user_id", user_ids).execute().data or []
    u_map = {int(u["telegram_user_id"]): u for u in users}

    rankings = []
    for i, s in enumerate(states, start=1):
        uid = int(s["telegram_user_id"])
        u = u_map.get(uid, {})
        rankings.append({
            "rank": i,
            "telegram_user_id": uid,
            "display_name": f"#{uid}",
            "is_pillar": bool(u.get("is_pillar") or u.get("is_founder")),
            "kills_lifetime": int(s.get("kills_lifetime", 0)),
            "scans_today": int(s.get("scans_today", 0)),
            "faction": s.get("faction", "UNSORTED"),
            "stats": {
                "STR": int(s.get("stat_str", 0)),
                "AGI": int(s.get("stat_agi", 0)),
                "INT": int(s.get("stat_int", 0)),
                "VIT": int(s.get("stat_vit", 0)),
            }
        })

    return {"ok": True, "rankings": rankings}
