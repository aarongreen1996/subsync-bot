import os
import stripe
from flask import Blueprint, request, jsonify
import requests as http_requests
from datetime import datetime, timezone, timedelta

admin_bp = Blueprint("admin", __name__)

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")h
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

stripe.api_key = STRIPE_SECRET_KEY


def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}

def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json() if r.status_code == 200 else []

def check_auth():
    return request.headers.get("X-Admin-Password", "") == ADMIN_PASSWORD


# Auth
@admin_bp.route("/api/admin/auth", methods=["POST"])
def admin_auth():
    data = request.json or {}
    if data.get("password") == ADMIN_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401


# Overview
@admin_bp.route("/api/admin/overview")
def admin_overview():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    companies = db_get("companies?order=created_at.desc")
    if not isinstance(companies, list):
        companies = []

    all_logs  = db_get("site_logs?select=id,status,created_at,from_number")
    all_projs = db_get("projects?select=id,whatsapp_number,site_name,status")
    if not isinstance(all_logs,  list): all_logs  = []
    if not isinstance(all_projs, list): all_projs = []

    now = datetime.now(timezone.utc)

    customers    = []
    mrr          = 0
    active_count = 0
    trial_count  = 0

    # Pre-fetch Stripe data once (not per-company) — much faster
    stripe_by_email = {}
    try:
        if STRIPE_SECRET_KEY:
            # Get all active/trialing subscriptions in one call
            all_subs = stripe.Subscription.list(limit=100, status="all",
                                                  expand=["data.customer"])
            for sub in all_subs.data:
                cust = sub.customer if isinstance(sub.customer, dict) else {}
                email = cust.get("email", "") if cust else ""
                if email and email not in stripe_by_email:
                    stripe_by_email[email] = {"status": sub.status, "id": sub.id}
    except Exception:
        pass

    for c in companies:
        wn   = c.get("whatsapp_number", "")
        logs = [l for l in all_logs if l.get("from_number") == wn]
        sent = [l for l in logs if l.get("status") == "sent"]
        sites = [p for p in all_projs if p.get("whatsapp_number") == wn
                 and p.get("status") == "active"]

        # Last log date
        log_dates = [l.get("created_at") for l in logs if l.get("created_at")]
        last_log  = max(log_dates) if log_dates else None
        days_inactive = None
        if last_log:
            try:
                dt = datetime.fromisoformat(last_log.replace("Z", "+00:00"))
                days_inactive = (now - dt).days
            except Exception:
                pass
        elif logs == []:
            days_inactive = 999

        # Stripe status from pre-fetched data
        email         = c.get("email", "")
        stripe_info   = stripe_by_email.get(email, {})
        stripe_status = stripe_info.get("status", "unknown")
        stripe_sub_id = stripe_info.get("id")

        if stripe_status == "active":
            mrr          += 49
            active_count += 1
        elif stripe_status == "trialing":
            trial_count  += 1

        customers.append({
            "id":            c.get("id"),
            "company_name":  c.get("company_name", "Unknown"),
            "email":         email,
            "whatsapp_number": wn,
            "trade":         c.get("trade", ""),
            "created_at":    c.get("created_at"),
            "stripe_status": stripe_status,
            "stripe_sub_id": stripe_sub_id,
            "total_logs":    len(logs),
            "sent_docs":     len(sent),
            "sites":         len(sites),
            "last_log_date": last_log,
            "days_inactive": days_inactive,
        })

    return jsonify({
        "customers":       customers,
        "total_customers": len(customers),
        "active_count":    active_count,
        "trial_count":     trial_count,
        "mrr":             mrr,
        "total_logs":      len(all_logs),
        "total_sent":      len([l for l in all_logs if l.get("status") == "sent"]),
    })


# Health checks
@admin_bp.route("/api/admin/health")
def admin_health():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    results = {}

    # Anthropic
    try:
        r = http_requests.get("https://status.anthropic.com/api/v2/status.json", timeout=5)
        data = r.json()
        results["anthropic"] = data.get("status", {}).get("indicator", "none") == "none"
    except Exception:
        results["anthropic"] = False

    # Groq
    try:
        r = http_requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=5
        )
        results["groq"] = r.status_code == 200
    except Exception:
        results["groq"] = False

    # Twilio
    try:
        sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        r = http_requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
            auth=(sid, token), timeout=5
        )
        results["twilio"] = r.status_code == 200
    except Exception:
        results["twilio"] = False

    # Resend
    try:
        r = http_requests.get(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            timeout=5
        )
        results["resend"] = r.status_code == 200
    except Exception:
        results["resend"] = False

    return jsonify(results)


# Customer detail
@admin_bp.route("/api/admin/customer/<int:company_id>")
def admin_customer(company_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    companies = db_get(f"companies?id=eq.{company_id}&limit=1")
    if not isinstance(companies, list) or not companies:
        return jsonify({"error": "Not found"}), 404

    company  = companies[0]
    wn       = company.get("whatsapp_number", "")
    projects = db_get(f"projects?whatsapp_number=eq.{wn.replace('+','%2B')}&status=eq.active")
    logs     = db_get(f"site_logs?from_number=eq.{wn.replace('+','%2B')}&order=created_at.desc&limit=20")

    if not isinstance(projects, list): projects = []
    if not isinstance(logs,     list): logs     = []

    # Stripe sub ID
    stripe_sub_id = None
    try:
        email = company.get("email", "")
        if email and STRIPE_SECRET_KEY:
            custs = stripe.Customer.list(email=email, limit=1)
            if custs.data:
                subs = stripe.Subscription.list(
                    customer=custs.data[0].id, limit=1, status="all"
                )
                if subs.data:
                    stripe_sub_id = subs.data[0].id
    except Exception:
        pass

    return jsonify({
        "company":       company,
        "projects":      projects,
        "logs":          logs,
        "stripe_sub_id": stripe_sub_id,
    })


# Cancel subscription
@admin_bp.route("/api/admin/cancel-subscription", methods=["POST"])
def cancel_subscription():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data   = request.json or {}
    sub_id = data.get("subscription_id")
    if not sub_id:
        return jsonify({"error": "subscription_id required"}), 400

    try:
        stripe.Subscription.cancel(sub_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Send welcome message
@admin_bp.route("/api/admin/send-welcome", methods=["POST"])
def admin_send_welcome():
    data = request.json or {}
    # Accept either admin header auth or body password
    if not check_auth() and data.get("admin_password") != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401

    whatsapp     = data.get("whatsapp", "")
    company_name = data.get("company_name", "Your Company")
    if not whatsapp:
        return jsonify({"error": "whatsapp required"}), 400

    import secrets
    from datetime import datetime, timezone, timedelta
    from twilio.rest import Client as TwilioClient

    # Create 24hr magic link
    token   = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    http_requests.post(
        f"{SUPABASE_URL}/rest/v1/auth_tokens",
        json={"token": token, "whatsapp": whatsapp, "expires_at": expires, "used": False},
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )

    APP_URL   = os.environ.get("APP_URL", "https://www.note2quote.co.uk")
    login_url = f"{APP_URL}/login?token={token}"

    lines = [
        "Welcome to Note2Quote, " + company_name + "!",
        "",
        "You are all set up. Here is how to get started:",
        "",
        "YOUR DASHBOARD (tap to open):",
        login_url,
        "",
        "HOW TO LOG WORK - just send a WhatsApp message or voice note:",
        "Example: Extra sockets in kitchen, 2 hours, 80 quid, Brookfield Site",
        "Or hold the mic button and speak naturally.",
        "",
        "QUICK COMMANDS:",
        "summary - full overview",
        "pending - see outstanding items",
        "approve [site] - mark as approved",
        "generate variations for [site] - create a PDF",
        "login - get a new dashboard link",
        "help - all commands",
        "",
        "Good luck on site!"
    ]
    msg = chr(10).join(lines)

    try:
        TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
        TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
        TWILIO_FROM  = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(from_=TWILIO_FROM, to=whatsapp, body=msg)
        return jsonify({"ok": True, "login_url": login_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
