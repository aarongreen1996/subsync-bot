import os
import secrets
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, redirect, jsonify
import requests as http_requests

auth_bp = Blueprint("auth", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
APP_URL      = os.environ.get("APP_URL", "https://www.note2quote.co.uk")


def sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return []
    return []


def db_patch(path, payload):
    http_requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        json=payload,
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )


def create_magic_token(whatsapp_number: str) -> str:
    """Create a 24hr magic login token and return the login URL."""
    token   = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    http_requests.post(
        f"{SUPABASE_URL}/rest/v1/auth_tokens",
        json={
            "token":      token,
            "whatsapp":   whatsapp_number,
            "expires_at": expires,
            "used":       False,
        },
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )
    return f"{APP_URL}/login?token={token}"


@auth_bp.route("/login")
def magic_login():
    """
    Validate token and serve dashboard HTML directly with autologin data
    injected — no redirect, so the token is never lost.
    """
    import os as _os
    token = request.args.get("token", "").strip()

    # If no token just serve the dashboard normally
    if not token:
        path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard.html")
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html"}

    # Look up token
    rows = db_get(f"auth_tokens?token=eq.{token}&used=eq.false&limit=1")

    if not isinstance(rows, list) or not rows:
        # Expired/invalid — serve dashboard with error flag injected
        path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("</head>", "<script>window.__magicError='expired';</script></head>", 1)
        return html, 200, {"Content-Type": "text/html"}

    row = rows[0]

    # Check expiry
    try:
        expires_at = datetime.fromisoformat(row.get("expires_at","").replace("Z","+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard.html")
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
            html = html.replace("</head>","<script>window.__magicError='expired';</script></head>",1)
            return html, 200, {"Content-Type": "text/html"}
    except Exception:
        pass

    # Mark as used
    # db_patch(f"auth_tokens?token=eq.{token}", {"used": True})

    # Get the phone number
    wa     = row.get("whatsapp", "")
    number = wa.replace("whatsapp:", "")  # e.g. +447711816351

    # Serve dashboard HTML with autologin data injected into <head>
    # This runs BEFORE any JS so the number is available immediately
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "dashboard.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    inject = f"<script>window.__magicNumber='{number}';</script>"
    html   = html.replace("</head>", inject + "</head>", 1)

    return html, 200, {"Content-Type": "text/html"}


@auth_bp.route("/api/auth/login", methods=["POST"])
def api_login():
    """Username/password login for dashboard."""
    data     = request.json or {}
    login    = data.get("login","").strip()
    password = data.get("password","").strip()

    if not login or not password:
        return jsonify({"ok":False,"error":"Fill in both fields."})

    # Try username first, then mobile number
    SURL = os.environ.get("SUPABASE_URL","").rstrip("/")
    SKEY = os.environ.get("SUPABASE_KEY","")
    heads = {"apikey":SKEY,"Authorization":f"Bearer {SKEY}"}

    # Search by username
    r = http_requests.get(f"{SURL}/rest/v1/companies?username=eq.{login}&limit=1", headers=heads)
    rows = r.json() if r.status_code == 200 else []

    # If not found, try by phone/whatsapp
    if not rows:
        # Normalise number
        n = login.replace(" ","").replace("-","")
        if n.startswith("07") and len(n)==11: n = "+44"+n[1:]
        if not n.startswith("+"): n = "+"+n
        wa = ("whatsapp:"+n).replace("+","%2B")
        r2 = http_requests.get(f"{SURL}/rest/v1/companies?whatsapp_number=eq.{wa}&limit=1", headers=heads)
        rows = r2.json() if r2.status_code == 200 else []

    if not rows:
        return jsonify({"ok":False,"error":"Account not found."})

    company = rows[0]
    stored_pw = company.get("dashboard_password","")

    if stored_pw != password:
        return jsonify({"ok":False,"error":"Wrong username or password."})

    wa = company.get("whatsapp_number","")
    return jsonify({"ok":True,"whatsapp":wa,"session_token":password})


@auth_bp.route("/api/magic-link", methods=["POST"])
def send_magic_link_api():
    """
    Dashboard calls this to send a new magic link via WhatsApp.
    Twilio send is handled by app.py; this just creates the token and returns the URL.
    """
    data   = request.json or {}
    number = data.get("number", "").strip()
    if not number:
        return jsonify({"error": "number required"}), 400

    # Normalise to whatsapp:+44... format
    n = number.replace(" ", "").replace("-", "")
    if n.startswith("07") and len(n) == 11:
        n = "+44" + n[1:]
    if not n.startswith("+"):
        n = "+" + n
    wa = "whatsapp:" + n

    url = create_magic_token(wa)
    return jsonify({"ok": True, "url": url, "number": n})
