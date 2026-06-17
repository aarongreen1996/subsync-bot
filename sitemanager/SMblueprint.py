"""
site_manager/blueprint.py
Site Manager Command Centre — Flask Blueprint
Mounted at /smc/ prefix in the main app
"""
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, abort
from functools import wraps
import json
import os

smc_bp = Blueprint("smc", __name__, template_folder="templates")

# ── Supabase client (shared with PD) ─────────────────────────────────────────
from supabase import create_client
_supa_url = os.environ.get("SUPABASE_URL", "")
_supa_key = os.environ.get("SUPABASE_KEY", "")
supabase = create_client(_supa_url, _supa_key) if _supa_url and _supa_key else None

SMC_PROJECT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

# ── Auth helpers ──────────────────────────────────────────────────────────────
SMC_PASSWORD = os.environ.get("SMC_PASSWORD", "sandle2026")

def _require_smc_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("smc_auth"):
            return redirect(url_for("smc.login"))
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ───────────────────────────────────────────────────────────────
@smc_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == SMC_PASSWORD:
            session["smc_auth"] = True
            return redirect(url_for("smc.index"))
        error = "Incorrect password"
    return render_template("smc/login.html", error=error)


@smc_bp.route("/logout")
def logout():
    session.pop("smc_auth", None)
    return redirect(url_for("smc.login"))


# ── Main app ──────────────────────────────────────────────────────────────────
@smc_bp.route("/")
@_require_smc_auth
def index():
    return render_template("smc/app.html")


# ── API: project info ─────────────────────────────────────────────────────────
@smc_bp.route("/api/project")
@_require_smc_auth
def api_project():
    try:
        res = supabase.table("smc_projects").select("*").eq("id", SMC_PROJECT_ID).single().execute()
        return jsonify(res.data or {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: plots ────────────────────────────────────────────────────────────────
@smc_bp.route("/api/plots")
@_require_smc_auth
def api_plots():
    try:
        res = supabase.table("smc_plots").select("*").eq("project_id", SMC_PROJECT_ID).order("plot_number").execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@smc_bp.route("/api/plots/<plot_id>", methods=["PATCH"])
@_require_smc_auth
def api_plot_update(plot_id):
    data = request.get_json() or {}
    allowed = {"programme_start_day", "map_x", "map_y"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields"}), 400
    try:
        supabase.table("smc_plots").update(updates).eq("id", plot_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: stage definitions ────────────────────────────────────────────────────
@smc_bp.route("/api/stage-defs")
@_require_smc_auth
def api_stage_defs():
    try:
        res = supabase.table("smc_stage_defs").select("*").eq("project_id", SMC_PROJECT_ID).order("stage_order").execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: plot stages (Gantt schedule) ─────────────────────────────────────────
@smc_bp.route("/api/plot-stages")
@_require_smc_auth
def api_plot_stages():
    try:
        res = supabase.table("smc_plot_stages").select("*, smc_plots(plot_number)").execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@smc_bp.route("/api/plot-stages", methods=["POST"])
@_require_smc_auth
def api_plot_stages_upsert():
    """Upsert an entire plot's stage schedule (called when user drags / edits)."""
    data = request.get_json() or {}
    stages = data.get("stages", [])
    if not stages:
        return jsonify({"ok": False, "error": "No stages"}), 400
    try:
        for s in stages:
            existing = supabase.table("smc_plot_stages").select("id").eq("plot_id", s["plot_id"]).eq("stage_def_id", s["stage_def_id"]).execute()
            row = {
                "plot_id": s["plot_id"],
                "stage_def_id": s["stage_def_id"],
                "start_day": s["start_day"],
                "duration": s["duration"],
                "baseline_start": s.get("baseline_start", s["start_day"]),
                "status": s.get("status", "pending"),
                "notes": s.get("notes", ""),
            }
            if existing.data:
                supabase.table("smc_plot_stages").update(row).eq("id", existing.data[0]["id"]).execute()
            else:
                supabase.table("smc_plot_stages").insert(row).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@smc_bp.route("/api/plot-stages/<stage_id>", methods=["PATCH"])
@_require_smc_auth
def api_plot_stage_patch(stage_id):
    data = request.get_json() or {}
    allowed = {"start_day", "duration", "status", "notes"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields"}), 400
    try:
        from datetime import datetime, timezone
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        supabase.table("smc_plot_stages").update(updates).eq("id", stage_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: H&S inspections ──────────────────────────────────────────────────────
@smc_bp.route("/api/hs-inspections")
@_require_smc_auth
def api_hs_inspections():
    try:
        res = supabase.table("smc_hs_inspections").select("*").eq("project_id", SMC_PROJECT_ID).order("next_date").execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@smc_bp.route("/api/hs-inspections/<insp_id>", methods=["PATCH"])
@_require_smc_auth
def api_hs_inspection_update(insp_id):
    data = request.get_json() or {}
    allowed = {"last_date", "next_date", "responsible_person"}
    updates = {k: v for k, v in data.items() if k in allowed}
    try:
        supabase.table("smc_hs_inspections").update(updates).eq("id", insp_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: H&S log ──────────────────────────────────────────────────────────────
@smc_bp.route("/api/hs-log")
@_require_smc_auth
def api_hs_log():
    try:
        res = supabase.table("smc_hs_log").select("*").eq("project_id", SMC_PROJECT_ID).order("log_date", desc=True).execute()
        return jsonify(res.data or [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@smc_bp.route("/api/hs-log", methods=["POST"])
@_require_smc_auth
def api_hs_log_create():
    data = request.get_json() or {}
    data["project_id"] = SMC_PROJECT_ID
    allowed = {"project_id", "log_date", "log_type", "person_name", "status", "detail", "severity"}
    row = {k: v for k, v in data.items() if k in allowed}
    try:
        supabase.table("smc_hs_log").insert(row).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: map image URL ────────────────────────────────────────────────────────
@smc_bp.route("/api/map-image", methods=["GET"])
@_require_smc_auth
def api_map_image_get():
    try:
        res = supabase.table("smc_map_image").select("*").eq("project_id", SMC_PROJECT_ID).limit(1).execute()
        return jsonify(res.data[0] if res.data else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@smc_bp.route("/api/map-image", methods=["POST"])
@_require_smc_auth
def api_map_image_set():
    """Store a public image URL for the site map background."""
    data = request.get_json() or {}
    image_url = data.get("image_url", "")
    try:
        existing = supabase.table("smc_map_image").select("id").eq("project_id", SMC_PROJECT_ID).execute()
        if existing.data:
            supabase.table("smc_map_image").update({"image_url": image_url}).eq("project_id", SMC_PROJECT_ID).execute()
        else:
            supabase.table("smc_map_image").insert({"project_id": SMC_PROJECT_ID, "image_url": image_url}).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
