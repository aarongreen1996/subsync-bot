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
    return r.status_code in (200, 201, 204)


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
    """Normalise UK number and URL-encode for Supabase query strings.
    Returns whatsapp:%2B44XXXXXXXXX — the + MUST be %2B or Supabase reads it as a space."""
    n = n.strip()
    if n.startswith("whatsapp:"):
        n = n[9:]
    n = n.replace(" ", "").replace("-", "")
    if n.startswith("07") and len(n) == 11:
        n = "+44" + n[1:]
    if n.startswith("44") and not n.startswith("+"):
        n = "+" + n
    if not n.startswith("+"):
        n = "+" + n
    # URL-encode the + so Supabase doesn't read it as a space
    return "whatsapp:" + n.replace("+", "%2B")


def raw_number(n):
    """Return the raw whatsapp:+44... format for storing in DB (not URL-encoded)."""
    n = n.strip()
    if n.startswith("whatsapp:"):
        n = n[9:]
    n = n.replace(" ", "").replace("-", "").replace("%2B", "+")
    if n.startswith("07") and len(n) == 11:
        n = "+44" + n[1:]
    if n.startswith("44") and not n.startswith("+"):
        n = "+" + n
    if not n.startswith("+"):
        n = "+" + n
    return "whatsapp:" + n


# ── Get account data ──────────────────────────────────────────────────────────
@account_bp.route("/api/account")
def get_account():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number  = request.args.get("number", "")
    encoded = encode_number(number)  # URL-encoded for queries

    company  = db_get(f"companies?whatsapp_number=eq.{encoded}&limit=1")
    projects = db_get(f"projects?whatsapp_number=eq.{encoded}&status=eq.active&order=site_name.asc")

    if not isinstance(company, list) or not company:
        return jsonify({"error": "Company not found"}), 404

    c = company[0]

    # Stripe subscription info
    stripe_info = {"status": "unknown", "trial_end": None, "next_billing": None}
    try:
        customers = stripe.Customer.list(email=c.get("email", ""), limit=1)
        if customers.data:
            subs = stripe.Subscription.list(customer=customers.data[0].id, limit=1, status="all")
            if subs.data:
                sub = subs.data[0]
                stripe_info = {
                    "status":      sub.status,
                    "trial_end":   sub.trial_end,
                    "next_billing": sub.current_period_end,
                    "subscription_id": sub.id,
                }
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
        "company":  c,
        "projects": projects if isinstance(projects, list) else [],
        "stripe":   stripe_info,
    })


# ── Update company details ────────────────────────────────────────────────────
@account_bp.route("/api/account/company", methods=["PATCH"])
def update_company():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number  = request.args.get("number", "")
    encoded = encode_number(number)
    data    = request.json or {}
    allowed = ["company_name", "address", "email", "phone", "vat_number", "primary_color"]
    update  = {k: v for k, v in data.items() if k in allowed}

    if not update:
        return jsonify({"error": "Nothing to update"}), 400

    ok = db_patch(f"companies?whatsapp_number=eq.{encoded}", update)
    return jsonify({"ok": ok})


# ── Add site ──────────────────────────────────────────────────────────────────
@account_bp.route("/api/account/sites", methods=["POST"])
def add_site():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number    = request.args.get("number", "")
    wa_raw    = raw_number(number)  # store as whatsapp:+44... (not URL-encoded)
    data      = request.json or {}
    site_name = data.get("site_name", "").strip()
    client_name = data.get("client_name", "").strip()

    if not site_name:
        return jsonify({"error": "Site name required"}), 400

    ok = db_post("projects", {
        "whatsapp_number": wa_raw,
        "site_name":       site_name,
        "client_name":     client_name,
        "status":          "active",
    })
    return jsonify({"ok": ok})


# ── Update site ───────────────────────────────────────────────────────────────
@account_bp.route("/api/account/sites/<int:site_id>", methods=["PATCH"])
def update_site(site_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number  = request.args.get("number", "")
    encoded = encode_number(number)

    # Verify ownership — use URL-encoded number in query
    site = db_get(f"projects?id=eq.{site_id}&whatsapp_number=eq.{encoded}&limit=1")
    if not isinstance(site, list) or not site:
        return jsonify({"error": "Site not found or access denied"}), 403

    data    = request.json or {}
    allowed = ["site_name", "client_name", "client_email", "client_phone", "client_address"]
    update  = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "Nothing to update"}), 400

    ok = db_patch(f"projects?id=eq.{site_id}", update)
    return jsonify({"ok": bool(ok)})


# ── Delete site ───────────────────────────────────────────────────────────────
@account_bp.route("/api/account/sites/<int:site_id>", methods=["DELETE"])
def delete_site(site_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number  = request.args.get("number", "")
    encoded = encode_number(number)

    site = db_get(f"projects?id=eq.{site_id}&whatsapp_number=eq.{encoded}&limit=1")
    if not isinstance(site, list) or not site:
        return jsonify({"error": "Site not found or access denied"}), 403

    ok = db_patch(f"projects?id=eq.{site_id}", {"status": "archived"})
    return jsonify({"ok": ok})


# ── Logo upload ───────────────────────────────────────────────────────────────
@account_bp.route("/api/account/logo", methods=["POST"])
def upload_logo():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number   = request.args.get("number", "")
    encoded  = encode_number(number)
    file_data    = request.data
    content_type = request.headers.get("Content-Type", "image/png")

    if not file_data:
        return jsonify({"error": "No file data received"}), 400
    if len(file_data) > 2 * 1024 * 1024:
        return jsonify({"error": "Logo must be under 2MB"}), 400

    allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
    if content_type.split(";")[0].strip() not in allowed_types:
        return jsonify({"error": "File must be an image (PNG, JPG, GIF or WebP)"}), 400

    ext      = "png" if "png" in content_type else "jpg"
    filename = encoded.replace("whatsapp:", "").replace("%2B", "").replace("+", "") + "." + ext

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
    ok = db_patch(f"companies?whatsapp_number=eq.{encoded}", {"logo_url": logo_url})
    return jsonify({"ok": ok, "logo_url": logo_url})


# ── Login settings ────────────────────────────────────────────────────────────
@account_bp.route("/api/account/login", methods=["PATCH"])
def update_login():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number  = request.args.get("number", "")
    encoded = encode_number(number)
    data    = request.json or {}
    allowed = ["username", "dashboard_password"]
    update  = {k: v for k, v in data.items() if k in allowed}

    if not update:
        return jsonify({"error": "Nothing to update"}), 400

    # Check username not already taken by someone else
    if "username" in update:
        existing = db_get(f"companies?username=eq.{update['username']}&limit=1")
        if isinstance(existing, list) and existing:
            existing_num = existing[0].get("whatsapp_number", "")
            # Compare normalised forms
            if encode_number(existing_num) != encoded:
                return jsonify({"error": "Username already taken"}), 400

    ok = db_patch(f"companies?whatsapp_number=eq.{encoded}", update)
    return jsonify({"ok": ok})
