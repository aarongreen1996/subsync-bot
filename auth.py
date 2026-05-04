import os
import secrets
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, redirect
import requests as http_requests
from twilio.rest import Client

auth_bp = Blueprint("auth", __name__)

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
APP_URL       = os.environ.get("APP_URL", "https://www.subsync.xyz")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
TWILIO_ACCOUNT_SID     = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN      = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")


def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}

def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json() if r.status_code == 200 else []

def db_post(path, payload):
    r = http_requests.post(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                           headers={**sb_headers(), "Prefer": "return=minimal"})
    return r.status_code in (200, 201)

def db_patch(path, payload):
    http_requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                        headers={**sb_headers(), "Prefer": "return=minimal"})


def create_magic_token(whatsapp_number):
    token      = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    db_post("auth_tokens", {"token": token, "whatsapp": whatsapp_number,
                             "expires_at": expires_at, "used": False})
    return f"{APP_URL}/login?token={token}"


def verify_token(token):
    results = db_get(f"auth_tokens?token=eq.{token}&limit=1")
    if not isinstance(results, list) or not results:
        return None
    record = results[0]
    if record.get("used"):
        return None
    expires_raw = record.get("expires_at", "")
    try:
        expires_dt = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_dt:
            return None
    except Exception:
        pass
    return record.get("whatsapp")


# ── Magic link login ──────────────────────────────────────────────────────────
@auth_bp.route("/login")
def magic_login():
    token = request.args.get("token", "")
    if not token:
        return "<h2>Invalid link</h2>", 400

    whatsapp = verify_token(token)
    if not whatsapp:
        return """<!DOCTYPE html><html><head><meta charset="UTF-8">
        <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@400;600&display=swap" rel="stylesheet">
        <style>body{font-family:'Manrope',sans-serif;background:#fff3e0;min-height:100vh;display:flex;align-items:center;justify-content:center;}
        .box{background:white;border-radius:16px;padding:48px;text-align:center;max-width:400px;border:1px solid #e8ddd0;}
        h2{font-family:'Bebas Neue',serif;color:#d62828;font-size:32px;margin-bottom:12px;letter-spacing:2px;}
        p{color:#8a7560;font-weight:300;line-height:1.6;}</style></head><body>
        <div class="box"><h2>Link Expired</h2>
        <p>This login link has expired or already been used.<br><br>
        Send <strong>my dashboard</strong> to the Note2Quote bot on WhatsApp to get a new one.</p>
        </div></body></html>""", 401

    number = whatsapp.replace("whatsapp:", "")
    return redirect(f"/dashboard?autologin={number}")


# ── Username/password login ───────────────────────────────────────────────────
@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.json or {}
    login_id = data.get("login", "").strip()
    password = data.get("password", "").strip()

    if not login_id or not password:
        return jsonify({"ok": False, "error": "Login and password required"}), 400

    # Check master password first (backwards compatibility)
    if password == DASHBOARD_PASSWORD:
        # login_id is the whatsapp number
        number = login_id if login_id.startswith("+") else login_id
        return jsonify({"ok": True, "whatsapp": number, "session_token": "__magic__"})

    # Try username lookup
    companies = db_get(f"companies?username=eq.{login_id}&limit=1")
    if isinstance(companies, list) and companies:
        company = companies[0]
        stored_pw = company.get("dashboard_password")
        if stored_pw and stored_pw == password:
            whatsapp = company.get("whatsapp_number", "").replace("whatsapp:", "")
            return jsonify({"ok": True, "whatsapp": whatsapp, "session_token": "__magic__"})
        elif not stored_pw and password == DASHBOARD_PASSWORD:
            whatsapp = company.get("whatsapp_number", "").replace("whatsapp:", "")
            return jsonify({"ok": True, "whatsapp": whatsapp, "session_token": "__magic__"})

    # Try whatsapp number lookup
    encoded = login_id.replace("+", "%2B")
    wa_num  = "whatsapp:" + login_id if not login_id.startswith("whatsapp:") else login_id
    companies = db_get(f"companies?whatsapp_number=eq.{wa_num.replace('+','%2B')}&limit=1")
    if isinstance(companies, list) and companies:
        company = companies[0]
        stored_pw = company.get("dashboard_password")
        if stored_pw and stored_pw == password:
            whatsapp = login_id
            return jsonify({"ok": True, "whatsapp": whatsapp, "session_token": "__magic__"})
        elif not stored_pw and password == DASHBOARD_PASSWORD:
            return jsonify({"ok": True, "whatsapp": login_id, "session_token": "__magic__"})

    return jsonify({"ok": False, "error": "Wrong username or password"}), 401


# ── Magic link request via API ────────────────────────────────────────────────
@auth_bp.route("/api/auth/magic-request", methods=["POST"])
def magic_request():
    data  = request.json or {}
    login = data.get("login", "").strip()
    if not login:
        return jsonify({"ok": False, "error": "Login required"}), 400

    # Find company by username or whatsapp
    company = None
    companies = db_get(f"companies?username=eq.{login}&limit=1")
    if isinstance(companies, list) and companies:
        company = companies[0]
    else:
        wa = "whatsapp:" + login if not login.startswith("whatsapp:") else login
        companies = db_get(f"companies?whatsapp_number=eq.{wa.replace('+','%2B')}&limit=1")
        if isinstance(companies, list) and companies:
            company = companies[0]

    if not company:
        return jsonify({"ok": False, "error": "No account found for that username or number"}), 404

    whatsapp = company.get("whatsapp_number", "")
    if not whatsapp:
        return jsonify({"ok": False, "error": "No WhatsApp number on file"}), 400

    try:
        login_url = create_magic_token(whatsapp)
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=whatsapp,
            body=(f"🔐 *Your Note2Quote login link*\n\n"
                  f"Tap to log in instantly:\n{login_url}\n\n"
                  f"⏰ Expires in 30 minutes.")
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:100]}), 500


# ── Old auth endpoint (keep for compatibility) ────────────────────────────────
@auth_bp.route("/api/auth", methods=["POST"])
def auth():
    data = request.json or {}
    pw   = data.get("password", "")
    if pw == DASHBOARD_PASSWORD or pw == "__magic__":
        return jsonify({"ok": True})
    # Also accept company-specific passwords
    companies = db_get(f"companies?dashboard_password=eq.{pw}&limit=1")
    if isinstance(companies, list) and companies:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Wrong password"}), 401
