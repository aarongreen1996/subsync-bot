"""
sitemanager/smc_induction.py

Site induction module for Site Manager Command Centre (SMC).
Covers: repeat-visitor lookup, induction form + RAMS sign-off, quiz
submission, QR sign in/out, CSCS card photo upload, and the manager
roster.

Reuses the same supabase client, SMC_PROJECT_ID, and auth decorator
as SMblueprint.py — this assumes sitemanager is a proper package
(has __init__.py) so the relative import works. If your app.py
imports SMblueprint some other way (e.g. plain `import SMblueprint`
rather than as a package), change the import line below to match.
"""

from flask import Blueprint, request, jsonify
from datetime import datetime, timezone
import base64, uuid, datetime as dt

from .SMblueprint import supabase, SMC_PROJECT_ID, _require_auth, ok, err

induction_bp = Blueprint("induction", __name__)


@induction_bp.after_request
def add_cors_headers(response):
    """Allows the operative-facing endpoints to be called from a phone
    browser that isn't on the note2quote.co.uk domain (e.g. a QR-scan
    landing page hosted elsewhere, or this API tester)."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

REPEAT_INDUCTION_VALID_DAYS = 365  # company/person-level induction validity


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════
# OPERATIVE-FACING — no login. These are hit from a subcontractor's own
# phone after scanning the site QR code, so must NOT sit behind _require_auth.
# ══════════════════════════════════════════════════════════════════════════

@induction_bp.route("/api/induction/lookup", methods=["POST"])
def induction_lookup():
    """Repeat-visitor check by phone or CSCS number — lets the frontend
    skip straight to RAMS + badge if there's a still-valid induction."""
    data = request.get_json() or {}
    phone = data.get("phone")
    cscs_number = data.get("cscs_number")
    if not phone and not cscs_number:
        return jsonify({"ok": False, "error": "phone or cscs_number required"}), 400
    try:
        q = supabase.table("smc_operatives").select("*")
        q = q.eq("cscs_number", cscs_number) if cscs_number else q.eq("phone", phone)
        result = q.limit(1).execute()
        if not result.data:
            return jsonify({"ok": True, "found": False})

        operative = result.data[0]
        recent = (
            supabase.table("smc_inductions")
            .select("*")
            .eq("operative_id", operative["id"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        still_valid = False
        if recent.data:
            last = recent.data[0]
            if last.get("quiz_passed"):
                age_days = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(last["created_at"].replace("Z", "+00:00"))
                ).days
                still_valid = age_days <= REPEAT_INDUCTION_VALID_DAYS

        return jsonify({"ok": True, "found": True, "operative": operative, "induction_still_valid": still_valid})
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/form", methods=["POST"])
def induction_form():
    """Creates/matches the contractor by name, upserts the operative."""
    data = request.get_json() or {}
    required = ["name", "phone", "company_name", "emergency_contact_name", "emergency_contact_phone"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"ok": False, "error": f"missing fields: {', '.join(missing)}"}), 400
    try:
        existing = (
            supabase.table("smc_contractors")
            .select("id")
            .ilike("name", data["company_name"])
            .limit(1)
            .execute()
        )
        if existing.data:
            contractor_id = existing.data[0]["id"]
        else:
            created = (
                supabase.table("smc_contractors")
                .insert({"name": data["company_name"], "project_id": SMC_PROJECT_ID})
                .execute()
            )
            contractor_id = created.data[0]["id"]

        operative_payload = {
            "contractor_id": contractor_id,
            "name": data["name"],
            "phone": data["phone"],
            "emergency_contact_name": data["emergency_contact_name"],
            "emergency_contact_phone": data["emergency_contact_phone"],
            "vehicle_reg": data.get("vehicle_reg"),
            "trade_id": data.get("trade_id"),
            "cscs_type": data.get("cscs_type"),
            "cscs_number": data.get("cscs_number"),
            "cscs_expiry": data.get("cscs_expiry"),
            "cscs_card_url": data.get("cscs_card_url"),
            "cscs_verified": bool(data.get("cscs_verified", False)),
        }

        operative_id = data.get("operative_id")
        if operative_id:
            supabase.table("smc_operatives").update(operative_payload).eq("id", operative_id).execute()
        else:
            created_op = supabase.table("smc_operatives").insert(operative_payload).execute()
            operative_id = created_op.data[0]["id"]

        return jsonify({"ok": True, "operative_id": operative_id, "contractor_id": contractor_id})
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/cscs-photo", methods=["POST"])
def induction_cscs_photo():
    """Upload a CSCS card photo to the same smc-photos storage bucket
    used elsewhere in SMC. Mirrors api_photo_upload in SMblueprint.py."""
    data = request.get_json() or {}
    image_b64 = data.get("image_b64", "")
    mime_type = data.get("mime_type", "image/jpeg")
    if not image_b64:
        return jsonify({"ok": False, "error": "image_b64 required"}), 400
    try:
        img_bytes = base64.b64decode(image_b64)
        ext = "jpg" if "jpeg" in mime_type else mime_type.split("/")[-1]
        ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        uid = str(uuid.uuid4())[:8]
        path = f"cscs-cards/{ts}_{uid}.{ext}"
        supabase.storage.from_("smc-photos").upload(
            path, img_bytes, {"content-type": mime_type, "upsert": "true"}
        )
        public_url = supabase.storage.from_("smc-photos").get_public_url(path)
        return jsonify({"ok": True, "url": public_url})
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/start", methods=["POST"])
def induction_start():
    data = request.get_json() or {}
    operative_id = data.get("operative_id")
    if not operative_id:
        return jsonify({"ok": False, "error": "operative_id required"}), 400
    try:
        created = (
            supabase.table("smc_inductions")
            .insert({"operative_id": operative_id, "project_id": SMC_PROJECT_ID})
            .execute()
        )
        return jsonify({"ok": True, "induction": created.data[0]})
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/video-watched", methods=["POST"])
def induction_video_watched():
    data = request.get_json() or {}
    induction_id = data.get("induction_id")
    if not induction_id:
        return jsonify({"ok": False, "error": "induction_id required"}), 400
    try:
        supabase.table("smc_inductions").update({"video_watched_at": now_iso()}).eq("id", induction_id).execute()
        return ok()
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/quiz", methods=["POST"])
def induction_quiz():
    data = request.get_json() or {}
    induction_id = data.get("induction_id")
    if not induction_id:
        return jsonify({"ok": False, "error": "induction_id required"}), 400
    score = data.get("score", 0)
    total = data.get("total", 1)
    passed = score >= total  # require full marks; loosen if you want a pass threshold
    try:
        supabase.table("smc_inductions").update(
            {"quiz_score": score, "quiz_passed": passed}
        ).eq("id", induction_id).execute()
        return jsonify({"ok": True, "passed": passed})
    except Exception as e:
        return err(e)


@induction_bp.route("/induction")
def induction_landing():
    """Public QR-scan landing page — no login required."""
    import os
    html_path = os.path.join(os.path.dirname(__file__), "templates", "smc", "induction.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    from flask import Response
    return Response(content, mimetype="text/html")


@induction_bp.route("/api/induction/trades", methods=["GET"])
def induction_trades():
    """Public trade list for the operative form dropdown — the existing
    /api/trades in SMblueprint.py sits behind _require_auth, which an
    operative on their own phone won't have."""
    try:
        result = supabase.table("smc_trades").select("*").order("sort_order").execute()
        return jsonify(result.data or [])
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/rams", methods=["GET"])
def induction_get_rams():
    """RAMS relevant to this operative's trade, for the current site."""
    trade_id = request.args.get("trade_id")
    try:
        q = supabase.table("smc_rams").select("*").eq("project_id", SMC_PROJECT_ID)
        if trade_id:
            q = q.eq("trade_id", trade_id)
        result = q.execute()
        return jsonify(result.data or [])
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/rams-sign", methods=["POST"])
def induction_sign_rams():
    data = request.get_json() or {}
    required = ["rams_id", "operative_id", "induction_id"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"ok": False, "error": f"missing fields: {', '.join(missing)}"}), 400
    try:
        created = (
            supabase.table("smc_rams_briefings")
            .insert(
                {
                    "rams_id": data["rams_id"],
                    "operative_id": data["operative_id"],
                    "induction_id": data["induction_id"],
                    "signature_data": data.get("signature_data"),
                }
            )
            .execute()
        )
        return jsonify({"ok": True, "briefing": created.data[0]})
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/sign-in", methods=["POST"])
def induction_sign_in():
    data = request.get_json() or {}
    induction_id = data.get("induction_id")
    if not induction_id:
        return jsonify({"ok": False, "error": "induction_id required"}), 400
    try:
        supabase.table("smc_inductions").update(
            {"status": "signed_in", "signed_in_at": now_iso()}
        ).eq("id", induction_id).execute()
        return ok()
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/sign-out", methods=["POST"])
def induction_sign_out():
    data = request.get_json() or {}
    induction_id = data.get("induction_id")
    if not induction_id:
        return jsonify({"ok": False, "error": "induction_id required"}), 400
    try:
        supabase.table("smc_inductions").update(
            {"status": "signed_out", "signed_out_at": now_iso()}
        ).eq("id", induction_id).execute()
        return ok()
    except Exception as e:
        return err(e)


# ══════════════════════════════════════════════════════════════════════════
# MANAGER-FACING — behind the SMC login, same as the rest of the dashboard.
# ══════════════════════════════════════════════════════════════════════════

@induction_bp.route("/manage")
@_require_auth
def induction_manage():
    """Manager dashboard — roster + company folders. Behind SMC login."""
    import os
    from flask import Response
    html_path = os.path.join(os.path.dirname(__file__), "templates", "smc", "induction_manage.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content, mimetype="text/html")


@induction_bp.route("/api/induction/contractors")
@_require_auth
def induction_contractors():
    """Company folder view — each contractor with their operatives nested,
    CSCS expiry flagged, and a RAMS count. This is what an HSE visit gets shown."""
    try:
        contractors = supabase.table("smc_contractors").select("*").eq("project_id", SMC_PROJECT_ID).execute()
        operatives = supabase.table("smc_operatives").select("*").execute()
        rams = supabase.table("smc_rams").select("id, contractor_id").eq("project_id", SMC_PROJECT_ID).execute()

        ops_by_contractor = {}
        for op in (operatives.data or []):
            ops_by_contractor.setdefault(op["contractor_id"], []).append(op)
        rams_count = {}
        for r in (rams.data or []):
            rams_count[r["contractor_id"]] = rams_count.get(r["contractor_id"], 0) + 1

        result = []
        for c in (contractors.data or []):
            result.append({
                **c,
                "operatives": ops_by_contractor.get(c["id"], []),
                "rams_count": rams_count.get(c["id"], 0),
            })
        return jsonify(result)
    except Exception as e:
        return err(e)


@induction_bp.route("/api/induction/roster")
@_require_auth
def induction_roster():
    """Everyone inducted for the site, joined through to operative + contractor.
    This is what the SMC dashboard tab and any overdue-signout reminder reads from."""
    try:
        result = (
            supabase.table("smc_inductions")
            .select("*, smc_operatives(name, phone, vehicle_reg, smc_contractors(name))")
            .eq("project_id", SMC_PROJECT_ID)
            .order("created_at", desc=True)
            .execute()
        )
        return jsonify(result.data or [])
    except Exception as e:
        return err(e)
