# portal.py — Perfect Delivery portal: login, dashboard, API routes
import os
import json
import uuid
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Blueprint, request, render_template, jsonify,
    redirect, url_for, make_response, abort
)
from supabase import create_client, Client

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")
ADMIN_KEY     = os.environ.get("PD_ADMIN_KEY", "change-me")
SESSION_DAYS  = 30

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

portal_bp = Blueprint("portal", __name__, template_folder="templates", url_prefix="/pd/portal")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _get_tenant() -> dict:
    try:
        res = supabase.table("pd_tenants").select("*").limit(1).execute()
        return res.data[0] if res.data else {}
    except Exception:
        return {}


def _get_current_user():
    """Get user from session cookie. Returns user dict or None."""
    token = request.cookies.get("pd_session")
    if not token:
        return None
    try:
        now = datetime.now(timezone.utc).isoformat()
        res = supabase.table("pd_sessions").select(
            "*, pd_users(*)"
        ).eq("token", token).gt("expires_at", now).single().execute()
        if not res.data:
            return None
        user = res.data["pd_users"]
        user["session_token"] = token
        return user
    except Exception:
        return None


def _require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_current_user()
        if not user:
            return redirect("/pd/portal")
        return f(*args, user=user, **kwargs)
    return decorated


def _require_admin_role(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_current_user()
        if not user:
            return redirect("/pd/portal")
        if user.get("role") != "tenant_admin":
            return jsonify({"ok": False, "error": "Admin access required"}), 403
        return f(*args, user=user, **kwargs)
    return decorated


def _user_site_filter(user: dict, query):
    """Apply site filter for non-admin users."""
    if user.get("role") == "tenant_admin":
        return query
    site_ids = user.get("site_ids") or []
    if isinstance(site_ids, str):
        try:
            site_ids = json.loads(site_ids)
        except Exception:
            site_ids = []
    if site_ids:
        query = query.in_("site_id", site_ids)
    else:
        # No sites assigned — return nothing
        query = query.eq("id", "00000000-0000-0000-0000-000000000000")
    return query


# ── Auth routes ───────────────────────────────────────────────────────────────

@portal_bp.route("", methods=["GET"])
@portal_bp.route("/", methods=["GET"])
def login_page():
    user = _get_current_user()
    if user:
        return redirect("/pd/portal/dashboard")
    tenant = _get_tenant()
    return render_template("pd/portal_login.html", tenant=tenant)


@portal_bp.route("/auth", methods=["POST"])
def auth():
    data     = request.get_json() or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"ok": False, "error": "Email and password are required"}), 400

    pw_hash = _hash_password(password)
    try:
        res = supabase.table("pd_users").select("*").eq("email", email).eq("password_hash", pw_hash).single().execute()
    except Exception:
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401

    if not res.data:
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401

    user = res.data

    # Create session
    token      = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    supabase.table("pd_sessions").insert({
        "user_id":    user["id"],
        "token":      token,
        "expires_at": expires_at,
    }).execute()

    # Update last login
    supabase.table("pd_users").update({
        "last_login": datetime.now(timezone.utc).isoformat()
    }).eq("id", user["id"]).execute()

    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie(
        "pd_session", token,
        httponly=True, samesite="Lax",
        max_age=SESSION_DAYS * 86400,
        secure=os.environ.get("FLASK_ENV") != "development",
    )
    return resp


@portal_bp.route("/signout", methods=["POST"])
def signout():
    token = request.cookies.get("pd_session")
    if token:
        try:
            supabase.table("pd_sessions").delete().eq("token", token).execute()
        except Exception:
            pass
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("pd_session")
    return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@portal_bp.route("/dashboard")
@_require_login
def dashboard(user):
    tenant = _get_tenant()
    # Get sites for user modal
    if user.get("role") == "tenant_admin":
        sites_res = supabase.table("pd_sites").select("id, name").order("name").execute()
    else:
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        sites_res = supabase.table("pd_sites").select("id, name").in_("id", site_ids).execute() if site_ids else type('obj', (object,), {'data': []})()

    sites = sites_res.data or []
    sites_json = json.dumps(sites)

    from .checklist_data import STAGES
    stages_json = json.dumps({
        str(k): {"name": v["name"]}
        for k, v in STAGES.items()
    })

    return render_template(
        "pd/portal_dashboard.html",
        user=user,
        tenant=tenant,
        sites=sites,
        sites_json=sites_json,
        stages_json=stages_json,
        admin_key=ADMIN_KEY,
    )


# ── Plot progress (portal version) ────────────────────────────────────────────

@portal_bp.route("/plot/<plot_id>/progress")
@_require_login
def plot_progress(user, plot_id):
    res = supabase.table("pd_plots").select("*, pd_sites(*)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    site = plot["pd_sites"]

    # Check access
    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        if plot["site_id"] not in site_ids:
            abort(403)

    from .checklist_data import STAGES, STAGE_GROUPS
    stages_json = json.dumps({
        str(k): {"name": v["name"], "applies_to": v["applies_to"]}
        for k, v in STAGES.items()
    })
    groups_json = json.dumps([
        {"name": group_name, "stages": stage_nums}
        for group_name, stage_nums in STAGE_GROUPS.items()
    ])

    return render_template(
        "pd/plot_progress.html",
        plot=plot,
        site=site,
        stages_json=stages_json,
        groups_json=groups_json,
        admin_key=ADMIN_KEY,
    )


# ── API routes ────────────────────────────────────────────────────────────────

@portal_bp.route("/api/sites")
@_require_login
def api_sites(user):
    if user.get("role") == "tenant_admin":
        res = supabase.table("pd_sites").select("*").order("name").execute()
    else:
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        if not site_ids:
            return jsonify([])
        res = supabase.table("pd_sites").select("*").in_("id", site_ids).execute()
    sites = res.data or []

    # Add plot count per site
    for site in sites:
        plots_res = supabase.table("pd_plots").select("id", count="exact").eq("site_id", site["id"]).execute()
        site["plot_count"] = plots_res.count or 0

    return jsonify(sites)


@portal_bp.route("/api/plots")
@_require_login
def api_plots(user):
    site_id = request.args.get("site_id")
    query = supabase.table("pd_plots").select("*").order("plot_number")
    if site_id:
        query = query.eq("site_id", site_id)
    res = query.execute()
    plots = res.data or []

    # Add approved stage count per plot
    for plot in plots:
        subs_res = supabase.table("pd_submissions").select("stage_number, status").eq("plot_id", plot["id"]).eq("status", "approved").execute()
        plot["approved_count"] = len(set(s["stage_number"] for s in (subs_res.data or [])))

    return jsonify(plots)


@portal_bp.route("/api/submissions")
@_require_login
def api_submissions(user):
    status = request.args.get("status")
    limit  = int(request.args.get("limit", 50))

    query = supabase.table("pd_submissions").select(
        "*, pd_plots(plot_number, site_id, pd_sites(name))"
    ).order("submitted_at", desc=True).limit(limit)

    if status:
        query = query.eq("status", status)

    res = query.execute()
    subs = res.data or []

    # Filter by site access for non-admins
    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        subs = [s for s in subs if s.get("pd_plots", {}).get("site_id") in site_ids]

    return jsonify(subs)


@portal_bp.route("/api/photos")
@_require_login
def api_photos(user):
    site_id      = request.args.get("site_id")
    stage_number = request.args.get("stage_number")
    limit        = int(request.args.get("limit", 200))

    # Get submissions filtered by access
    subs_query = supabase.table("pd_submissions").select(
        "id, stage_number, stage_name, submitted_at, pd_plots(plot_number, site_id, pd_sites(name))"
    ).order("stage_number")

    if stage_number:
        subs_query = subs_query.eq("stage_number", int(stage_number))

    subs_res = subs_query.execute()
    subs = subs_res.data or []

    # Filter by site access
    if user.get("role") != "tenant_admin":
        site_ids = user.get("site_ids") or []
        if isinstance(site_ids, str):
            try: site_ids = json.loads(site_ids)
            except: site_ids = []
        subs = [s for s in subs if s.get("pd_plots", {}).get("site_id") in site_ids]

    if site_id:
        subs = [s for s in subs if s.get("pd_plots", {}).get("site_id") == site_id]

    if not subs:
        return jsonify([])

    sub_ids = [s["id"] for s in subs]
    # Batch fetch photos
    photos_res = supabase.table("pd_photos").select("*").in_("submission_id", sub_ids).limit(limit).execute()
    photos = photos_res.data or []

    # Enrich photos with submission metadata
    sub_map = {s["id"]: s for s in subs}
    for p in photos:
        sub = sub_map.get(p["submission_id"], {})
        p["stage_name"]   = sub.get("stage_name", "")
        p["stage_number"] = sub.get("stage_number", 0)
        p["submitted_at"] = sub.get("submitted_at", "")
        p["plot_number"]  = sub.get("pd_plots", {}).get("plot_number", "")
        p["site_name"]    = sub.get("pd_plots", {}).get("pd_sites", {}).get("name", "")

    photos.sort(key=lambda p: (p.get("stage_number", 0), p.get("submitted_at", "")))
    return jsonify(photos)


# ── User management API ───────────────────────────────────────────────────────

@portal_bp.route("/api/users", methods=["GET"])
@_require_admin_role
def api_users_get(user):
    tenant = _get_tenant()
    if not tenant.get("id"):
        return jsonify([])
    res = supabase.table("pd_users").select(
        "id, name, email, role, site_ids, created_at, last_login"
    ).eq("tenant_id", tenant["id"]).order("name").execute()
    return jsonify(res.data or [])


@portal_bp.route("/api/users", methods=["POST"])
@_require_admin_role
def api_users_create(user):
    data = request.get_json() or {}
    tenant = _get_tenant()
    if not tenant.get("id"):
        return jsonify({"ok": False, "error": "No tenant found"}), 400

    name     = (data.get("name") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role     = data.get("role") or "site_manager"
    site_ids = data.get("site_ids") or []

    if not name or not email or not password:
        return jsonify({"ok": False, "error": "Name, email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400

    try:
        res = supabase.table("pd_users").insert({
            "tenant_id":     tenant["id"],
            "name":          name,
            "email":         email,
            "password_hash": _hash_password(password),
            "role":          role,
            "site_ids":      json.dumps(site_ids),
        }).execute()
        return jsonify({"ok": True, "user": res.data[0]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@portal_bp.route("/api/users/<user_id>", methods=["PUT"])
@_require_admin_role
def api_users_update(user, user_id):
    data     = request.get_json() or {}
    updates  = {}
    if data.get("name"):     updates["name"]     = data["name"].strip()
    if data.get("email"):    updates["email"]    = data["email"].strip().lower()
    if data.get("role"):     updates["role"]     = data["role"]
    if data.get("site_ids") is not None: updates["site_ids"] = json.dumps(data["site_ids"])
    if data.get("password"):
        if len(data["password"]) < 8:
            return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400
        updates["password_hash"] = _hash_password(data["password"])

    if not updates:
        return jsonify({"ok": False, "error": "Nothing to update"}), 400

    supabase.table("pd_users").update(updates).eq("id", user_id).execute()
    return jsonify({"ok": True})


@portal_bp.route("/api/users/<user_id>", methods=["DELETE"])
@_require_admin_role
def api_users_delete(user, user_id):
    # Prevent self-deletion
    if user_id == user.get("id"):
        return jsonify({"ok": False, "error": "You cannot delete your own account"}), 400
    supabase.table("pd_users").delete().eq("id", user_id).execute()
    return jsonify({"ok": True})
