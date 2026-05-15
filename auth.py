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
    Validate a magic link token and redirect to the dashboard
    with the phone number as ?autologin= so the JS can auto-log in.
    """
    token = request.args.get("token", "").strip()
    if not token:
        return redirect(f"{APP_URL}/dashboard")

    # Look up the token
    rows = db_get(f"auth_tokens?token=eq.{token}&used=eq.false&limit=1")

    if not isinstance(rows, list) or not rows:
        # Token not found or already used — redirect to dashboard login screen
        return redirect(f"{APP_URL}/dashboard?expired=1")

    row = rows[0]

    # Check expiry
    try:
        expires_at = datetime.fromisoformat(
            row.get("expires_at", "").replace("Z", "+00:00")
        )
        if datetime.now(timezone.utc) > expires_at:
            return redirect(f"{APP_URL}/dashboard?expired=1")
    except Exception:
        pass

    # Don't mark as used yet — let the dashboard JS call /api/validate-token
    # which marks it used after confirming. This handles redirects that
    # WhatsApp's browser follows before the user sees the page.

    # Pass token to dashboard so JS can validate it client-side
    return redirect(f"{APP_URL}/dashboard?token={token}")


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
