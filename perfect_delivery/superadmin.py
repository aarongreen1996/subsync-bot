"""
superadmin.py — Perfect Delivery super-admin panel
Access: /pd/superadmin  (protected by PD_SUPER_KEY env var)
Manages: tenants, their admin users, usage stats, impersonation
"""
import os
import json
import secrets
import hashlib
from datetime import datetime, timezone, timedelta

from flask import (
    Blueprint, request, jsonify, render_template,
    redirect, session, make_response
)
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SUPER_KEY    = os.environ.get("PD_SUPER_KEY", "change-me-super")
COOKIE_NAME  = "pd_super_session"
SESSION_DAYS = 1

superadmin_bp = Blueprint(
    "superadmin", __name__,
    template_folder="templates",
    url_prefix="/pd/superadmin"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def _is_auth(req) -> bool:
    token = req.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        now = datetime.now(timezone.utc).isoformat()
        res = supabase.table("pd_super_sessions").select("id").eq(
            "token", token).gt("expires_at", now).execute()
        return bool(res.data)
    except Exception:
        return False

def _require_super(func):
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not _is_auth(request):
            return redirect("/pd/superadmin/login")
        return func(*args, **kwargs)
    return wrapper

def _get_stats(tenant_id: str) -> dict:
    try:
        sites_res = supabase.table("pd_sites").select("id", count="exact").eq("tenant_id", tenant_id).execute()
        site_count = sites_res.count or 0
        site_ids   = [s["id"] for s in (sites_res.data or [])]

        plot_count = 0
        sub_count  = 0
        if site_ids:
            plots_res = supabase.table("pd_plots").select("id", count="exact").in_("site_id", site_ids).execute()
            plot_count = plots_res.count or 0
            plot_ids   = [p["id"] for p in (plots_res.data or [])]
            if plot_ids:
                subs_res = supabase.table("pd_submissions").select("id", count="exact").in_("plot_id", plot_ids).execute()
                sub_count = subs_res.count or 0

        return {"sites": site_count, "plots": plot_count, "submissions": sub_count}
    except Exception:
        return {"sites": 0, "plots": 0, "submissions": 0}


# ── Auth routes ───────────────────────────────────────────────────────────────

@superadmin_bp.route("/login", methods=["GET"])
def login_page():
    if _is_auth(request):
        return redirect("/pd/superadmin")
    return render_template("pd/superadmin_login.html")


@superadmin_bp.route("/login", methods=["POST"])
def login_submit():
    data = request.get_json() or {}
    key  = data.get("key", "")
    if key != SUPER_KEY:
        return jsonify({"ok": False, "error": "Invalid key"}), 401

    token      = secrets.token_urlsafe(40)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()

    try:
        supabase.table("pd_super_sessions").insert({
            "token": token, "expires_at": expires_at
        }).execute()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie(
        COOKIE_NAME, token,
        httponly=True, secure=True, samesite="Lax",
        max_age=SESSION_DAYS * 86400
    )
    return resp


@superadmin_bp.route("/logout", methods=["POST"])
def logout():
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            supabase.table("pd_super_sessions").delete().eq("token", token).execute()
        except Exception:
            pass
    resp = make_response(redirect("/pd/superadmin/login"))
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ── Main dashboard ────────────────────────────────────────────────────────────

@superadmin_bp.route("/")
@superadmin_bp.route("")
@_require_super
def dashboard():
    tenants = supabase.table("pd_tenants").select("*").order("name").execute().data or []
    for t in tenants:
        t["stats"] = _get_stats(t["id"])
        # Get admin users for this tenant
        try:
            users = supabase.table("pd_users").select("id, name, email, role").eq(
                "tenant_id", t["id"]).execute().data or []
            t["users"] = users
        except Exception:
            t["users"] = []
    return render_template("pd/superadmin.html", tenants=tenants)


# ── Tenant API ────────────────────────────────────────────────────────────────

@superadmin_bp.route("/api/tenants", methods=["POST"])
@_require_super
def create_tenant():
    data = request.get_json() or {}
    name  = (data.get("name") or "").strip()
    email = (data.get("admin_email") or "").strip().lower()
    pw    = data.get("admin_password") or ""
    admin_name = (data.get("admin_name") or "").strip()

    if not name or not email or not pw or not admin_name:
        return jsonify({"ok": False, "error": "Name, admin name, email and password are required"}), 400
    if len(pw) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400

    try:
        # Create tenant
        tenant_res = supabase.table("pd_tenants").insert({
            "name": name,
            "contact_email": email,
        }).execute()
        if not tenant_res.data:
            return jsonify({"ok": False, "error": "Failed to create tenant"}), 500
        tenant = tenant_res.data[0]

        # Create tenant admin user
        supabase.table("pd_users").insert({
            "tenant_id":     tenant["id"],
            "name":          admin_name,
            "email":         email,
            "password_hash": _hash(pw),
            "role":          "tenant_admin",
        }).execute()

        return jsonify({"ok": True, "tenant_id": tenant["id"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@superadmin_bp.route("/api/tenants/<tenant_id>", methods=["PUT"])
@_require_super
def update_tenant(tenant_id):
    data = request.get_json() or {}
    updates = {}
    for f in ["name", "contact_email", "logo_url", "primary_color", "address", "phone"]:
        if f in data:
            updates[f] = data[f]
    if not updates:
        return jsonify({"ok": False, "error": "Nothing to update"}), 400
    supabase.table("pd_tenants").update(updates).eq("id", tenant_id).execute()
    return jsonify({"ok": True})


@superadmin_bp.route("/api/tenants/<tenant_id>", methods=["DELETE"])
@_require_super
def delete_tenant(tenant_id):
    # Soft delete — just mark inactive rather than cascade delete
    try:
        supabase.table("pd_tenants").update({"active": False}).eq("id", tenant_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── User management under tenant ──────────────────────────────────────────────

@superadmin_bp.route("/api/tenants/<tenant_id>/users", methods=["POST"])
@_require_super
def create_tenant_user(tenant_id):
    data  = request.get_json() or {}
    name  = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    pw    = data.get("password") or ""
    role  = data.get("role", "tenant_admin")

    if not name or not email or not pw:
        return jsonify({"ok": False, "error": "Name, email and password required"}), 400
    if len(pw) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400
    if role not in ["tenant_admin", "site_manager"]:
        return jsonify({"ok": False, "error": "Invalid role"}), 400

    try:
        supabase.table("pd_users").insert({
            "tenant_id":     tenant_id,
            "name":          name,
            "email":         email,
            "password_hash": _hash(pw),
            "role":          role,
        }).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@superadmin_bp.route("/api/users/<user_id>/reset", methods=["POST"])
@_require_super
def reset_user_password(user_id):
    data = request.get_json() or {}
    pw   = data.get("password") or ""
    if len(pw) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400
    supabase.table("pd_users").update({"password_hash": _hash(pw)}).eq("id", user_id).execute()
    supabase.table("pd_sessions").delete().eq("user_id", user_id).execute()
    return jsonify({"ok": True})


# ── Impersonate tenant ────────────────────────────────────────────────────────

@superadmin_bp.route("/api/tenants/<tenant_id>/impersonate", methods=["POST"])
@_require_super
def impersonate(tenant_id):
    """Create a short-lived portal session as the tenant admin and redirect."""
    try:
        res = supabase.table("pd_users").select("id").eq(
            "tenant_id", tenant_id).eq("role", "tenant_admin").limit(1).execute()
        if not res.data:
            return jsonify({"ok": False, "error": "No tenant admin found"}), 404

        user_id    = res.data[0]["id"]
        token      = secrets.token_urlsafe(40)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()

        supabase.table("pd_sessions").insert({
            "user_id":    user_id,
            "token":      token,
            "expires_at": expires_at,
        }).execute()

        resp = make_response(jsonify({"ok": True, "redirect": "/pd/portal/dashboard"}))
        resp.set_cookie(
            "pd_portal_session", token,
            httponly=True, secure=True, samesite="Lax",
            max_age=8 * 3600
        )
        return resp
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
