import os
import secrets
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, redirect
import requests as http_requests
from urllib.parse import quote

auth_bp = Blueprint("auth", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
APP_URL      = os.environ.get("APP_URL", "https://www.subsync.xyz")


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


def create_magic_token(whatsapp_number):
    """Generate a secure token, store it, return the login URL."""
    token = secrets.token_urlsafe(32)
    # Store expiry as unix timestamp (simpler to compare)
    expires_at = int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp())

    db_post("auth_tokens", {
        "token":      token,
        "whatsapp":   whatsapp_number,
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        "used":       False,
    })

    return f"{APP_URL}/login?token={token}"


def verify_token(token):
    """Check token is valid. Returns whatsapp number or None."""
    # Get the token record regardless of expiry first
    results = db_get(f"auth_tokens?token=eq.{token}&limit=1")

    if not isinstance(results, list) or not results:
        return None

    record = results[0]

    # Check if used
    if record.get("used"):
        return None

    # Check expiry manually
    expires_raw = record.get("expires_at", "")
    try:
        if expires_raw.endswith("+00"):
            expires_raw = expires_raw + ":00"
        expires_dt = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_dt:
            return None
    except Exception:
        pass  # If we can't parse, allow it through

    return record.get("whatsapp")


# ── Magic link login route ────────────────────────────────────────────────────
@auth_bp.route("/login")
def magic_login():
    token = request.args.get("token", "")
    if not token:
        return "<h2>Invalid link</h2>", 400

    whatsapp = verify_token(token)
    if not whatsapp:
        return """<!DOCTYPE html><html><head><meta charset="UTF-8">
        <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,900&family=DM+Sans:wght@400;600&display=swap" rel="stylesheet">
        <style>
          body{font-family:'DM Sans',sans-serif;background:#fff3e0;min-height:100vh;
               display:flex;align-items:center;justify-content:center;}
          .box{background:white;border-radius:16px;padding:48px;text-align:center;
               max-width:400px;border:1px solid #e8ddd0;}
          h2{font-family:'Fraunces',serif;color:#d62828;font-size:28px;margin-bottom:12px;}
          p{color:#8a7560;font-weight:300;line-height:1.6;}
        </style></head><body>
        <div class="box"><h2>Link expired</h2>
        <p>This login link has expired or already been used.<br><br>
        Send <strong>my dashboard</strong> to the SubSync bot on WhatsApp to get a new one.</p>
        </div></body></html>""", 401

    # Clean number for use as identifier
    number = whatsapp.replace("whatsapp:", "")

    # Redirect to dashboard with auto-login
    return redirect(f"/dashboard?autologin={number}")
