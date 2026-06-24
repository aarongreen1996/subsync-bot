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
    updates = {k: v for k, v in data.items() if k in {"programme_start_day", "map_x", "map_y", "current_stage_id", "notes", "cml_signed_off", "cml_signed_off_date", "crc_signed_off", "crc_signed_off_date", "crc_deadline_date", "cml_cert_url", "crc_cert_url"}}
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
    template_id = request.args.get("template_id")
    all_templates = request.args.get("all") == "1"
    try:
        q = supabase.table("smc_stage_defs").select("*").eq("project_id", SMC_PROJECT_ID)
        if template_id:
            q = q.eq("template_id", template_id)
        elif not all_templates:
            # Default: only return the House (Standard) template — the primary programme template.
            # This prevents 178 duplicate stage names appearing in dropdowns and the Gantt.
            default_tpl = supabase.table("smc_stage_templates") \
                .select("id").eq("is_default", True).limit(1).execute()
            if default_tpl.data:
                q = q.eq("template_id", default_tpl.data[0]["id"])
        r = q.order("stage_order").execute()
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
    updates = {k: v for k, v in data.items() if k in {"last_date", "next_date", "responsible_person", "frequency_editable"}}
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


@smc_bp.route("/api/hs-log/<log_id>", methods=["PATCH"])
@_require_auth
def api_hs_log_update(log_id):
    """Edit a log entry's status/comment, or sign it off."""
    data = request.get_json() or {}
    updates = {k: v for k, v in data.items() if k in
               {"status", "comment", "detail", "severity", "signed_off", "signed_off_by"}}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields"}), 400
    if updates.get("signed_off"):
        from datetime import datetime, timezone
        updates["signed_off_at"] = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table("smc_hs_log").update(updates).eq("id", log_id).execute()
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


@smc_bp.route("/api/stage-templates", methods=["POST"])
@_require_auth
def api_stage_template_create():
    """Create a new custom programme template (e.g. '3-Bed House', 'Townhouse')."""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    try:
        r = supabase.table("smc_stage_templates").insert({
            "name": name,
            "description": data.get("description", "Custom programme"),
            "is_default": False,
        }).execute()
        return jsonify({"ok": True, "template": r.data[0] if r.data else None})
    except Exception as e:
        return err(e)

@smc_bp.route("/api/stage-templates/<target_id>/clone-from/<source_id>", methods=["POST"])
@_require_auth
def api_stage_template_clone(target_id, source_id):
    """Clone all stage defs from source template into target template.
    Used to seed Bungalow/Flat templates from House Standard as a starting point."""
    try:
        # Fetch source stages
        src = supabase.table("smc_stage_defs").select("*") \
            .eq("project_id", SMC_PROJECT_ID).eq("template_id", source_id).order("stage_order").execute()
        if not src.data:
            return jsonify({"ok": False, "error": "No stages found in source template"}), 400

        # Delete any existing stages in the target template first
        supabase.table("smc_stage_defs").delete() \
            .eq("project_id", SMC_PROJECT_ID).eq("template_id", target_id).execute()

        # Insert cloned stages into target
        new_rows = []
        for s in src.data:
            new_rows.append({
                "project_id": SMC_PROJECT_ID,
                "template_id": target_id,
                "stage_order": s["stage_order"],
                "name": s["name"],
                "trade": s.get("trade", ""),
                "trade_id": s.get("trade_id"),
                "color": s.get("color", "#64748b"),
                "default_duration": s.get("default_duration", 1),
            })
        supabase.table("smc_stage_defs").insert(new_rows).execute()
        return jsonify({"ok": True, "cloned": len(new_rows)})
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


# ══════════════════════════════════════════════════════════════════════════
# ITEMS (cleanup notices, outstanding works, dayworks, RFI notes)
# ══════════════════════════════════════════════════════════════════════════

def _next_ref(item_type):
    """Generate the next reference number for an item type (e.g. CLN-001)."""
    prefixes = {
        'cleanup_notice':   'CLN',
        'outstanding_work': 'OSW',
        'daywork':          'DW',
        'rfi_note':         'RFI',
    }
    prefix = prefixes.get(item_type, 'REF')
    try:
        r = supabase.table("smc_ref_counters") \
            .select("next_val") \
            .eq("project_id", SMC_PROJECT_ID) \
            .eq("item_type", item_type) \
            .single().execute()
        n = r.data["next_val"] if r.data else 1
        supabase.table("smc_ref_counters") \
            .update({"next_val": n + 1}) \
            .eq("project_id", SMC_PROJECT_ID) \
            .eq("item_type", item_type).execute()
        return f"{prefix}-{n:03d}"
    except Exception:
        return f"{prefix}-001"


@smc_bp.route("/api/items")
@_require_auth
def api_items():
    plot_id = request.args.get("plot_id")
    item_type = request.args.get("type")
    try:
        q = supabase.table("smc_items").select("*").eq("project_id", SMC_PROJECT_ID)
        if plot_id:
            q = q.eq("plot_id", plot_id)
        if item_type:
            q = q.eq("item_type", item_type)
        r = q.order("created_at", desc=True).execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/items", methods=["POST"])
@_require_auth
def api_item_create():
    data = request.get_json() or {}
    item_type = data.get("item_type", "general")
    from datetime import datetime, timezone
    row = {
        "project_id":     SMC_PROJECT_ID,
        "plot_id":        data.get("plot_id"),
        "item_type":      item_type,
        "ref_number":     _next_ref(item_type),
        "title":          data.get("title", "").strip(),
        "description":    data.get("description", "").strip(),
        "trade_id":       data.get("trade_id"),
        "trade_name":     data.get("trade_name", ""),
        "status":         data.get("status", "open"),
        "priority":       data.get("priority", "medium"),
        "deadline_date":  data.get("deadline_date"),
        "chargeable":     data.get("chargeable", False),
        "estimated_cost": data.get("estimated_cost"),
        "ref_drawing":    data.get("ref_drawing", ""),
        "updated_at":     datetime.now(timezone.utc).isoformat(),
    }
    try:
        r = supabase.table("smc_items").insert(row).execute()
        return jsonify({"ok": True, "item": r.data[0] if r.data else None})
    except Exception as e:
        return err(e)


@smc_bp.route("/api/items/<item_id>", methods=["PATCH"])
@_require_auth
def api_item_update(item_id):
    data = request.get_json() or {}
    from datetime import datetime, timezone
    updates = {k: v for k, v in data.items() if k in {
        "title", "description", "trade_id", "trade_name", "status",
        "priority", "deadline_date", "resolved_date", "chargeable",
        "estimated_cost", "ref_drawing"
    }}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table("smc_items").update(updates).eq("id", item_id).execute()
        return ok()
    except Exception as e:
        return err(e)


@smc_bp.route("/api/items/<item_id>", methods=["DELETE"])
@_require_auth
def api_item_delete(item_id):
    try:
        supabase.table("smc_items").delete().eq("id", item_id).execute()
        return ok()
    except Exception as e:
        return err(e)


# ══════════════════════════════════════════════════════════════════════════
# PHOTOS — Supabase Storage upload + metadata
# ══════════════════════════════════════════════════════════════════════════

@smc_bp.route("/api/photos", methods=["POST"])
@_require_auth
def api_photo_upload():
    """Receive a base64-encoded image, upload to Supabase Storage, record metadata."""
    data = request.get_json() or {}
    plot_id   = data.get("plot_id")
    item_id   = data.get("item_id")          # optional
    caption   = data.get("caption", "")
    taken_at  = data.get("taken_at")         # ISO string from client
    image_b64 = data.get("image_b64", "")
    mime_type = data.get("mime_type", "image/jpeg")
    if not plot_id or not image_b64:
        return jsonify({"ok": False, "error": "plot_id and image_b64 required"}), 400
    try:
        import base64, uuid, datetime as dt
        img_bytes = base64.b64decode(image_b64)
        ext       = "jpg" if "jpeg" in mime_type else mime_type.split("/")[-1]
        ts        = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        uid       = str(uuid.uuid4())[:8]
        path      = f"{plot_id}/{ts}_{uid}.{ext}"
        # Upload to Supabase Storage
        supabase.storage.from_("smc-photos").upload(
            path, img_bytes, {"content-type": mime_type, "upsert": "true"}
        )
        public_url = supabase.storage.from_("smc-photos").get_public_url(path)
        # Store metadata
        row = {
            "project_id":   SMC_PROJECT_ID,
            "plot_id":      plot_id,
            "item_id":      item_id,
            "storage_path": path,
            "public_url":   public_url,
            "caption":      caption,
            "taken_at":     taken_at or dt.datetime.utcnow().isoformat(),
        }
        r = supabase.table("smc_photos").insert(row).execute()
        return jsonify({"ok": True, "photo": r.data[0] if r.data else None, "public_url": public_url})
    except Exception as e:
        return err(e)


@smc_bp.route("/api/photos")
@_require_auth
def api_photos_list():
    plot_id = request.args.get("plot_id")
    item_id = request.args.get("item_id")
    try:
        q = supabase.table("smc_photos").select("*").eq("project_id", SMC_PROJECT_ID)
        if plot_id:
            q = q.eq("plot_id", plot_id)
        if item_id:
            q = q.eq("item_id", item_id)
        r = q.order("taken_at", desc=True).execute()
        return jsonify(r.data or [])
    except Exception as e:
        return err(e)


@smc_bp.route("/api/photos/<photo_id>", methods=["DELETE"])
@_require_auth
def api_photo_delete(photo_id):
    try:
        r = supabase.table("smc_photos").select("storage_path").eq("id", photo_id).single().execute()
        if r.data:
            supabase.storage.from_("smc-photos").remove([r.data["storage_path"]])
        supabase.table("smc_photos").delete().eq("id", photo_id).execute()
        return ok()
    except Exception as e:
        return err(e)


# ══════════════════════════════════════════════════════════════════════════
# AI COMMUNICATIONS — generate emails/summaries via Claude API
# ══════════════════════════════════════════════════════════════════════════

@smc_bp.route("/api/ai/generate", methods=["POST"])
@_require_auth
def api_ai_generate():
    """Generate professional communications using Claude.
    The prompt is built server-side from live site data so the AI has full context."""
    data = request.get_json() or {}
    comm_type = data.get("type")           # 'daily_summary' | 'weekly_summary' | 'trade_notice' | 'qs_dayworks' | 'progress_report'
    context   = data.get("context", {})   # extra context from the frontend

    # Fetch fresh data for AI context
    try:
        plots_r    = supabase.table("smc_plots").select("plot_number,house_type,tenure,current_stage_id,crc_deadline_date,cml_signed_off,crc_signed_off,notes").eq("project_id", SMC_PROJECT_ID).execute()
        items_r    = supabase.table("smc_items").select("*").eq("project_id", SMC_PROJECT_ID).eq("status", "open").order("created_at", desc=True).execute()
        hs_r       = supabase.table("smc_hs_log").select("*").eq("project_id", SMC_PROJECT_ID).order("log_date", desc=True).limit(20).execute()
        stages_r   = supabase.table("smc_stage_defs").select("id,name,stage_order").eq("project_id", SMC_PROJECT_ID).order("stage_order").execute()
        plots      = plots_r.data or []
        items      = items_r.data or []
        hs_log     = hs_r.data or []
        stage_map  = {s["id"]: s["name"] for s in (stages_r.data or [])}
    except Exception as e:
        return err(e)

    # Enrich plots with current stage name
    from datetime import date
    today_str = str(date.today())
    for p in plots:
        p["current_stage_name"] = stage_map.get(p.get("current_stage_id"), "Not started")

    # Build prompt based on communication type
    site_name = "SS17 Phase 1C — Sandle Park, Fordingbridge"

    def summarise_items(item_type, label):
        filtered = [i for i in items if i["item_type"] == item_type]
        if not filtered:
            return ""
        lines = []
        for i in filtered:
            parts = [f"• [{i.get('ref_number','—')}] Plot {context.get('plot_number_map', {}).get(i.get('plot_id',''), '?')}: {i['title']}"]
            if i.get("trade_name"):  parts.append(f"({i['trade_name']})")
            if i.get("deadline_date"): parts.append(f"— due {i['deadline_date']}")
            if i.get("estimated_cost"): parts.append(f"— est. £{i['estimated_cost']}")
            lines.append(" ".join(parts))
        return f"\n{label}:\n" + "\n".join(lines)

    in_prog  = [p for p in plots if p["current_stage_name"] not in ("Not started", "CRC")]
    complete = [p for p in plots if p.get("crc_signed_off")]
    behind   = [p for p in plots if p.get("crc_deadline_date")]  # simplified — could add schedule calc

    plot_summary = f"Site: {site_name}\nTotal plots: {len(plots)}\nIn progress: {len(in_prog)}\nCRC signed off: {len(complete)}"
    plot_stages  = "\n".join([f"  Plot {p['plot_number']}: {p['current_stage_name']}" for p in plots if p['current_stage_name'] != 'Not started'])

    cleanup_text    = summarise_items("cleanup_notice", "Clean-Up Notices (Open)")
    outstanding_txt = summarise_items("outstanding_work", "Outstanding Works")
    daywork_txt     = summarise_items("daywork", "Dayworks/Extras")
    rfi_txt         = summarise_items("rfi_note", "RFI Notes")
    hs_txt = "\n".join([f"  • {h['log_date']} [{h.get('severity','').upper()}] {h['log_type']}: {h['detail']}" for h in hs_log[:5]]) or "None logged recently."

    prompts = {
        "daily_summary": f"""You are a professional site manager at {site_name}.
Write a concise daily site report email (suitable for copying into an email to a line manager).
Include: today's date ({today_str}), progress on key plots, any issues or H&S concerns, outstanding actions.
Keep it professional but conversational — not overly formal.

SITE DATA:
{plot_summary}

Current build stages (in-progress plots):
{plot_stages or 'None currently active'}

Outstanding items:
{cleanup_text}{outstanding_txt}{daywork_txt}{rfi_txt}

Recent H&S log:
{hs_txt}

Extra notes from site manager: {context.get('notes', 'None')}

Generate the email now. Start with 'Subject:' then 'Body:' on a new line.""",

        "weekly_summary": f"""You are a professional site manager at {site_name}.
Write a weekly progress report email suitable for a line manager or senior stakeholder.
Include: week summary, plots progressed, plots at risk, H&S summary, outstanding items, next week's focus.

SITE DATA:
{plot_summary}

Current build stages:
{plot_stages or 'None currently active'}

Open items this week:
{cleanup_text}{outstanding_txt}{daywork_txt}{rfi_txt}

H&S activity:
{hs_txt}

Extra notes: {context.get('notes', 'None')}

Generate the email. Start with 'Subject:' then 'Body:' on a new line.""",

        "trade_notice": f"""You are a professional site manager writing a formal notice to a trade contractor.
Trade: {context.get('trade_name', 'Contractor')}
Site: {site_name}
Date: {today_str}
Area / Location: {context.get('area_reference', 'Site')}

Write a short, professional but firm notice. Include:
1. A clear statement of the issue or instruction
2. The specific area or location this relates to: {context.get('area_reference', 'as noted on site')}
3. A specific timeframe for compliance (use a reasonable timeframe based on urgency)
4. A warning that failure to comply will result in {site_name} arranging the works at the contractor's cost
5. A sign-off section with a space for the contractor to acknowledge receipt

Issue / Instruction:
{context.get('notice_content', 'General instruction to the trade.')}

Keep it concise — 3-5 short paragraphs. Professional tone. Factual, not aggressive.
End with:
Issued by: {context.get('signed_by', 'Site Manager')}
Received by: ___________________  Date: ___________  Signature: ___________

Start with 'Subject:' then 'Body:' on a new line.""",

        "qs_dayworks": f"""You are a professional site manager informing a quantity surveyor of dayworks and agreed extras on site.
Site: {site_name}
Date: {today_str}

Write a clear, professional email to the QS summarising the following agreed extras and dayworks that need to be recorded and valued:

{daywork_txt or 'See attached notes.'}

Extra context from site manager: {context.get('notes', 'None')}

Keep it factual and brief. The QS needs enough detail to raise instructions or value the works.
Start with 'Subject:' then 'Body:' on a new line.""",

        "progress_report": f"""You are a professional site manager writing a formal progress report for {site_name}.
Date: {today_str}

Write a structured progress report covering: programme status, plots at risk, H&S summary, outstanding actions, and immediate priorities.

SITE DATA:
{plot_summary}

Build progress:
{plot_stages or 'None currently active'}

Open actions:
{cleanup_text}{outstanding_txt}{daywork_txt}{rfi_txt}

H&S:
{hs_txt}

Notes: {context.get('notes', 'None')}

Format as a clear report with headings. Professional, concise.""",
    }

    prompt = prompts.get(comm_type)
    if not prompt:
        return jsonify({"ok": False, "error": f"Unknown communication type: {comm_type}"}), 400

    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text if msg.content else ""
        return jsonify({"ok": True, "text": text})
    except Exception as e:
        return err(e)


# ── Certificate uploads (CML/CRC) ─────────────────────────────────────────────
@smc_bp.route("/api/plots/<plot_id>/upload-cert", methods=["POST"])
@_require_auth
def api_plot_upload_cert(plot_id):
    """Upload a CML or CRC certificate PDF/image and store public URL on the plot."""
    data = request.get_json() or {}
    cert_type = data.get("cert_type")   # 'cml' or 'crc'
    image_b64 = data.get("image_b64", "")
    mime_type = data.get("mime_type", "application/pdf")
    if not plot_id or not image_b64 or cert_type not in ("cml", "crc"):
        return jsonify({"ok": False, "error": "plot_id, cert_type (cml/crc), and image_b64 required"}), 400
    try:
        import base64, uuid, datetime as dt
        img_bytes = base64.b64decode(image_b64)
        ext = "pdf" if "pdf" in mime_type else mime_type.split("/")[-1]
        ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        uid = str(uuid.uuid4())[:8]
        path = f"certs/{plot_id}/{cert_type}_{ts}_{uid}.{ext}"
        supabase.storage.from_("smc-photos").upload(
            path, img_bytes, {"content-type": mime_type, "upsert": "true"}
        )
        public_url = supabase.storage.from_("smc-photos").get_public_url(path)
        field = "cml_cert_url" if cert_type == "cml" else "crc_cert_url"
        supabase.table("smc_plots").update({field: public_url}).eq("id", plot_id).execute()
        return jsonify({"ok": True, "url": public_url})
    except Exception as e:
        return err(e)
