# AZEUQER TITANIUM 6.0 - UNIFIED BACKEND
import os, json, time, urllib.parse, random, cv2
import numpy as np
import mediapipe as mp
from datetime import datetime, timedelta
from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

# --- INITIALIZATION ---
app = FastAPI()

# THESE WILL BE SET IN RENDER DASHBOARD
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: Supabase Keys missing. App will crash if not set in Environment Variables.")
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

mp_face_mesh = mp.solutions.face_mesh.FaceMesh(
    static_image_mode=True, max_num_faces=1, refine_landmarks=True
)

# --- UTILS ---
def validate_auth(init_data: str):
    # Allows testing in browser with "debug_mode"
    if init_data == "debug_mode": 
        return {"id": 12345, "username": "Debug_User", "start_param": None}
    
    try:
        parsed = dict(x.split('=', 1) for x in init_data.split('&'))
        user_json = urllib.parse.unquote(parsed.get('user', '{}'))
        user_data = json.loads(user_json)
        user_data['start_param'] = parsed.get('start_param') 
        return user_data
    except Exception as e:
        raise HTTPException(status_code=403, detail="Invalid Telegram Auth")

def scan_face(image_bytes):
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None: return False
        results = mp_face_mesh.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        return bool(results.multi_face_landmarks)
    except: return False

# --- API MODELS ---
class BaseReq(BaseModel): initData: str
class LoginReq(BaseModel): initData: str; traits: dict = None
class SwipeReq(BaseModel): initData: str; target_id: int; direction: str
class SponsorReq(BaseModel): initData: str; sponsor_logo: str
class DonateReq(BaseModel): initData: str; amount: int
class CombatTurnReq(BaseModel): initData: str; action: str; boss_hp_current: int
class HireReq(BaseModel): initData: str; merc_id: int
class VoteReq(BaseModel): initData: str; target_id: int; vote: str

# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "Azeuqer Systems Online"}

@app.post("/auth/login")
async def login(req: LoginReq):
    u_data = validate_auth(req.initData)
    uid = u_data['id']
    
    # Check if user exists
    res = supabase.table("users").select("*").eq("user_id", uid).execute()
    if res.data:
        return {"status": "ok", "user": res.data[0]}
    
    # New User Logic
    count = supabase.table("users").select("user_id", count="exact").execute().count
    
    # Referral Logic
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
        "verification_status": "VERIFIED" if count < 100 else "PENDING",
        "referred_by": ref_id,
        "trait_work": traits.get('work'),
        "trait_pace": traits.get('pace'),
        "trait_mind": traits.get('mind'),
        "trait_vibe": traits.get('vibe')
    }
    
    supabase.table("users").insert(new_user).execute()
    return {"status": "created", "user": new_user}

@app.post("/auth/biolock")
async def biolock(initData: str = Body(...), file: UploadFile = File(...)):
    u_data = validate_auth(initData)
    uid = u_data['id']
    
    content = await file.read()
    if not scan_face(content):
        return {"status": "ERROR", "msg": "NO_FACE_DETECTED"}
        
    filename = f"{uid}_{int(time.time())}.jpg"
    supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
    url = supabase.storage.from_("bio-locks").get_public_url(filename)
    
    # Referral Payout Trigger
    user = supabase.table("users").select("*").eq("user_id", uid).execute().data[0]
    if user['referred_by'] and user['ap'] == 0:
        supabase.rpc("increment_user_ap", {"target_uid": uid, "amount": 50}).execute()
        supabase.rpc("increment_user_ap", {"target_uid": user['referred_by'], "amount": 50}).execute()

    supabase.table("users").update({"bio_lock_url": url}).eq("user_id", uid).execute()
    return {"status": "success", "url": url}

@app.post("/game/feed")
async def get_feed(req: BaseReq):
    uid = validate_auth(req.initData)['id']
    supabase.table("users").update({"last_active": datetime.utcnow().isoformat()}).eq("user_id", uid).execute()
    
    # Get exclusions
    swipes = supabase.table("swipes").select("target_id").eq("actor_id", uid).execute().data
    exclude = [x['target_id'] for x in swipes]
    exclude.append(uid)
    
    # Fetch Feed (Verified only)
    users = supabase.table("users").select("*").eq("verification_status", "VERIFIED").limit(50).execute().data
    final_feed = [u for u in users if u['user_id'] not in exclude]
    random.shuffle(final_feed)
    
    return {"status": "ok", "feed": final_feed[:10]}

@app.post("/game/swipe")
async def swipe(req: SwipeReq):
    uid = validate_auth(req.initData)['id']
    
    # Check Energy/Free Swipes logic would go here (Simplified for deployment safety)
    supabase.rpc("increment_user_ap", {"target_uid": uid, "amount": 1}).execute()
    
    # Update Target Votes
    target_col = "votes_light_month" if req.direction == "LIGHT" else "votes_spite_month"
    # Note: Pure SQL increment is safer, simplified here:
    t_res = supabase.table("users").select(target_col).eq("user_id", req.target_id).execute()
    if t_res.data:
        new_val = t_res.data[0][target_col] + 1
        supabase.table("users").update({target_col: new_val}).eq("user_id", req.target_id).execute()
        
    supabase.table("swipes").insert({"actor_id": uid, "target_id": req.target_id, "direction": req.direction}).execute()
    
    # Check Ambush (Every 10th swipe)
    swipe_count = supabase.table("swipes").select("id", count="exact").eq("actor_id", uid).execute().count
    if swipe_count % 10 == 0:
        supabase.table("users").update({"combat_state": "LOCKED"}).eq("user_id", uid).execute()
        return {"status": "AMBUSH"}
        
    return {"status": "SWIPE_OK"}

# --- LEADERBOARD & SPONSOR ---
@app.post("/game/leaderboard")
async def leaderboard(req: BaseReq):
    # Fetch Top 10 by AP (Simple Warlord Metric for MVP)
    users = supabase.table("users").select("username, ap, bio_lock_url, sponsor_id").order("ap", desc=True).limit(20).execute().data
    return {"status": "ok", "board": users}

@app.post("/game/sponsor/equip")
async def equip_sponsor(req: SponsorReq):
    uid = validate_auth(req.initData)['id']
    # Verify Top 7 Logic...
    supabase.table("users").update({"sponsor_id": req.sponsor_logo}).eq("user_id", uid).execute()
    return {"status": "EQUIPPED"}

@app.post("/game/foundation/donate")
async def donate(req: DonateReq):
    uid = validate_auth(req.initData)['id']
    u = supabase.table("users").select("ap").eq("user_id", uid).execute().data[0]
    if u['ap'] < req.amount: return {"status": "ERROR"}
    
    supabase.table("users").update({"ap": u['ap'] - req.amount}).eq("user_id", uid).execute()
    supabase.rpc("increment_config", {"key_name": "foundation_pool_current", "delta": req.amount}).execute()
    return {"status": "DONATED"}

# --- COMBAT ---
@app.post("/game/combat/info")
async def combat_info(req: BaseReq):
    uid = validate_auth(req.initData)['id']
    u = supabase.table("users").select("*").eq("user_id", uid).execute().data[0]
    # Simple Boss Scaling
    user_stats = u['base_str'] + u['base_agi'] + u['base_int'] + u['base_vit']
    boss = {
        "name": "The Gatekeeper", 
        "hp": int((100 + user_stats) * 1.1),
        "dmg": int(user_stats * 1.1)
    }
    return {"boss": boss}

@app.post("/game/combat/turn")
async def combat_turn(req: CombatTurnReq):
    uid = validate_auth(req.initData)['id']
    # Simulation Logic
    damage = random.randint(10, 20)
    new_hp = req.boss_hp_current - damage
    
    log = [f"You hit for {damage} damage!"]
    
    if new_hp <= 0:
        supabase.table("users").update({"combat_state": None}).eq("user_id", uid).execute()
        return {"status": "VICTORY", "log": log}
        
    # Boss hits back
    boss_dmg = random.randint(5, 15)
    log.append(f"Boss hit you for {boss_dmg}!")
    
    return {"status": "ONGOING", "new_boss_hp": new_hp, "log": log}

# --- TRIBUNAL ---
@app.post("/game/tribunal/case")
async def tribunal_case(req: BaseReq):
    uid = validate_auth(req.initData)['id']
    # Get a pending user
    u = supabase.table("users").select("*").eq("verification_status", "PENDING").limit(1).execute().data
    if not u: return {"status": "EMPTY"}
    return {"status": "CASE_FOUND", "case": u[0]}

@app.post("/game/tribunal/vote")
async def tribunal_vote(req: VoteReq):
    uid = validate_auth(req.initData)['id']
    try:
        supabase.table("tribunal_votes").insert({"judge_id": uid, "target_id": req.target_id, "vote": req.vote}).execute()
        # Check consensus...
        return {"status": "VOTED"}
    except: return {"status": "ERROR"}
