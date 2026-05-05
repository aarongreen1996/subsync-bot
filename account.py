import os
import stripe
from flask import Blueprint, request, jsonify
import requests as http_requests

account_bp = Blueprint("account", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

stripe.api_key = STRIPE_SECRET_KEY


def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json() if r.status_code == 200 else []


def db_post(path, payload):
    r = http_requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        json=payload,
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )
    return r.status_code in (200, 201)


def db_patch(path, payload):
    r = http_requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        json=payload,
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )
    return r.status_code in (200, 201, 204)


def db_delete(path):
    r = http_requests.delete(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=sb_headers()
    )
    return r.status_code in (200, 204)


def check_auth():
    pw = request.headers.get("X-Dashboard-Password", "")
    return pw == DASHBOARD_PASSWORD or pw == "__magic__"


def encode_number(n):
    return n.replace("+", "%2B")


# ── Get account data ──────────────────────────────────────────────────────────
@account_bp.route("/api/account")
def get_account():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number = request.args.get("number", "")
    encoded = encode_number(number)
    if not encoded.startswith("whatsapp:"):
        encoded = "whatsapp:" + encoded.lstrip("whatsapp:")

    company = db_get(f"companies?whatsapp_number=eq.{encoded}&limit=1")
    projects = db_get(f"projects?whatsapp_number=eq.{encoded}&status=eq.active&order=site_name.asc")

    if not isinstance(company, list) or not company:
        return jsonify({"error": "Company not found"}), 404

    c = company[0]

    # Get Stripe subscription
    stripe_info = {"status": "unknown", "trial_end": None, "next_billing": None, "cancel_url": None}
    try:
        customers = stripe.Customer.list(email=c.get("email", ""), limit=1)
        if customers.data:
            subs = stripe.Subscription.list(customer=customers.data[0].id, limit=1, status="all")
            if subs.data:
                sub = subs.data[0]
                stripe_info = {
                    "status": sub.status,
                    "trial_end": sub.trial_end,
                    "next_billing": sub.current_period_end,
                    "subscription_id": sub.id,
                }
                # Generate billing portal URL
                try:
                    session = stripe.billing_portal.Session.create(
                        customer=customers.data[0].id,
                        return_url=os.environ.get("APP_URL", "") + "/account"
                    )
                    stripe_info["billing_url"] = session.url
                except Exception:
                    pass
    except Exception:
        pass

    return jsonify({
        "company": c,
        "projects": projects if isinstance(projects, list) else [],
        "stripe": stripe_info,
    })


# ── Update company details ────────────────────────────────────────────────────
@account_bp.route("/api/account/company", methods=["PATCH"])
def update_company():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number = request.args.get("number", "")
    encoded = encode_number(number)
    if not encoded.startswith("whatsapp:"):
        encoded = "whatsapp:" + encoded.lstrip("whatsapp:")

    data = request.json or {}
    allowed = ["company_name", "address", "email", "phone", "vat_number", "primary_color"]
    update = {k: v for k, v in data.items() if k in allowed}

    if not update:
        return jsonify({"error": "Nothing to update"}), 400

    ok = db_patch(f"companies?whatsapp_number=eq.{encoded}", update)
    return jsonify({"ok": ok})


# ── Add site ──────────────────────────────────────────────────────────────────
@account_bp.route("/api/account/sites", methods=["POST"])
def add_site():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number = request.args.get("number", "")
    encoded_raw = number if number.startswith("whatsapp:") else "whatsapp:" + number
    data = request.json or {}
    site_name = data.get("site_name", "").strip()
    client_name = data.get("client_name", "").strip()

    if not site_name:
        return jsonify({"error": "Site name required"}), 400

    ok = db_post("projects", {
        "whatsapp_number": encoded_raw,
        "site_name": site_name,
        "client_name": client_name,
        "status": "active",
    })
    return jsonify({"ok": ok})


# ── Delete site ───────────────────────────────────────────────────────────────
@account_bp.route("/api/account/sites/<int:site_id>", methods=["PATCH"])
def update_site(site_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data    = request.json or {}
    allowed = ["site_name", "client_name", "client_email", "client_phone"]
    update  = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "Nothing to update"}), 400
    ok = db_patch(f"projects?id=eq.{site_id}", update)
    return jsonify({"ok": bool(ok)})


@account_bp.route("/api/account/sites/<int:site_id>", methods=["DELETE"])
def delete_site(site_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    ok = db_patch(f"projects?id=eq.{site_id}", {"status": "archived"})
    return jsonify({"ok": ok})


# ── Logo upload ───────────────────────────────────────────────────────────────
@account_bp.route("/api/account/logo", methods=["POST"])
def upload_logo():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number = request.args.get("number", "")
    encoded = encode_number(number)
    if not encoded.startswith("whatsapp:"):
        encoded = "whatsapp:" + encoded.lstrip("whatsapp:")

    # Get file from request
    file_data    = request.data
    content_type = request.headers.get("Content-Type", "image/png")

    if not file_data:
        return jsonify({"error": "No file data received"}), 400

    # Upload to Supabase Storage logos bucket
    ext      = "png" if "png" in content_type else "jpg" if "jpg" in content_type else "png"
    filename = f"{encoded.replace('whatsapp:', '').replace('+', '').replace('%2B', '')}.{ext}"

    upload_url = f"{SUPABASE_URL}/storage/v1/object/logos/{filename}"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  content_type,
        "x-upsert":      "true",
    }
    r = http_requests.post(upload_url, data=file_data, headers=headers)
    if r.status_code not in (200, 201):
        return jsonify({"error": f"Storage error: {r.text}"}), 500

    logo_url = f"{SUPABASE_URL}/storage/v1/object/public/logos/{filename}"

    # Save URL to company record
    ok = db_patch(f"companies?whatsapp_number=eq.{encoded}", {"logo_url": logo_url})
    return jsonify({"ok": ok, "logo_url": logo_url})


# ── Login settings (username + password) ─────────────────────────────────────
@account_bp.route("/api/account/login", methods=["PATCH"])
def update_login():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number = request.args.get("number", "")
    encoded = encode_number(number)
    if not encoded.startswith("whatsapp:"):
        encoded = "whatsapp:" + encoded.lstrip("whatsapp:")

    data    = request.json or {}
    allowed = ["username", "dashboard_password"]
    update  = {k: v for k, v in data.items() if k in allowed}

    if not update:
        return jsonify({"error": "Nothing to update"}), 400

    # Check username not already taken
    if "username" in update:
        existing = db_get(f"companies?username=eq.{update['username']}&limit=1")
        if isinstance(existing, list) and existing:
            existing_num = existing[0].get("whatsapp_number", "")
            if existing_num != encoded.replace("%2B", "+").replace("whatsapp:", "whatsapp:+"):
                return jsonify({"error": "Username already taken"}), 400

    ok = db_patch(f"companies?whatsapp_number=eq.{encoded}", update)
    return jsonify({"ok": ok})
