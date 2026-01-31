# AZEUQER TITANIUM 7.0 - MODULE 01: BIO-LOCK
import os, json, time, urllib.parse
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

# --- INIT ---
app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("CRITICAL: SUPABASE KEYS MISSING")
else:
    print("SYSTEM: CONNECTED TO SUPABASE")
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
    # Debug backdoor for testing in browser
    if init_data == "debug_mode": 
        return {"id": 12345, "username": "Architect"}
    
    try:
        parsed = dict(x.split('=', 1) for x in init_data.split('&'))
        user_json = urllib.parse.unquote(parsed.get('user', '{}'))
        return json.loads(user_json)
    except:
        print("AUTH FAILED: Using fallback debug user")
        return {"id": 12345, "username": "Debug_User"}

# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "MODULE 01 ONLINE"}

@app.post("/auth/login")
async def login(req: dict):
    # Minimal Login: Just check if user exists
    try:
        u_data = validate_auth(req.get('initData'))
        uid = u_data['id']
        
        # Check DB
        res = supabase.table("users").select("*").eq("user_id", uid).execute()
        if res.data:
            return {"status": "ok", "user": res.data[0]}
        
        # Create New
        new_user = {
            "user_id": uid,
            "username": u_data.get('username', 'Citizen')
        }
        supabase.table("users").insert(new_user).execute()
        return {"status": "created", "user": new_user}
        
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/upload")
async def upload_face(initData: str = Form(...), file: UploadFile = File(...)):
    print("--- UPLOAD REQUEST RECEIVED ---")
    try:
        u_data = validate_auth(initData)
        uid = u_data['id']
        
        # 1. READ FILE
        content = await file.read()
        filename = f"{uid}_{int(time.time())}.jpg"
        print(f"Processing File: {filename} ({len(content)} bytes)")

        # 2. UPLOAD TO SUPABASE
        try:
            supabase.storage.from_("bio-locks").upload(filename, content, {"content-type": "image/jpeg"})
            # Get Public URL
            url = f"{SUPABASE_URL}/storage/v1/object/public/bio-locks/{filename}"
            print(f"Upload Success: {url}")
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            return {"status": "error", "msg": "BUCKET_ERROR"}

        # 3. UPDATE USER PROFILE
        supabase.table("users").update({"bio_lock_url": url}).eq("user_id", uid).execute()
        
        return {"status": "success", "url": url}

    except Exception as e:
        print(f"CRITICAL FAILURE: {e}")
        return {"status": "error", "msg": str(e)}
