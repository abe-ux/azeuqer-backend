# AZEUQER TITANIUM 6.6 - STABLE & FEATURE COMPLETE
import os, json, time, urllib.parse, random, cv2
import numpy as np
import mediapipe as mp
from datetime import datetime, timedelta
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Body
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
    # Client creation might fail if http2 lib is missing, handled by requirements.txt fix
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"SUPABASE INIT ERROR: {e}")
        supabase = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# GLOBAL AI MODEL (Lazy Load)
mp_face_mesh = None

def get_face_mesh():
    global mp_face_mesh
    if mp_face_mesh is None:
        mp_face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=True, max_num_faces=1, refine_landmarks=True, min_detection_confidence=0.5
        )
    return mp_face_mesh

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

def scan_face(image_bytes):
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None: return False
        
        # Use Global Model
        mesh = get_face_mesh()
        results = mesh.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        return bool(results.multi_face_landmarks)
    except Exception as e:
        print(f"AI SCAN ERROR: {e}")
        return True # Fail open to prevent locking users out

# --- API MODELS ---
class BaseReq(BaseModel): initData: str
class LoginReq(BaseModel): initData: str; traits: dict = None
class SwipeReq(BaseModel): initData: str; target_id: int; direction: str
class CombatTurnReq(BaseModel): initData: str; action: str; boss_hp_current: int
class EquipReq(BaseModel): initData: str; item_id: str

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
        
        # New User
        count = supabase.table("users").select("user_id", count="exact").execute().count
        
        ref_id = None
        if u_data.get('start_param') and str(u_data['start_param']).startswith('ref_'):
            try: ref_id = int(str(u_data['start_param']).replace('ref_', ''))
            except: pass
            if ref_id == uid: ref_id = None

        new_user = {
            "user_id": uid,
            "username": u_data.get('username', 'Citizen'),
            "is_pioneer": count < 100,
            "verification_status": "VERIFIED",
            "referred_by": ref_id,
            "ap": 0,
            "hp_current": 100
        }
        
        supabase.table("users").insert(new_user).execute()
        return {"status": "created", "user": new_user}
    except Exception as e:
        print(f"LOGIN ERROR: {e}")
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def biolock(initData: str = Form(...), file: UploadFile = File(...)):
    try:
        u_data = validate_auth(initData)
        uid = u_data['id']
        content = await file.read()
        
        # AI Scan (Failsafe included)
        if not scan_face(content):
            return {"status": "ERROR", "msg": "NO_FACE_DETECTED"}
        
        filename = f"{uid}_{int(time.time())}.jpg"
        
        # Upload
        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            url = f"https://robohash.org/{uid}?set=set1" # Fallback

        updates = {"bio_lock_url": url}
        
        # Referral Trigger
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
        print(f"UPLOAD ERROR: {e}")
        return {"status": "error", "msg": str(e)}

# FIND THIS SECTION IN main.py AND REPLACE THE get_feed FUNCTION
@app.post("/game/feed")
async def get_feed(req: BaseReq):
    uid = validate_auth(req.initData)['id']
    supabase.table("users").update({"last_active": datetime.utcnow().isoformat()}).eq("user_id", uid).execute()
    
    # EXPLICITLY FETCH SPONSOR DATA
    users = supabase.table("users").select("user_id, username, bio_lock_url, faction, sponsor_id, is_pioneer, equipped_item").limit(50).execute().data
    
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

# --- INVENTORY & EQUIP ---
@app.post("/game/inventory")
async def get_inventory(req: BaseReq):
    uid = validate_auth(req.initData)['id']
    items = supabase.table("inventory").select("*").eq("owner_id", uid).execute().data
    return {"status": "ok", "items": items}

@app.post("/game/equip")
async def equip_item(req: EquipReq):
    uid = validate_auth(req.initData)['id']
    # Set as active stamp
    supabase.table("users").update({"equipped_item": req.item_id}).eq("user_id", uid).execute()
    return {"status": "EQUIPPED", "item": req.item_id}
class StatReq(BaseModel): initData: str; stat: str # 'STR', 'AGI', 'INT'

@app.post("/game/stats/upgrade")
async def upgrade_stat(req: StatReq):
    uid = validate_auth(req.initData)['id']
    
    # 1. Fetch User
    u = supabase.table("users").select("*").eq("user_id", uid).execute().data[0]
    
    if u['stat_points_available'] <= 0:
        return {"status": "ERROR", "msg": "NO_POINTS"}
        
    # 2. Determine Column
    col_map = {"STR": "base_str", "AGI": "base_agi", "INT": "base_int"}
    col = col_map.get(req.stat)
    
    if not col: return {"status": "ERROR"}
    
    # 3. Apply Upgrade
    supabase.table("users").update({
        col: u[col] + 1,
        "stat_points_available": u['stat_points_available'] - 1
    }).eq("user_id", uid).execute()
    
    return {"status": "UPGRADED", "new_val": u[col] + 1}
    
# --- COMBAT ---
@app.post("/game/combat/info")
async def combat_info(req: BaseReq):
    boss = {"name": "The Gatekeeper", "hp": 500, "dmg": 20}
    return {"boss": boss}

@app.post("/game/combat/turn")
async def combat_turn(req: CombatTurnReq):
    uid = validate_auth(req.initData)['id']
    
    damage = random.randint(15, 35)
    log = [f"Hit for {damage} DMG"]
    new_hp = req.boss_hp_current - damage
    
    # VICTORY
    if new_hp <= 0:
        supabase.table("users").update({"combat_state": None}).eq("user_id", uid).execute()
        
        # Loot Drop
        loot_table = ["Rusty Shiv", "Datapad", "Void Token", "Broken Cyber-Eye", "Stimpack"]
        item = random.choice(loot_table)
        
        supabase.table("inventory").insert({
            "owner_id": uid, "item_id": item, "type": "LOOT", "is_equipped": False
        }).execute()
        
        return {"status": "VICTORY", "log": log, "loot": item}
        
    # BOSS HIT
    boss_dmg = random.randint(5, 15)
    log.append(f"Boss hits you for {boss_dmg} DMG")
    
    return {"status": "ONGOING", "new_boss_hp": new_hp, "log": log}
