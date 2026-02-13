# AZEUQER TITANIUM 10.0 - MODULE 01: IDENTITY
import os, json, time, urllib.parse
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client

# --- INIT ---
app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("CRITICAL: SUPABASE KEYS MISSING")
    supabase = None
else:
    # We allow the client to auto-negotiate HTTP2 now that 'h2' is installed
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
    if init_data == "debug_mode": return {"id": 12345, "username": "Architect"}
    try:
        parsed = dict(x.split('=', 1) for x in init_data.split('&'))
        user_json = urllib.parse.unquote(parsed.get('user', '{}'))
        return json.loads(user_json)
    except:
        return {"id": 12345, "username": "Debug_User"}

# --- ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "TITANIUM 10.0 ONLINE"}

@app.post("/auth/login")
async def login(req: dict):
    try:
        u_data = validate_auth(req.get('initData'))
        uid = u_data['id']
        
        # Check if user exists
        res = supabase.table("users").select("*").eq("user_id", uid).execute()
        if res.data:
            return {"status": "ok", "user": res.data[0]}
        
        # Create New User
        new_user = {
            "user_id": uid, 
            "username": u_data.get('username', 'Citizen'),
            "ap": 0
        }
        supabase.table("users").insert(new_user).execute()
        return {"status": "created", "user": new_user}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    try:
        u_data = validate_auth(initData)
        uid = u_data['id']
        content = await file.read()
        
        filename = f"{uid}_{int(time.time())}.jpg"
        
        # Upload to Storage
        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            return {"status": "error", "msg": "BUCKET_FAIL"}

        # Save URL to User Profile
        supabase.table("users").update({"bio_lock_url": url}).eq("user_id", uid).execute()
        return {"status": "success", "url": url}
    
    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        return {"status": "error", "msg": str(e)}
# ADD THIS TO THE BOTTOM OF main.py
@app.post("/auth/reset")
async def reset_user(req: dict):
    try:
        u_data = validate_auth(req.get('initData'))
        uid = u_data['id']
        # Wipe the bio_lock_url to force re-entry
        supabase.table("users").update({"bio_lock_url": None}).eq("user_id", uid).execute()
        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}
