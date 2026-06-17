"""
sitemanager/SMblueprint.py
Site Manager Command Centre — Flask Blueprint
Mounted at /smc/ prefix in the main app.py
Auth uses a signed cookie — no Flask session required.
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, make_response
from functools import wraps
import os, hmac, hashlib

smc_bp = Blueprint("smc", __name__, template_folder="templates")

# ── Supabase ──────────────────────────────────────────────────────────────────
from supabase import create_client
_url = os.environ.get("SUPABASE_URL", "")
_key = os.environ.get("SUPABASE_KEY", "")
supabase = create_client(_url, _key) if _url and _key else None

SMC_PROJECT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SMC_PASSWORD   = os.environ.get("SMC_PASSWORD", "sandle2026")
COOKIE_NAME    = "smc_token"


# ── Cookie-based auth (no Flask session needed) ───────────────────────────────
def _make_token():
    """Create a simple HMAC token from the password."""
    return hmac.new(SMC_PASSWORD.encode(), b"smc_auth", hashlib.sha256).hexdigest()


def _valid_cookie():
    token = request.cookies.get(COOKIE_NAME, "")
    expected = _make_token()
    return hmac.compare_digest(token, expected)


def _auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _valid_cookie():
            return redirect(url_for("smc.login"))
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ───────────────────────────────────────────────────────────────
@smc_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == SMC_PASSWORD:
            resp = make_response(redirect(url_for("smc.index")))
            resp.set_cookie(
                COOKIE_NAME,
                _make_token(),
                max_age=60 * 60 * 12,   # 12 hours
                httponly=True,
                samesite="Lax",
            )
            return resp
        error = "Incorrect password"
    return render_template("smc/SMlogin.html", error=error)


@smc_bp.route("/logout")
def logout():
    resp = make_response(redirect(url_for("smc.login")))
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ── App shell ─────────────────────────────────────────────────────────────────
@smc_bp.route("/")
@_auth
def index():
    return render_template("smc/SMapp.html")


# ── Helpers ───────────────────────────────────────────────────────────────────
def ok():
    return jsonify({"ok": True})

def err(e):
    return jsonify({"ok": False, "error": str(e)}), 500


# ── Project ───────────────────────────────────────────────────────────────────
@smc_bp.route("/api/project")
@_auth
def api_project():
    try:
        r = supabase.table("smc_projects").select("*").eq("id", SMC_PROJECT_ID).single().execute()
        return jsonify(r.data or {})
    except Exception as e:
        return err(e)


# ── Plots ─────────────────────────────────────────────────────────────────────
@smc_bp.route("/api/plots")
@_auth
def api_plots():
    try:
        r = supabase.table("smc_plots").select("*").eq("project_id", SMC_PROJECT_ID).order("plot_number").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/plots/<plot_id>", methods=["PATCH"])
@_auth
def api_plot_update(plot_id):
    data = request.get_json() or {}
    updates = {k: v for k, v in data.items() if k in {"programme_start_day", "map_x", "map_y"}}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields"}), 400
    try:
        supabase.table("smc_plots").update(updates).eq("id", plot_id).execute()
        return ok()
    except Exception as e:
        return err(e)


# ── Stage definitions ─────────────────────────────────────────────────────────
@smc_bp.route("/api/stage-defs")
@_auth
def api_stage_defs():
    try:
        r = supabase.table("smc_stage_defs").select("*").eq("project_id", SMC_PROJECT_ID).order("stage_order").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


# ── Plot stages ───────────────────────────────────────────────────────────────
@smc_bp.route("/api/plot-stages")
@_auth
def api_plot_stages():
    try:
        r = supabase.table("smc_plot_stages").select("*").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/plot-stages", methods=["POST"])
@_auth
def api_plot_stages_upsert():
    stages = (request.get_json() or {}).get("stages", [])
    if not stages:
        return jsonify({"ok": False, "error": "No stages"}), 400
    try:
        for s in stages:
            existing = supabase.table("smc_plot_stages").select("id") \
                .eq("plot_id", s["plot_id"]).eq("stage_def_id", s["stage_def_id"]).execute()
            row = {
                "plot_id":        s["plot_id"],
                "stage_def_id":   s["stage_def_id"],
                "start_day":      s["start_day"],
                "duration":       s["duration"],
                "baseline_start": s.get("baseline_start", s["start_day"]),
                "status":         s.get("status", "pending"),
            }
            if existing.data:
                supabase.table("smc_plot_stages").update(row).eq("id", existing.data[0]["id"]).execute()
            else:
                supabase.table("smc_plot_stages").insert(row).execute()
        return ok()
    except Exception as e:
        return err(e)


@smc_bp.route("/api/plot-stages/<stage_id>", methods=["PATCH"])
@_auth
def api_plot_stage_patch(stage_id):
    data = request.get_json() or {}
    updates = {k: v for k, v in data.items() if k in {"start_day", "duration", "status", "notes"}}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields"}), 400
    try:
        from datetime import datetime, timezone
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        supabase.table("smc_plot_stages").update(updates).eq("id", stage_id).execute()
        return ok()
    except Exception as e:
        return err(e)


# ── H&S inspections ───────────────────────────────────────────────────────────
@smc_bp.route("/api/hs-inspections")
@_auth
def api_hs_inspections():
    try:
        r = supabase.table("smc_hs_inspections").select("*") \
            .eq("project_id", SMC_PROJECT_ID).order("next_date").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/hs-inspections/<insp_id>", methods=["PATCH"])
@_auth
def api_hs_inspection_update(insp_id):
    data = request.get_json() or {}
    updates = {k: v for k, v in data.items() if k in {"last_date", "next_date", "responsible_person"}}
    try:
        supabase.table("smc_hs_inspections").update(updates).eq("id", insp_id).execute()
        return ok()
    except Exception as e:
        return err(e)


# ── H&S log ───────────────────────────────────────────────────────────────────
@smc_bp.route("/api/hs-log")
@_auth
def api_hs_log():
    try:
        r = supabase.table("smc_hs_log").select("*") \
            .eq("project_id", SMC_PROJECT_ID).order("log_date", desc=True).execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/hs-log", methods=["POST"])
@_auth
def api_hs_log_create():
    data = request.get_json() or {}
    data["project_id"] = SMC_PROJECT_ID
    row = {k: v for k, v in data.items() if k in
           {"project_id", "log_date", "log_type", "person_name", "status", "detail", "severity"}}
    try:
        supabase.table("smc_hs_log").insert(row).execute()
        return ok()
    except Exception as e:
        return err(e)


# ── Map image ─────────────────────────────────────────────────────────────────
@smc_bp.route("/api/map-image", methods=["GET"])
@_auth
def api_map_image_get():
    try:
        r = supabase.table("smc_map_image").select("*") \
            .eq("project_id", SMC_PROJECT_ID).limit(1).execute()
        return jsonify(r.data[0] if r.data else {})
    except Exception as e:
        return err(e)


@smc_bp.route("/api/map-image", methods=["POST"])
@_auth
def api_map_image_set():
    image_url = (request.get_json() or {}).get("image_url", "")
    try:
        existing = supabase.table("smc_map_image").select("id") \
            .eq("project_id", SMC_PROJECT_ID).execute()
        if existing.data:
            supabase.table("smc_map_image").update({"image_url": image_url}) \
                .eq("project_id", SMC_PROJECT_ID).execute()
        else:
            supabase.table("smc_map_image").insert(
                {"project_id": SMC_PROJECT_ID, "image_url": image_url}
            ).execute()
        return ok()
    except Exception as e:
        return err(e)
