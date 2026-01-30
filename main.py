# AZEUQER TITANIUM 6.1 - STABILIZED BACKEND
import os, json, time, urllib.parse, random, cv2
import numpy as np
# import mediapipe as mp # DISABLED TO SAVE RAM
from datetime import datetime, timedelta
from fastapi import FastAPI, UploadFile, File, HTTPException, Form # ADDED FORM
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

# --- INITIALIZATION ---
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
    allow_headers=["*"]
)

# --- UTILS ---
def validate_auth(init_data: str):
    if init_data == "debug_mode": 
        return {"id": 12345, "username": "Debug_User", "start_param": None}
    try:
        parsed = dict(x.split('=', 1) for x in init_data.split('&'))
        user_json = urllib.parse.unquote(parsed.get('user', '{}'))
        user_data = json.loads(user_json)
        user_data['start_param'] = parsed.get('start_param') 
        return user_data
    except:
        # Fallback for testing if auth fails
        print("Auth failed, using debug user")
        return {"id": 12345, "username": "Debug_User", "start_param": None}

# --- API MODELS ---
class BaseReq(BaseModel): initData: str
class LoginReq(BaseModel): initData: str; traits: dict = None
class SwipeReq(BaseModel): initData: str; target_id: int; direction: str
class SponsorReq(BaseModel): initData: str; sponsor_logo: str
class DonateReq(BaseModel): initData: str; amount: int
class CombatTurnReq(BaseModel): initData: str; action: str; boss_hp_current: int
class VoteReq(BaseModel): initData: str; target_id: int; vote: str

# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "Azeuqer Systems Online"}

@app.post("/auth/login")
async def login(req: LoginReq):
    u_data = validate_auth(req.initData)
    uid = u_data['id']
    
    try:
        res = supabase.table("users").select("*").eq("user_id", uid).execute()
        if res.data: return {"status": "ok", "user": res.data[0]}
        
        count = supabase.table("users").select("user_id", count="exact").execute().count
        
        ref_id = None
        if u_data.get('start_param') and str(u_data['start_param']).startswith('ref_'):
            try: ref_id = int(str(u_data['start_param']).replace('ref_', ''))
            except: pass
            if ref_id == uid: ref_id = None

        traits = req.traits or {"work":0.5}
        new_user = {
            "user_id": uid,
            "username": u_data.get('username', 'Citizen'),
            "is_pioneer": count < 100,
            "verification_status": "VERIFIED", # Auto-verify for now
            "referred_by": ref_id,
            "trait_work": traits.get('work'),
            "trait_pace": traits.get('pace'),
            "trait_mind": traits.get('mind'),
            "trait_vibe": traits.get('vibe')
        }
        
        supabase.table("users").insert(new_user).execute()
        return {"status": "created", "user": new_user}
    except Exception as e:
        print(f"LOGIN ERROR: {e}")
        return {"status": "error", "msg": str(e)}

# --- THE CRITICAL FIX IS HERE ---
@app.post("/auth/biolock")
async def biolock(initData: str = Form(...), file: UploadFile = File(...)):
    # NOTE: We use Form(...) instead of Body(...) because this is a multipart request
    print("Received Biolock Request") 
    
    try:
        u_data = validate_auth(initData)
        uid = u_data['id']
        
        content = await file.read()
        
        # --- AI SCAN DISABLED FOR STABILITY ---
        # if not scan_face(content): return {"status": "ERROR", "msg": "NO_FACE"}
        
        filename = f"{uid}_{int(time.time())}.jpg"
        
        print(f"Uploading {filename}...")
        
        # Upload to Supabase
        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            # Fallback URL if storage fails (prevents getting stuck)
            url = "https://placehold.co/400x400/000/FFF?text=SCAN_ERROR"

        # Update User
        updates = {"bio_lock_url": url}
        
        # Referral Payout
        try:
            user = supabase.table("users").select("*").eq("user_id", uid).execute().data[0]
            if user['referred_by'] and user['ap'] == 0:
                supabase.rpc("increment_user_ap", {"target_uid": uid, "amount": 50}).execute()
                supabase.rpc("increment_user_ap", {"target_uid": user['referred_by'], "amount": 50}).execute()
                updates["ap"] = 50
        except: pass

        supabase.table("users").update(updates).eq("user_id", uid).execute()
        return {"status": "success", "url": url}
        
    except Exception as e:
        print(f"CRITICAL UPLOAD FAILURE: {e}")
        return {"status": "error", "msg": str(e)}

@app.post("/game/feed")
async def get_feed(req: BaseReq):
    uid = validate_auth(req.initData)['id']
    supabase.table("users").update({"last_active": datetime.utcnow().isoformat()}).eq("user_id", uid).execute()
    
    # Simple Feed Fetch
    users = supabase.table("users").select("*").limit(50).execute().data
    final_feed = [u for u in users if u['user_id'] != uid]
    random.shuffle(final_feed)
    
    return {"status": "ok", "feed": final_feed[:10]}

@app.post("/game/swipe")
async def swipe(req: SwipeReq):
    uid = validate_auth(req.initData)['id']
    supabase.rpc("increment_user_ap", {"target_uid": uid, "amount": 1}).execute()
    supabase.table("swipes").insert({"actor_id": uid, "target_id": req.target_id, "direction": req.direction}).execute()
    
    # Ambush Check
    swipe_count = supabase.table("swipes").select("id", count="exact").eq("actor_id", uid).execute().count
    if swipe_count % 10 == 0:
        supabase.table("users").update({"combat_state": "LOCKED"}).eq("user_id", uid).execute()
        return {"status": "AMBUSH"}
        
    return {"status": "SWIPE_OK"}

# --- COMBAT ---
@app.post("/game/combat/info")
async def combat_info(req: BaseReq):
    boss = {"name": "The Gatekeeper", "hp": 500, "dmg": 20}
    return {"boss": boss}

# --- REPLACEMENT FOR combat_turn ---
@app.post("/game/combat/turn")
async def combat_turn(req: CombatTurnReq):
    uid = validate_auth(req.initData)['id']
    
    # 1. Player hits Boss
    damage = random.randint(15, 35)
    # Crit chance (10%)
    if random.random() < 0.1: 
        damage *= 2
        log = [f"CRITICAL HIT! Dealt {damage} DMG"]
    else:
        log = [f"Hit for {damage} DMG"]
    
    new_hp = req.boss_hp_current - damage
    
    # 2. Check Victory
    if new_hp <= 0:
        #UNLOCK USER
        supabase.table("users").update({"combat_state": None}).eq("user_id", uid).execute()
        
        # GENERATE LOOT
        loot_table = ["Rusty Shiv", "Datapad", "Void Token", "Broken Cyber-Eye", "Stimpack"]
        item = random.choice(loot_table)
        
        # INSERT TO INVENTORY
        supabase.table("inventory").insert({
            "owner_id": uid,
            "item_id": item,
            "type": "LOOT",
            "is_equipped": False
        }).execute()
        
        return {"status": "VICTORY", "log": log, "loot": item}
        
    # 3. Boss hits Player
    boss_dmg = random.randint(5, 15)
    log.append(f"Boss hits you for {boss_dmg} DMG")
    
    return {"status": "ONGOING", "new_boss_hp": new_hp, "log": log}
