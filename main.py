"""
Azeuqer Backend — main.py (Python 3.9 compatible)
=================================================
This is a FULL, paste-ready FastAPI backend that matches the client endpoints you’re using:

Client calls supported:
- POST /auth/login
- POST /profile/email
- POST /profile/referrals
- POST /game/inventory
- POST /game/feed
- POST /game/swipe

Key features:
- Python 3.9 safe typing (NO `str | None`)
- Telegram initData parsing (works with debug_mode too)
- SQLite persistence (single file DB)
- Lazy energy regen (+1 energy / 5 minutes, max 30)
- 20 free scans/day; scans 21+ cost 1 energy
- Faction logic (EUPHORIA vs DISSONANCE) based on total LIGHT vs SPITE
- Referral capture via Telegram start_param = "ref_<tg_id>" (one-time)
- Email save endpoint with EmailStr validation + one-time 500 AP reward
"""

import os
import json
import hmac
import time
import hashlib
import sqlite3
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import parse_qsl, unquote_plus

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field


# =============================================================================
# CONFIG
# =============================================================================

APP_NAME = "azeuqer-backend"
DB_PATH = os.getenv("AZ_DB_PATH", "azeuqer.sqlite3")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()  # optional but recommended
ALLOW_DEBUG_MODE = os.getenv("AZ_ALLOW_DEBUG_MODE", "1").strip() == "1"

ENERGY_MAX = 30
ENERGY_REGEN_SECONDS = 5 * 60  # 5 minutes
FREE_SCANS_PER_DAY = 20
FACTION_UNLOCK_AT = 10

CORS_ORIGINS = os.getenv("AZ_CORS_ORIGINS", "*").split(",")


# =============================================================================
# DB HELPERS
# =============================================================================

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      tg_id           INTEGER UNIQUE NOT NULL,
      username        TEXT,
      first_name      TEXT,
      last_name       TEXT,
      photo_url       TEXT,
      email           TEXT,
      role            TEXT DEFAULT 'CITIZEN',
      ap              INTEGER DEFAULT 0,
      stars           INTEGER DEFAULT 0,

      energy          INTEGER DEFAULT 30,
      last_energy_ts  INTEGER DEFAULT 0,

      boss_kills      INTEGER DEFAULT 0,

      total_light     INTEGER DEFAULT 0,
      total_spite     INTEGER DEFAULT 0,

      referrer_tg_id  INTEGER,
      referral_lock   INTEGER DEFAULT 0,

      email_rewarded  INTEGER DEFAULT 0,
      created_ts      INTEGER DEFAULT 0,
      updated_ts      INTEGER DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_stats (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      tg_id        INTEGER NOT NULL,
      day_key      TEXT NOT NULL,
      scans        INTEGER DEFAULT 0,
      light        INTEGER DEFAULT 0,
      spite        INTEGER DEFAULT 0,
      boss_spawned INTEGER DEFAULT 0,
      UNIQUE(tg_id, day_key)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS inventory (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      tg_id       INTEGER NOT NULL,
      item_name   TEXT NOT NULL,
      rarity      TEXT NOT NULL,
      item_desc   TEXT,
      created_ts  INTEGER DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS swipes (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      tg_id      INTEGER NOT NULL,
      target_id  INTEGER NOT NULL,
      direction  TEXT NOT NULL,
      created_ts INTEGER DEFAULT 0
    );
    """)

    conn.commit()
    conn.close()


def now_ts() -> int:
    return int(time.time())


def day_key(ts: Optional[int] = None) -> str:
    # YYYY-MM-DD in local server time (fine for MVP)
    import datetime
    dt = datetime.datetime.fromtimestamp(ts or time.time())
    return dt.strftime("%Y-%m-%d")


def get_user_row(conn: sqlite3.Connection, tg_id: int) -> Optional[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    return cur.fetchone()


def ensure_daily_row(conn: sqlite3.Connection, tg_id: int, dk: str) -> sqlite3.Row:
    cur = conn.cursor()
    cur.execute("""
      INSERT OR IGNORE INTO daily_stats (tg_id, day_key, scans, light, spite, boss_spawned)
      VALUES (?, ?, 0, 0, 0, 0)
    """, (tg_id, dk))
    conn.commit()
    cur.execute("SELECT * FROM daily_stats WHERE tg_id = ? AND day_key = ?", (tg_id, dk))
    return cur.fetchone()


def update_user_ts(conn: sqlite3.Connection, tg_id: int) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE users SET updated_ts = ? WHERE tg_id = ?", (now_ts(), tg_id))
    conn.commit()


# =============================================================================
# TELEGRAM initData (parse + optional verify)
# =============================================================================

def parse_init_data(init_data: str) -> Dict[str, str]:
    """
    Telegram initData is a querystring-like string.
    Example keys: query_id, user, auth_date, hash, start_param
    """
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    return parsed


def telegram_verify_init_data(init_data: str, bot_token: str) -> bool:
    """
    Verifies Telegram WebApp initData signature.
    If no bot_token provided, this will never be used.
    """
    data = parse_init_data(init_data)
    received_hash = data.get("hash", "")
    if not received_hash:
        return False

    # Build data-check-string from all fields except hash, sorted
    items = []
    for k in sorted(data.keys()):
        if k == "hash":
            continue
        items.append(f"{k}={data[k]}")
    data_check_string = "\n".join(items)

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    return hmac.compare_digest(computed_hash, received_hash)


def extract_user_from_init_data(init_data: str) -> Tuple[int, str, Optional[str], Optional[str], Optional[str]]:
    """
    Returns: (tg_id, username, first_name, last_name, photo_url)

    Accepts:
    - Real Telegram initData (user field is JSON, URL-encoded)
    - "debug_mode" (creates a stable demo tg_id)
    """
    if init_data == "debug_mode":
        return (12345, "Architect", "Abe", "Roy", "https://picsum.photos/seed/azeuqer_debug/512/512")

    data = parse_init_data(init_data)

    # If bot token exists, verify. If invalid => reject.
    if BOT_TOKEN:
        if not telegram_verify_init_data(init_data, BOT_TOKEN):
            raise HTTPException(status_code=401, detail="Invalid Telegram initData signature.")
    else:
        # No bot token set: allow, but you should set TELEGRAM_BOT_TOKEN in production.
        if not ALLOW_DEBUG_MODE:
            raise HTTPException(status_code=500, detail="Server missing TELEGRAM_BOT_TOKEN.")

    user_raw = data.get("user", "")
    if not user_raw:
        raise HTTPException(status_code=400, detail="initData missing user field.")

    try:
        user_json = json.loads(unquote_plus(user_raw))
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to parse Telegram user JSON.")

    tg_id = int(user_json.get("id"))
    username = user_json.get("username") or f"agent_{tg_id}"
    first_name = user_json.get("first_name")
    last_name = user_json.get("last_name")
    photo_url = user_json.get("photo_url")

    return (tg_id, username, first_name, last_name, photo_url)


def extract_start_param(init_data: str) -> Optional[str]:
    if init_data == "debug_mode":
        return None
    data = parse_init_data(init_data)
    # Some clients pass start_param; if not present, it's just None.
    return data.get("start_param")


# =============================================================================
# GAME LOGIC (energy regen, faction, etc.)
# =============================================================================

def lazy_regen_energy(current_energy: int, last_ts: int) -> Tuple[int, int]:
    """
    Returns (new_energy, new_last_ts) with lazy regen.
    """
    if current_energy >= ENERGY_MAX:
        return (ENERGY_MAX, last_ts or now_ts())

    now = now_ts()
    last = last_ts or now
    elapsed = now - last
    if elapsed < ENERGY_REGEN_SECONDS:
        return (current_energy, last)

    gained = elapsed // ENERGY_REGEN_SECONDS
    if gained <= 0:
        return (current_energy, last)

    new_energy = min(ENERGY_MAX, current_energy + int(gained))
    new_last = last + int(gained) * ENERGY_REGEN_SECONDS
    return (new_energy, new_last)


def compute_faction(total_light: int, total_spite: int) -> str:
    if total_light > total_spite:
        return "EUPHORIA"
    if total_spite > total_light:
        return "DISSONANCE"
    # tie
    return "EUPHORIA" if (hash(f"{total_light}:{total_spite}") % 2 == 0) else "DISSONANCE"


def as_user_payload(row: sqlite3.Row) -> Dict[str, Any]:
    faction = compute_faction(int(row["total_light"]), int(row["total_spite"]))
    return {
        "user_id": row["tg_id"],
        "username": row["username"] or "AGENT",
        "email": row["email"],
        "role": row["role"] or "CITIZEN",

        "ap": int(row["ap"] or 0),
        "stars": int(row["stars"] or 0),

        "energy": int(row["energy"] or ENERGY_MAX),
        "faction": faction,
        "boss_kills": int(row["boss_kills"] or 0),

        "bio_lock_url": row["photo_url"] or "https://picsum.photos/seed/azeuqer_default/512/512"
    }


def ensure_user(conn: sqlite3.Connection, init_data: str) -> sqlite3.Row:
    tg_id, username, first_name, last_name, photo_url = extract_user_from_init_data(init_data)
    ts = now_ts()

    cur = conn.cursor()
    existing = get_user_row(conn, tg_id)
    if existing:
        # Update profile basics (username/photo can change)
        cur.execute("""
          UPDATE users
          SET username = ?, first_name = ?, last_name = ?, photo_url = ?, updated_ts = ?
          WHERE tg_id = ?
        """, (username, first_name, last_name, photo_url, ts, tg_id))
        conn.commit()
        existing = get_user_row(conn, tg_id)
        return existing

    # New user
    last_energy = ts
    cur.execute("""
      INSERT INTO users (
        tg_id, username, first_name, last_name, photo_url,
        ap, stars, energy, last_energy_ts,
        boss_kills, total_light, total_spite,
        created_ts, updated_ts
      )
      VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, 0, 0, 0, ?, ?)
    """, (tg_id, username, first_name, last_name, photo_url, ENERGY_MAX, last_energy, ts, ts))
    conn.commit()

    # Give a small starter inventory (optional vibe)
    seed_items = [
        ("Neon Hoodie", "RARE", "+VIT vibe. Sponsor-ready."),
        ("Void Runner Glasses", "EPIC", "+INT vibe. Particle-ready."),
    ]
    for nm, rar, desc in seed_items:
        cur.execute("""
          INSERT INTO inventory (tg_id, item_name, rarity, item_desc, created_ts)
          VALUES (?, ?, ?, ?, ?)
        """, (tg_id, nm, rar, desc, ts))
    conn.commit()

    return get_user_row(conn, tg_id)


def apply_referral_if_any(conn: sqlite3.Connection, user_row: sqlite3.Row, init_data: str) -> None:
    """
    One-time referral capture:
    start_param must be "ref_<tg_id>"
    - If user has no referrer and referral_lock is 0, set referrer_tg_id
    - Award both sides +100 AP once
    - The referrer also can earn future 5% on AP later (not implemented here)
    """
    start = extract_start_param(init_data)
    if not start:
        return

    if not start.startswith("ref_"):
        return

    try:
        ref_tg_id = int(start.replace("ref_", "").strip())
    except Exception:
        return

    tg_id = int(user_row["tg_id"])
    if ref_tg_id == tg_id:
        return

    # If already locked, do nothing
    if int(user_row["referral_lock"] or 0) == 1:
        return

    # Only set if empty
    if user_row["referrer_tg_id"] is not None:
        # lock anyway so user can’t change it later
        cur = conn.cursor()
        cur.execute("UPDATE users SET referral_lock = 1, updated_ts = ? WHERE tg_id = ?", (now_ts(), tg_id))
        conn.commit()
        return

    # Referrer must exist
    ref_row = get_user_row(conn, ref_tg_id)
    if not ref_row:
        return

    cur = conn.cursor()
    ts = now_ts()

    # Set referrer and lock
    cur.execute("""
      UPDATE users
      SET referrer_tg_id = ?, referral_lock = 1, updated_ts = ?
      WHERE tg_id = ?
    """, (ref_tg_id, ts, tg_id))

    # Award both +100 AP
    cur.execute("UPDATE users SET ap = ap + 100, updated_ts = ? WHERE tg_id = ?", (ts, tg_id))
    cur.execute("UPDATE users SET ap = ap + 100, updated_ts = ? WHERE tg_id = ?", (ts, ref_tg_id))

    conn.commit()


# =============================================================================
# Pydantic Models (Python 3.9 compatible)
# =============================================================================

class InitPayload(BaseModel):
    initData: str = Field(..., description="Telegram WebApp initData string, or 'debug_mode'")


class EmailPayload(BaseModel):
    initData: str = Field(..., description="Telegram WebApp initData string, or 'debug_mode'")
    email: EmailStr


class ReferralsPayload(BaseModel):
    initData: str


class InventoryPayload(BaseModel):
    initData: str


class FeedPayload(BaseModel):
    initData: str
    limit: Optional[int] = 20


class SwipePayload(BaseModel):
    initData: str
    target_id: int
    direction: str  # "LIGHT" or "SPITE"


# =============================================================================
# FastAPI App
# =============================================================================

init_db()

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# ENDPOINTS
# =============================================================================

@app.get("/")
def root():
    return {"ok": True, "name": APP_NAME}


@app.post("/auth/login")
def auth_login(payload: InitPayload):
    conn = db()
    try:
        user = ensure_user(conn, payload.initData)
        apply_referral_if_any(conn, user, payload.initData)

        # Apply lazy energy regen and persist
        cur = conn.cursor()
        e0 = int(user["energy"] or ENERGY_MAX)
        t0 = int(user["last_energy_ts"] or 0)
        e1, t1 = lazy_regen_energy(e0, t0)
        if e1 != e0 or t1 != t0:
            cur.execute("UPDATE users SET energy = ?, last_energy_ts = ?, updated_ts = ? WHERE tg_id = ?",
                        (e1, t1, now_ts(), int(user["tg_id"])))
            conn.commit()
            user = get_user_row(conn, int(user["tg_id"]))

        return {"ok": True, "user": as_user_payload(user)}
    finally:
        conn.close()


@app.post("/profile/email")
def profile_email(payload: EmailPayload):
    conn = db()
    try:
        user = ensure_user(conn, payload.initData)
        tg_id = int(user["tg_id"])
        ts = now_ts()

        # one-time 500 AP reward for setting email
        reward = 0
        if int(user["email_rewarded"] or 0) == 0:
            reward = 500

        cur = conn.cursor()
        cur.execute("""
          UPDATE users
          SET email = ?, ap = ap + ?, email_rewarded = 1, updated_ts = ?
          WHERE tg_id = ?
        """, (str(payload.email), reward, ts, tg_id))
        conn.commit()

        user = get_user_row(conn, tg_id)
        return {"ok": True, "reward_ap": reward, "user": as_user_payload(user)}
    finally:
        conn.close()


@app.post("/profile/referrals")
def profile_referrals(payload: ReferralsPayload):
    conn = db()
    try:
        user = ensure_user(conn, payload.initData)
        tg_id = int(user["tg_id"])

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE referrer_tg_id = ?", (tg_id,))
        c = int(cur.fetchone()["c"])
        return {"ok": True, "count": c}
    finally:
        conn.close()


@app.post("/game/inventory")
def game_inventory(payload: InventoryPayload):
    conn = db()
    try:
        user = ensure_user(conn, payload.initData)
        tg_id = int(user["tg_id"])

        cur = conn.cursor()
        cur.execute("""
          SELECT item_name, rarity, item_desc
          FROM inventory
          WHERE tg_id = ?
          ORDER BY id DESC
          LIMIT 120
        """, (tg_id,))
        items = [{
            "name": r["item_name"],
            "rarity": r["rarity"],
            "desc": r["item_desc"] or ""
        } for r in cur.fetchall()]

        return {"ok": True, "items": items}
    finally:
        conn.close()


@app.post("/game/feed")
def game_feed(payload: FeedPayload):
    conn = db()
    try:
        user = ensure_user(conn, payload.initData)
        my_id = int(user["tg_id"])
        limit = int(payload.limit or 20)
        limit = max(5, min(60, limit))

        cur = conn.cursor()
        cur.execute("""
          SELECT tg_id, username, photo_url, total_light, total_spite
          FROM users
          WHERE tg_id != ?
          ORDER BY updated_ts DESC
          LIMIT ?
        """, (my_id, limit))

        feed = []
        for r in cur.fetchall():
            faction = compute_faction(int(r["total_light"] or 0), int(r["total_spite"] or 0))
            feed.append({
                "user_id": int(r["tg_id"]),
                "username": r["username"] or f"agent_{r['tg_id']}",
                "bio_lock_url": r["photo_url"] or "https://picsum.photos/seed/azeuqer_feed/512/512",
                "faction": faction
            })

        # If feed is empty, create a few demo bots (first run)
        if not feed:
            ts = now_ts()
            demo = [
                (90001, "NOVA", "https://picsum.photos/seed/azeuqer_nova/512/512"),
                (90002, "KAI", "https://picsum.photos/seed/azeuqer_kai/512/512"),
                (90003, "NYX", "https://picsum.photos/seed/azeuqer_nyx/512/512"),
            ]
            for tg_id, uname, purl in demo:
                cur.execute("""
                  INSERT OR IGNORE INTO users (
                    tg_id, username, photo_url, ap, stars, energy, last_energy_ts,
                    boss_kills, total_light, total_spite, created_ts, updated_ts
                  )
                  VALUES (?, ?, ?, 0, 0, ?, ?, 0, 0, 0, ?, ?)
                """, (tg_id, uname, purl, ENERGY_MAX, ts, ts, ts))
            conn.commit()

            cur.execute("""
              SELECT tg_id, username, photo_url, total_light, total_spite
              FROM users
              WHERE tg_id != ?
              ORDER BY updated_ts DESC
              LIMIT ?
            """, (my_id, limit))
            feed = []
            for r in cur.fetchall():
                faction = compute_faction(int(r["total_light"] or 0), int(r["total_spite"] or 0))
                feed.append({
                    "user_id": int(r["tg_id"]),
                    "username": r["username"] or f"agent_{r['tg_id']}",
                    "bio_lock_url": r["photo_url"] or "https://picsum.photos/seed/azeuqer_feed/512/512",
                    "faction": faction
                })

        return {"ok": True, "feed": feed}
    finally:
        conn.close()


@app.post("/game/swipe")
def game_swipe(payload: SwipePayload):
    conn = db()
    try:
        user = ensure_user(conn, payload.initData)
        tg_id = int(user["tg_id"])
        dk = day_key()
        direction = (payload.direction or "").upper().strip()
        if direction not in ("LIGHT", "SPITE"):
            raise HTTPException(status_code=400, detail="direction must be LIGHT or SPITE")

        # Lazy regen energy first
        cur = conn.cursor()
        e0 = int(user["energy"] or ENERGY_MAX)
        t0 = int(user["last_energy_ts"] or 0)
        e1, t1 = lazy_regen_energy(e0, t0)

        # Ensure daily row
        drow = ensure_daily_row(conn, tg_id, dk)
        scans = int(drow["scans"] or 0)

        # Apply scan cost rule:
        # scans 1..20 are free, scans 21+ cost 1 energy each
        if scans >= FREE_SCANS_PER_DAY:
            if e1 <= 0:
                return {"ok": False, "reason": "NO_ENERGY", "energy": e1, "scans_today": scans}
            e1 -= 1

        # Update daily stats
        scans += 1
        light = int(drow["light"] or 0)
        spite = int(drow["spite"] or 0)
        if direction == "LIGHT":
            light += 1
        else:
            spite += 1

        cur.execute("""
          UPDATE daily_stats
          SET scans = ?, light = ?, spite = ?
          WHERE tg_id = ? AND day_key = ?
        """, (scans, light, spite, tg_id, dk))

        # Update user totals + AP (+1 per swipe)
        if direction == "LIGHT":
            cur.execute("""
              UPDATE users
              SET total_light = total_light + 1, ap = ap + 1, energy = ?, last_energy_ts = ?, updated_ts = ?
              WHERE tg_id = ?
            """, (e1, t1, now_ts(), tg_id))
        else:
            cur.execute("""
              UPDATE users
              SET total_spite = total_spite + 1, ap = ap + 1, energy = ?, last_energy_ts = ?, updated_ts = ?
              WHERE tg_id = ?
            """, (e1, t1, now_ts(), tg_id))

        # Record swipe (best-effort)
        cur.execute("""
          INSERT INTO swipes (tg_id, target_id, direction, created_ts)
          VALUES (?, ?, ?, ?)
        """, (tg_id, int(payload.target_id), direction, now_ts()))

        conn.commit()

        # Return updated user + daily
        user2 = get_user_row(conn, tg_id)
        payload_user = as_user_payload(user2)
        payload_user["energy"] = int(user2["energy"] or ENERGY_MAX)

        # Faction unlock message (client can use this)
        unlocked = scans >= FACTION_UNLOCK_AT

        return {
            "ok": True,
            "direction": direction,
            "ap_delta": 1,
            "energy": payload_user["energy"],
            "scans_today": scans,
            "free_scans_limit": FREE_SCANS_PER_DAY,
            "faction_unlocked": unlocked,
            "user": payload_user
        }
    finally:
        conn.close()


# =============================================================================
# Run locally:
#   uvicorn main:app --host 0.0.0.0 --port 8000
# On Render:
#   Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
# =============================================================================
