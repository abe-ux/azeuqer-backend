# AZEUQER TITANIUM - main.py (Render)
import os, json, time, urllib.parse, re
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

# --- INIT ---
app = FastAPI()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase = None
if not SUPABASE_URL or not SUPABASE_KEY:
    print("CRITICAL: SUPABASE KEYS MISSING")
else:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# --- MODELS ---
class LoginReq(BaseModel):
    initData: str
    referralCode: Optional[str] = None

class EmailReq(BaseModel):
    initData: str
    email: str

class ResetReq(BaseModel):
    initData: str

# --- UTILS ---
def require_supabase():
    if supabase is None:
        return False
    return True

def validate_auth(init_data: str):
    # DEBUG MODE
    if init_data == "debug_mode":
        return {"id": 12345, "username": "Architect"}

    # Telegram initData contains urlencoded pairs, including user JSON
    try:
        parsed = dict(x.split("=", 1) for x in init_data.split("&"))
        user_json = urllib.parse.unquote(parsed.get("user", "{}"))
        return json.loads(user_json)
    except:
        return {"id": 12345, "username": "Debug_User"}

def is_valid_email(email: str) -> bool:
    if not email:
        return False
    email = email.strip()
    # basic but safe validation
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", email, re.IGNORECASE))

# --- ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "AZEUQER BACKEND ONLINE"}

@app.post("/auth/login")
async def login(req: LoginReq):
    if not require_supabase():
        return {"status": "error", "msg": "SUPABASE_NOT_CONFIGURED"}

    try:
        u_data = validate_auth(req.initData)
        uid = int(u_data["id"])
        uname = u_data.get("username") or "Citizen"

        # Look up user
        res = supabase.table("users").select("*").eq("user_id", uid).execute()
        if res.data:
            return {"status": "ok", "user": res.data[0]}

        # Founder logic: first 23 get auto-accepted
        # NOTE: If your schema uses different fields, adjust here.
        count_res = supabase.table("users").select("user_id", count="exact").execute()
        total_users = (count_res.count or 0)

        role = "FOUNDER" if total_users < 23 else "CITIZEN"
        status = "VERIFIED" if total_users < 23 else "PENDING"

        # Referral parsing (optional)
        referred_by = None
        if req.referralCode and req.referralCode.startswith("ref_"):
            try:
                rid = int(req.referralCode.split("_")[1])
                if rid != uid:
                    referred_by = rid
            except:
                referred_by = None

        new_user = {
            "user_id": uid,
            "username": uname,
            "ap": 0,
            "email": None,
            "bio_lock_url": None,
            "role": role,
            "status": status,
            "referred_by": referred_by
        }

        supabase.table("users").insert(new_user).execute()

        # Re-fetch for consistency
        res2 = supabase.table("users").select("*").eq("user_id", uid).execute()
        return {"status": "created", "user": (res2.data[0] if res2.data else new_user)}

    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/profile/email")
async def save_email(req: EmailReq):
    """
    Saves email for a user.
    Awards +500 AP ONLY the first time email is set.
    """
    if not require_supabase():
        return {"status": "error", "msg": "SUPABASE_NOT_CONFIGURED"}

    try:
        u_data = validate_auth(req.initData)
        uid = int(u_data["id"])

        email = (req.email or "").strip().lower()
        if not is_valid_email(email):
            return {"status": "error", "msg": "INVALID_EMAIL"}

        # Fetch current user
        res = supabase.table("users").select("*").eq("user_id", uid).execute()
        if not res.data:
            return {"status": "error", "msg": "USER_NOT_FOUND"}

        user = res.data[0]
        already_had_email = bool(user.get("email"))

        updates = {"email": email}
        # Award only once
        if not already_had_email:
            updates["ap"] = int(user.get("ap") or 0) + 500

        supabase.table("users").update(updates).eq("user_id", uid).execute()

        # Return updated user
        res2 = supabase.table("users").select("*").eq("user_id", uid).execute()
        return {"status": "ok", "user": (res2.data[0] if res2.data else {**user, **updates})}

    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/auth/biolock")
async def upload_biolock(initData: str = Form(...), file: UploadFile = File(...)):
    """
    Uploads selfie to Supabase Storage bucket "bio-locks" and stores public URL on user row.
    """
    if not require_supabase():
        return {"status": "error", "msg": "SUPABASE_NOT_CONFIGURED"}

    try:
        u_data = validate_auth(initData)
        uid = int(u_data["id"])

        content = await file.read()
        if not content or len(content) < 2000:
            return {"status": "error", "msg": "FILE_TOO_SMALL"}

        filename = f"{uid}_{int(time.time())}.jpg"

        try:
            supabase.storage.from_("bio-locks").upload(
                filename,
                content,
                {"content-type": "image/jpeg"}
            )
            url = supabase.storage.from_("bio-locks").get_public_url(filename)
        except Exception as e:
            print(f"STORAGE ERROR: {e}")
            return {"status": "error", "msg": "BUCKET_FAIL"}

        supabase.table("users").update({"bio_lock_url": url}).eq("user_id", uid).execute()
        return {"status": "success", "url": url}

    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        return {"status": "error", "msg": str(e)}

@app.post("/auth/reset")
async def reset_user(req: ResetReq):
    if not require_supabase():
        return {"status": "error", "msg": "SUPABASE_NOT_CONFIGURED"}

    try:
        u_data = validate_auth(req.initData)
        uid = int(u_data["id"])
        supabase.table("users").update({"bio_lock_url": None}).eq("user_id", uid).execute()
        return {"status": "RESET_COMPLETE"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}
