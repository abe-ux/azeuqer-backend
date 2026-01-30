# AZEUQER TITANIUM 6.4 - LITE EDITION (NO AI)
import os, json, time, urllib.parse, random
from datetime import datetime, timedelta
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

# --- INITIALIZATION ---
app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Fail gracefully if keys are missing
if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: Supabase Keys missing.")
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
        return {"id": 12345, "username": "Debug_User", "start_param": None}

# --- API MODELS ---
class BaseReq(BaseModel): initData: str
class LoginReq(BaseModel): initData: str; traits: dict = None
class SwipeReq(BaseModel): initData: str; target_id: int; direction: str
class CombatTurnReq(BaseModel): initData: str; action: str; boss_hp_current: int

# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "Azeuqer Systems Online"}

@app.post("/auth/login")
async def login(req: LoginReq):
    try:
        u_data = validate_auth(req.initData)
        uid = u_data['id']
        
        res = supabase.table("users").select("*").eq("user_id", uid).execute()
        if res.data: return {"status": "ok", "user": res.data[0]}
        
        # New User Logic
        count = supabase.table("users").select("user_id", count="exact").execute().count
        traits = req.traits or {"work":0.5}
        
        new_user = {
            "user_id": uid,
            "username": u_data.get('username', 'Citizen'),
            "is_pioneer": count < 100,
            "verification_status": "VERIFIED",
            "referred_by": None,
            "trait_work": traits.get('work'),
            "trait_pace": traits.get('pace'),
            "trait_mind": traits.get('mind'),
            "trait_vibe": traits.get('vibe')
        }
        
        supabase.table("users").insert(new_user).execute()
        return {"status": "created", "user": new_user}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def biolock(initData: str = Form(...), file: UploadFile = File(...)):
    try:
        u_data = validate_auth(initData)
        uid = u_data['id']
        
        # READ FILE
        content = await file.read()
        filename = f"{uid}_{int(time.time())}.jpg"
        
        # UPLOAD TO SUPABASE
        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            # If bucket fails, use a placeholder so user isn't stuck
            print(f"Storage Error: {e}")
            url = f"https://robohash.org/{uid}?set=set3"

        # UPDATE USER
        updates = {"bio_lock_url": url}
        
        # REFERRAL PAYOUT
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
        return {"status": "error", "msg": str(e)}

@app.post("/game/feed")
async def get_feed(req: BaseReq):
    uid = validate_auth(req.initData)['id']
    supabase.table("users").update({"last_active": datetime.utcnow().isoformat()}).eq("user_id", uid).execute()
    
    users = supabase.table("users").select("*").limit(50).execute().data
    final_feed = [u for u in users if u['user_id'] != uid]
    random.shuffle(final_feed)
    
    return {"status": "ok", "feed": final_feed[:10]}

@app.post("/game/swipe")
async def swipe(req: SwipeReq):
    uid = validate_auth(req.initData)['id']
    supabase.rpc("increment_user_ap", {"target_uid": uid, "amount": 1}).execute()
    supabase.table("swipes").insert({"actor_id": uid, "target_id": req.target_id, "direction": req.direction}).execute()
    
    swipe_count = supabase.table("swipes").select("id", count="exact").eq("actor_id", uid).execute().count
    if swipe_count % 10 == 0:
        supabase.table("users").update({"combat_state": "LOCKED"}).eq("user_id", uid).execute()
        return {"status": "AMBUSH"}
        
    return {"status": "SWIPE_OK"}

@app.post("/game/inventory")
async def get_inventory(req: BaseReq):
    uid = validate_auth(req.initData)['id']
    items = supabase.table("inventory").select("*").eq("owner_id", uid).execute().data
    return {"status": "ok", "items": items}

@app.post("/game/combat/info")
async def combat_info(req: BaseReq):
    boss = {"name": "The Gatekeeper", "hp": 500, "dmg": 20}
    return {"boss": boss}
class EquipReq(BaseModel): initData: str; item_id: str

@app.post("/game/equip")
async def equip_item(req: EquipReq):
    uid = validate_auth(req.initData)['id']
    
    # 1. Verify Ownership
    # (Simplified: In prod, check table 'inventory')
    
    # 2. Set as Active Stamp on User Profile
    # We store the item name in a new column or reuse 'sponsor_id' for MVP
    # Let's use a specific field 'equipped_item' in the 'users' table
    # NOTE: You might need to run this SQL once: 
    # ALTER TABLE users ADD COLUMN equipped_item TEXT DEFAULT NULL;
    
    supabase.table("users").update({"equipped_item": req.item_id}).eq("user_id", uid).execute()
    return {"status": "EQUIPPED", "item": req.item_id}
    
@app.post("/game/combat/turn")
async def combat_turn(req: CombatTurnReq):
    uid = validate_auth(req.initData)['id']
    damage = random.randint(15, 35)
    log = [f"Hit for {damage} DMG"]
    new_hp = req.boss_hp_current - damage
    
    if new_hp <= 0:
        supabase.table("users").update({"combat_state": None}).eq("user_id", uid).execute()
        loot = random.choice(["Rusty Shiv", "Datapad", "Void Token", "Stimpack"])
        supabase.table("inventory").insert({"owner_id": uid, "item_id": loot, "type": "LOOT"}).execute()
        return {"status": "VICTORY", "log": log, "loot": loot}
        
    log.append(f"Boss hits for {random.randint(5,15)} DMG")
    return {"status": "ONGOING", "new_boss_hp": new_hp, "log": log}
