"""
sitemanager/SMblueprint.py
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, make_response, send_from_directory
from functools import wraps
import os, hashlib

smc_bp = Blueprint("smc", __name__, template_folder="templates")

from supabase import create_client
_url = os.environ.get("SUPABASE_URL", "")
_key = os.environ.get("SUPABASE_KEY", "")
supabase = create_client(_url, _key) if _url and _key else None

SMC_PROJECT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
SMC_PASSWORD   = os.environ.get("SMC_PASSWORD", "sandle2026")
COOKIE_NAME    = "smc_auth"


def _token():
    return hashlib.sha256(SMC_PASSWORD.encode()).hexdigest()


def _authed():
    return request.cookies.get(COOKIE_NAME) == _token()


def _require_auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _authed():
            return redirect(url_for("smc.login"))
        return f(*a, **kw)
    return wrap


# ── Auth ──────────────────────────────────────────────────────────────────────
@smc_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == SMC_PASSWORD:
            resp = make_response(redirect(url_for("smc.index")))
            resp.set_cookie(COOKIE_NAME, _token(), max_age=43200, httponly=True, samesite="Lax")
            return resp
        error = "Incorrect password"
    return render_template("smc/SMlogin.html", error=error)


@smc_bp.route("/logout")
def logout():
    resp = make_response(redirect(url_for("smc.login")))
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ── App shell — served as a raw static file, bypassing Jinja2 entirely ───────
@smc_bp.route("/")
@_require_auth
def index():
    # send_from_directory serves the file byte-for-byte — no Jinja processing.
    # This means React's {{ }} syntax is never touched by Jinja2.
    static_dir = os.path.join(os.path.dirname(__file__), "templates", "smc")
    return send_from_directory(static_dir, "SMapp.html")


# ── Helpers ───────────────────────────────────────────────────────────────────
def ok():    return jsonify({"ok": True})
def err(e):  return jsonify({"ok": False, "error": str(e)}), 500


# ── Project ───────────────────────────────────────────────────────────────────
@smc_bp.route("/api/project")
@_require_auth
def api_project():
    try:
        r = supabase.table("smc_projects").select("*").eq("id", SMC_PROJECT_ID).single().execute()
        return jsonify(r.data or {})
    except Exception as e:
        return err(e)


# ── Plots ─────────────────────────────────────────────────────────────────────
@smc_bp.route("/api/plots")
@_require_auth
def api_plots():
    try:
        r = supabase.table("smc_plots").select("*").eq("project_id", SMC_PROJECT_ID).order("plot_number").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/plots/<plot_id>", methods=["PATCH"])
@_require_auth
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


# ── Stage defs ────────────────────────────────────────────────────────────────
@smc_bp.route("/api/stage-defs")
@_require_auth
def api_stage_defs():
    try:
        r = supabase.table("smc_stage_defs").select("*").eq("project_id", SMC_PROJECT_ID).order("stage_order").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


# ── Plot stages ───────────────────────────────────────────────────────────────
@smc_bp.route("/api/plot-stages")
@_require_auth
def api_plot_stages():
    try:
        r = supabase.table("smc_plot_stages").select("*").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/plot-stages", methods=["POST"])
@_require_auth
def api_plot_stages_upsert():
    stages = (request.get_json() or {}).get("stages", [])
    if not stages:
        return jsonify({"ok": False, "error": "No stages"}), 400
    try:
        for s in stages:
            ex = supabase.table("smc_plot_stages").select("id") \
                .eq("plot_id", s["plot_id"]).eq("stage_def_id", s["stage_def_id"]).execute()
            row = {
                "plot_id":        s["plot_id"],
                "stage_def_id":   s["stage_def_id"],
                "start_day":      s["start_day"],
                "duration":       s["duration"],
                "baseline_start": s.get("baseline_start", s["start_day"]),
                "status":         s.get("status", "pending"),
            }
            if ex.data:
                supabase.table("smc_plot_stages").update(row).eq("id", ex.data[0]["id"]).execute()
            else:
                supabase.table("smc_plot_stages").insert(row).execute()
        return ok()
    except Exception as e:
        return err(e)


@smc_bp.route("/api/plot-stages/<stage_id>", methods=["PATCH"])
@_require_auth
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
@_require_auth
def api_hs_inspections():
    try:
        r = supabase.table("smc_hs_inspections").select("*") \
            .eq("project_id", SMC_PROJECT_ID).order("next_date").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/hs-inspections/<insp_id>", methods=["PATCH"])
@_require_auth
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
@_require_auth
def api_hs_log():
    try:
        r = supabase.table("smc_hs_log").select("*") \
            .eq("project_id", SMC_PROJECT_ID).order("log_date", desc=True).execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/hs-log", methods=["POST"])
@_require_auth
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
@_require_auth
def api_map_image_get():
    try:
        r = supabase.table("smc_map_image").select("*") \
            .eq("project_id", SMC_PROJECT_ID).limit(1).execute()
        return jsonify(r.data[0] if r.data else {})
    except Exception as e:
        return err(e)


@smc_bp.route("/api/map-image", methods=["POST"])
@_require_auth
def api_map_image_set():
    image_url = (request.get_json() or {}).get("image_url", "")
    try:
        ex = supabase.table("smc_map_image").select("id").eq("project_id", SMC_PROJECT_ID).execute()
        if ex.data:
            supabase.table("smc_map_image").update({"image_url": image_url}) \
                .eq("project_id", SMC_PROJECT_ID).execute()
        else:
            supabase.table("smc_map_image").insert(
                {"project_id": SMC_PROJECT_ID, "image_url": image_url}
            ).execute()
        return ok()
    except Exception as e:
        return err(e)
