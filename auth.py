import os
import secrets
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, redirect
import requests as http_requests
from twilio.rest import Client

auth_bp = Blueprint("auth", __name__)

SUPABASE_URL           = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY           = os.environ.get("SUPABASE_KEY", "")
APP_URL                = os.environ.get("APP_URL", "https://www.note2quote.co.uk")
ADMIN_PASSWORD         = os.environ.get("ADMIN_PASSWORD", "admin123")
TWILIO_ACCOUNT_SID     = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN      = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")


def sb():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}

def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb())
    return r.json() if r.status_code == 200 else []

def db_post(path, payload):
    r = http_requests.post(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                           headers={**sb(), "Prefer": "return=minimal"})
    return r.status_code in (200, 201)


def normalise_number(raw):
    """Convert any number format to whatsapp:+44XXXXXXXXX"""
    n = raw.strip().replace(" ", "").replace("-", "")
    # Remove whatsapp: prefix if present
    if n.startswith("whatsapp:"):
        n = n[9:]
    # Convert 07... to +447...
    if n.startswith("07") and len(n) == 11:
        n = "+44" + n[1:]
    # Convert 447... to +447...
    if n.startswith("44") and not n.startswith("+"):
        n = "+" + n
    # Ensure + prefix
    if not n.startswith("+"):
        n = "+" + n
    return "whatsapp:" + n


def find_company(login_id):
    """Find company by username, phone number (any format), or whatsapp number."""
    login_id = login_id.strip()

    # Try username first (clean alphanumeric lookup)
    if not any(c.isdigit() for c in login_id.replace(".", "").replace("_", "").replace("-", "")):
        results = db_get(f"companies?username=eq.{login_id}&limit=1")
        if isinstance(results, list) and results:
            return results[0]

    # Try normalising as a phone number and looking up
    try:
        wa = normalise_number(login_id)
        encoded = wa.replace("+", "%2B")
        results = db_get(f"companies?whatsapp_number=eq.{encoded}&limit=1")
        if isinstance(results, list) and results:
            return results[0]
    except Exception:
        pass

    # Try username as fallback (if it contains digits it might still be a username)
    results = db_get(f"companies?username=eq.{login_id}&limit=1")
    if isinstance(results, list) and results:
        return results[0]

    return None


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
    try:
        expires_dt = datetime.fromisoformat(
            record.get("expires_at", "").replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_dt:
            return None
    except Exception:
        pass
    # Mark as used
    http_requests.patch(
        f"{SUPABASE_URL}/rest/v1/auth_tokens?token=eq.{token}",
        json={"used": True},
        headers={**sb(), "Prefer": "return=minimal"}
    )
    return record.get("whatsapp")


# ── Magic link login page ─────────────────────────────────────────────────────
@auth_bp.route("/login")
def magic_login():
    token = request.args.get("token", "")
    if not token:
        return redirect("/dashboard")

    # Check if token exists and is valid WITHOUT marking as used yet
    # This prevents WhatsApp link preview from consuming the token
    results = db_get(f"auth_tokens?token=eq.{token}&limit=1")
    if not isinstance(results, list) or not results:
        return _expired_page()
    record = results[0]
    if record.get("used"):
        return _expired_page()
    try:
        from datetime import datetime, timezone
        expires_dt = datetime.fromisoformat(
            record.get("expires_at", "").replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_dt:
            return _expired_page()
    except Exception:
        pass

    # Show a tap-to-enter page — user must click the button
    # This way WhatsApp preview doesn't consume the token
    number = record.get("whatsapp", "").replace("whatsapp:", "")
    page = """<!DOCTYPE html><html><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@400;600;700&display=swap" rel="stylesheet">
    <style>*{box-sizing:border-box;margin:0;padding:0;}body{font-family:'Manrope',sans-serif;background:#0c0d10;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
    .box{background:#131419;border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:48px 36px;text-align:center;max-width:380px;width:100%;}
    .logo{font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:3px;color:#fff;margin-bottom:8px;}
    p{color:rgba(255,255,255,0.4);font-size:14px;line-height:1.7;margin-bottom:28px;}
    .btn{display:block;background:#f59e0b;color:#0c0d10;padding:15px 28px;border-radius:10px;text-decoration:none;font-weight:700;font-size:16px;box-shadow:0 4px 0 #d97706;transition:all .15s;}
    .btn:active{transform:translateY(2px);box-shadow:0 2px 0 #d97706;}
    .note{font-size:11px;color:rgba(255,255,255,0.2);margin-top:16px;}</style></head>
    <body><div class="box">
    <div class="logo">Note2Quote</div>
    <p>Your dashboard is ready. Tap the button below to open it.</p>
    <a href="/login/confirm?token=TOKEN" class="btn">Open my dashboard →</a>
    <p class="note">This link expires in 30 minutes</p>
    </div></body></html>""".replace("TOKEN", token)
    return page


@auth_bp.route("/login/confirm")
def magic_login_confirm():
    """Second step — user actively tapped the button, now verify and redirect."""
    token    = request.args.get("token", "")
    whatsapp = verify_token(token)  # This marks as used
    if not whatsapp:
        return _expired_page()
    number = whatsapp.replace("whatsapp:", "")
    return redirect(f"/dashboard?autologin={number}")


def _expired_page():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@400;600&display=swap" rel="stylesheet">
    <style>*{box-sizing:border-box;margin:0;padding:0;}body{font-family:'Manrope',sans-serif;background:#0c0d10;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
    .box{background:#131419;border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:40px 32px;text-align:center;max-width:380px;width:100%;}
    h2{font-family:'Bebas Neue',sans-serif;color:#ef4444;font-size:28px;letter-spacing:2px;margin-bottom:12px;}
    p{color:rgba(255,255,255,0.4);font-size:14px;line-height:1.7;}strong{color:rgba(255,255,255,0.7);}
    a{display:inline-block;margin-top:20px;background:#f59e0b;color:#0c0d10;padding:10px 24px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px;}</style></head>
    <body><div class="box"><h2>Link Expired</h2>
    <p>This login link has expired or already been used.<br><br>
    Send <strong>login</strong> to the bot on WhatsApp to get a new one.</p>
    <a href="/dashboard">Go to dashboard</a></div></body></html>""", 401


# ── Username/password + magic request login ───────────────────────────────────
@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.json or {}
    login_id = data.get("login", "").strip()
    password = data.get("password", "").strip()

    if not login_id or not password:
        return jsonify({"ok": False, "error": "Please fill in both fields"}), 400

    company = find_company(login_id)
    if not company:
        return jsonify({"ok": False, "error": "No account found. Check your username or number."}), 404

    stored_pw = company.get("dashboard_password", "")
    if not stored_pw:
        return jsonify({"ok": False, "error": "No password set. Use the magic link instead — send 'login' on WhatsApp."}), 401

    if not secrets.compare_digest(stored_pw, password):
        return jsonify({"ok": False, "error": "Wrong password. Try again or send 'login' on WhatsApp for a magic link."}), 401

    whatsapp = company.get("whatsapp_number", "").replace("whatsapp:", "")
    return jsonify({"ok": True, "whatsapp": whatsapp, "session_token": "__magic__",
                    "company_name": company.get("company_name", "")})


# ── Magic link request ────────────────────────────────────────────────────────
# Simple in-memory rate limit for magic-request (max 3 per number per 10 min)
_magic_attempts = {}

@auth_bp.route("/api/auth/magic-request", methods=["POST"])
def magic_request():
    from datetime import datetime, timezone, timedelta
    data     = request.json or {}
    login_id = data.get("login", "").strip()
    if not login_id:
        return jsonify({"ok": False, "error": "Enter your phone number or username"}), 400

    # Rate limit: max 3 magic link requests per 10 minutes per login_id
    now = datetime.now(timezone.utc)
    key = login_id.lower()
    attempts = _magic_attempts.get(key, [])
    attempts = [t for t in attempts if now - t < timedelta(minutes=10)]
    if len(attempts) >= 3:
        return jsonify({"ok": False, "error": "Too many attempts. Wait 10 minutes then try again."}), 429
    attempts.append(now)
    _magic_attempts[key] = attempts

    company = find_company(login_id)
    if not company:
        return jsonify({"ok": False, "error": "No account found for that number or username"}), 404

    whatsapp = company.get("whatsapp_number", "")
    if not whatsapp:
        return jsonify({"ok": False, "error": "No WhatsApp number on file"}), 400

    try:
        login_url = create_magic_token(whatsapp)
        client    = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=whatsapp,
            body=(
                "Your Note2Quote dashboard login link" + chr(10) + chr(10) +
                "Tap to open your dashboard instantly:" + chr(10) +
                login_url + chr(10) + chr(10) +
                "Or log in manually at note2quote.co.uk/dashboard:" + chr(10) +
                "Username: " + (company.get('username') or 'not set — use your mobile number') + chr(10) +
                "Password: " + (company.get('dashboard_password') or 'not set — use this link') + chr(10) +
                "Mobile: " + company.get('whatsapp_number','').replace('whatsapp:+44','07').replace('whatsapp:','') + chr(10) + chr(10) +
                "This link expires in 30 minutes. Send login anytime for a new one."
            )
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:100]}), 500


# ── Legacy auth check (for Stripe webhook + admin) ────────────────────────────
@auth_bp.route("/api/auth", methods=["POST"])
def auth_check():
    data = request.json or {}
    pw   = data.get("password", "")
    # Only admin/webhook use — not for end users
    if pw == os.environ.get("DASHBOARD_PASSWORD", "n2q2026"):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Unauthorized"}), 401
