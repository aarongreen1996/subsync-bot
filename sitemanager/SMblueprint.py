"""
sitemanager/SMblueprint.py
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, make_response, Response
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
            resp.set_cookie(COOKIE_NAME, _token(), max_age=43200, httponly=True, samesite="Lax", path="/")
            return resp
        error = "Incorrect password"
    return render_template("smc/SMlogin.html", error=error)


@smc_bp.route("/logout")
def logout():
    resp = make_response(redirect(url_for("smc.login")))
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ── App shell ─────────────────────────────────────────────────────────────────
# Read and return the file directly as text/html — bypasses Jinja2 entirely
@smc_bp.route("/")
@_require_auth
def index():
    html_path = os.path.join(os.path.dirname(__file__), "templates", "smc", "SMapp.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content, mimetype="text/html")


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
    updates = {k: v for k, v in data.items() if k in {"programme_start_day", "map_x", "map_y", "current_stage_id", "notes"}}
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


# ── Trades ─────────────────────────────────────────────────────────────────────
@smc_bp.route("/api/trades")
@_require_auth
def api_trades():
    try:
        r = supabase.table("smc_trades").select("*").order("sort_order").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


# ── Stage templates ───────────────────────────────────────────────────────────
@smc_bp.route("/api/stage-templates")
@_require_auth
def api_stage_templates():
    try:
        r = supabase.table("smc_stage_templates").select("*").order("name").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


# ── Stage def editing (rename, change trade, change duration, reorder) ───────
@smc_bp.route("/api/stage-defs/<stage_id>", methods=["PATCH"])
@_require_auth
def api_stage_def_update(stage_id):
    data = request.get_json() or {}
    updates = {k: v for k, v in data.items() if k in {"name", "trade", "trade_id", "color", "default_duration", "stage_order"}}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields"}), 400
    try:
        supabase.table("smc_stage_defs").update(updates).eq("id", stage_id).execute()
        return ok()
    except Exception as e:
        return err(e)


@smc_bp.route("/api/stage-defs", methods=["POST"])
@_require_auth
def api_stage_def_create():
    data = request.get_json() or {}
    row = {
        "project_id": SMC_PROJECT_ID,
        "template_id": data.get("template_id"),
        "stage_order": data.get("stage_order", 999),
        "name": data.get("name", "New Stage"),
        "trade": data.get("trade", ""),
        "trade_id": data.get("trade_id"),
        "color": data.get("color", "#64748b"),
        "default_duration": data.get("default_duration", 1),
    }
    try:
        r = supabase.table("smc_stage_defs").insert(row).execute()
        return jsonify({"ok": True, "stage": r.data[0] if r.data else None})
    except Exception as e:
        return err(e)


@smc_bp.route("/api/stage-defs/<stage_id>", methods=["DELETE"])
@_require_auth
def api_stage_def_delete(stage_id):
    try:
        supabase.table("smc_stage_defs").delete().eq("id", stage_id).execute()
        return ok()
    except Exception as e:
        return err(e)


# ── Materials master tracker ──────────────────────────────────────────────────
@smc_bp.route("/api/materials")
@_require_auth
def api_materials():
    try:
        r = supabase.table("smc_materials").select("*, smc_stage_defs(name)") \
            .eq("project_id", SMC_PROJECT_ID).order("created_at").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/materials", methods=["POST"])
@_require_auth
def api_material_create():
    data = request.get_json() or {}
    row = {
        "project_id": SMC_PROJECT_ID,
        "material_name": data.get("material_name", ""),
        "linked_stage_id": data.get("linked_stage_id"),
        "lead_time_weeks": data.get("lead_time_weeks", 1),
        "supplier_name": data.get("supplier_name", ""),
        "supplier_email": data.get("supplier_email", ""),
        "po_number": data.get("po_number", ""),
        "applies_to_all_plots": data.get("applies_to_all_plots", True),
        "description": data.get("description", ""),
    }
    try:
        r = supabase.table("smc_materials").insert(row).execute()
        material = r.data[0] if r.data else None
        # If specific plots given, create the plot links
        plot_ids = data.get("plot_ids", [])
        if material and not row["applies_to_all_plots"] and plot_ids:
            links = [{"material_id": material["id"], "plot_id": pid} for pid in plot_ids]
            supabase.table("smc_material_plot_links").insert(links).execute()
        return jsonify({"ok": True, "material": material})
    except Exception as e:
        return err(e)


@smc_bp.route("/api/materials/<material_id>", methods=["PATCH"])
@_require_auth
def api_material_update(material_id):
    data = request.get_json() or {}
    updates = {k: v for k, v in data.items() if k in
               {"material_name", "linked_stage_id", "lead_time_weeks", "supplier_name", "supplier_email", "po_number", "description"}}
    try:
        supabase.table("smc_materials").update(updates).eq("id", material_id).execute()
        return ok()
    except Exception as e:
        return err(e)


@smc_bp.route("/api/materials/<material_id>", methods=["DELETE"])
@_require_auth
def api_material_delete(material_id):
    try:
        supabase.table("smc_materials").delete().eq("id", material_id).execute()
        return ok()
    except Exception as e:
        return err(e)


# ── Material orders (the per-plot master tracker) ────────────────────────────
@smc_bp.route("/api/material-orders")
@_require_auth
def api_material_orders():
    try:
        r = supabase.table("smc_material_orders").select("*").execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/material-orders", methods=["POST"])
@_require_auth
def api_material_order_upsert():
    """Upsert a material's order status for a specific plot."""
    data = request.get_json() or {}
    material_id = data.get("material_id")
    plot_id = data.get("plot_id")
    if not material_id or not plot_id:
        return jsonify({"ok": False, "error": "material_id and plot_id required"}), 400
    try:
        from datetime import datetime, timezone
        existing = supabase.table("smc_material_orders").select("id") \
            .eq("material_id", material_id).eq("plot_id", plot_id).execute()
        row = {
            "material_id": material_id,
            "plot_id": plot_id,
            "status": data.get("status", "pending"),
            "ordered_date": data.get("ordered_date"),
            "delivery_date": data.get("delivery_date"),
            "notes": data.get("notes", ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if existing.data:
            supabase.table("smc_material_orders").update(row).eq("id", existing.data[0]["id"]).execute()
        else:
            supabase.table("smc_material_orders").insert(row).execute()
        return ok()
    except Exception as e:
        return err(e)


# ── Plot H&S issue — auto-populates the H&S log ──────────────────────────────
@smc_bp.route("/api/plots/<plot_id>/raise-hs-issue", methods=["POST"])
@_require_auth
def api_plot_raise_hs(plot_id):
    """Called from the plot detail modal's H&S box. Creates an smc_hs_log entry
    linked to this plot, which automatically shows on the H&S tab."""
    data = request.get_json() or {}
    detail = data.get("detail", "").strip()
    if not detail:
        return jsonify({"ok": False, "error": "No detail provided"}), 400
    try:
        plot = supabase.table("smc_plots").select("plot_number").eq("id", plot_id).single().execute()
        plot_number = plot.data.get("plot_number", "?") if plot.data else "?"
        from datetime import date
        row = {
            "project_id": SMC_PROJECT_ID,
            "plot_id": plot_id,
            "log_date": str(date.today()),
            "log_type": "Plot Issue — Plot " + str(plot_number),
            "person_name": data.get("person_name", "Site Manager"),
            "status": "Open",
            "detail": detail,
            "severity": data.get("severity", "amber"),
        }
        supabase.table("smc_hs_log").insert(row).execute()
        return ok()
    except Exception as e:
        return err(e)
