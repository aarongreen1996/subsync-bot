"""
smc_induction.py

Site induction module for Site Manager Command Centre (SMC).
Covers: repeat-visitor lookup, induction form + RAMS sign-off, quiz
submission, QR sign in/out, and the manager-facing roster/reminders.

Assumes a module-level `supabase` client is available the same way
SMblueprint.py sets one up (SUPABASE_URL / SUPABASE_KEY env vars via
supabase-py). Adjust the import at the top if your existing blueprint
wires the client differently — swap it in and everything below just works.
"""

from flask import Blueprint, request, jsonify
from datetime import datetime, timezone
from supabase import create_client
import os

induction_bp = Blueprint("induction", __name__, url_prefix="/api/induction")

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

REPEAT_INDUCTION_VALID_DAYS = 365  # company/person-level induction validity


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# 1. Lookup — called when the QR flow opens, to see if this operative
#    already has a valid induction (skips straight to site-specific
#    RAMS + badge instead of the full video/quiz).
# ---------------------------------------------------------------------
@induction_bp.route("/lookup", methods=["POST"])
def lookup_operative():
    data = request.get_json(force=True)
    phone = data.get("phone")
    cscs_number = data.get("cscs_number")

    if not phone and not cscs_number:
        return jsonify({"error": "phone or cscs_number required"}), 400

    query = supabase.table("smc_operatives").select("*")
    if cscs_number:
        query = query.eq("cscs_number", cscs_number)
    else:
        query = query.eq("phone", phone)

    result = query.limit(1).execute()
    if not result.data:
        return jsonify({"found": False})

    operative = result.data[0]

    # Most recent induction for this operative, any site
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

    return jsonify({"found": True, "operative": operative, "induction_still_valid": still_valid})


# ---------------------------------------------------------------------
# 2. Form submission — creates/updates contractor + operative records.
#    Company is matched by name (case-insensitive) or created new.
# ---------------------------------------------------------------------
@induction_bp.route("/form", methods=["POST"])
def submit_form():
    data = request.get_json(force=True)
    required = ["name", "phone", "company_name", "emergency_contact_name", "emergency_contact_phone"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    # Find or create contractor
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
            .insert({"name": data["company_name"], "project_id": data.get("project_id")})
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

    return jsonify({"operative_id": operative_id, "contractor_id": contractor_id})


# ---------------------------------------------------------------------
# 3. Start an induction session for a project (creates the row that
#    the video/quiz/RAMS/badge steps all attach to)
# ---------------------------------------------------------------------
@induction_bp.route("/start", methods=["POST"])
def start_induction():
    data = request.get_json(force=True)
    operative_id = data.get("operative_id")
    project_id = data.get("project_id")
    if not operative_id or not project_id:
        return jsonify({"error": "operative_id and project_id required"}), 400

    created = (
        supabase.table("smc_inductions")
        .insert({"operative_id": operative_id, "project_id": project_id})
        .execute()
    )
    return jsonify(created.data[0])


@induction_bp.route("/video-watched", methods=["POST"])
def video_watched():
    data = request.get_json(force=True)
    induction_id = data.get("induction_id")
    supabase.table("smc_inductions").update({"video_watched_at": now_iso()}).eq("id", induction_id).execute()
    return jsonify({"ok": True})


@induction_bp.route("/quiz", methods=["POST"])
def submit_quiz():
    data = request.get_json(force=True)
    induction_id = data.get("induction_id")
    score = data.get("score", 0)
    total = data.get("total", 1)
    passed = score >= total  # require full marks; loosen if you want a pass threshold

    supabase.table("smc_inductions").update(
        {"quiz_score": score, "quiz_passed": passed}
    ).eq("id", induction_id).execute()

    return jsonify({"passed": passed})


# ---------------------------------------------------------------------
# 4. RAMS — fetch what's relevant to this operative's trade/contractor,
#    then record their signed briefing against a specific RAMS version.
# ---------------------------------------------------------------------
@induction_bp.route("/rams", methods=["GET"])
def get_relevant_rams():
    project_id = request.args.get("project_id")
    trade_id = request.args.get("trade_id")
    contractor_id = request.args.get("contractor_id")

    query = supabase.table("smc_rams").select("*").eq("project_id", project_id)
    if trade_id:
        query = query.eq("trade_id", trade_id)
    result = query.execute()
    return jsonify(result.data)


@induction_bp.route("/rams-sign", methods=["POST"])
def sign_rams():
    data = request.get_json(force=True)
    required = ["rams_id", "operative_id", "induction_id"]
    if any(not data.get(f) for f in required):
        return jsonify({"error": f"required: {', '.join(required)}"}), 400

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
    return jsonify(created.data[0])


# ---------------------------------------------------------------------
# 5. Sign in / out — the QR pass, usable on repeat visits without
#    redoing the full induction.
# ---------------------------------------------------------------------
@induction_bp.route("/sign-in", methods=["POST"])
def sign_in():
    data = request.get_json(force=True)
    induction_id = data.get("induction_id")
    supabase.table("smc_inductions").update(
        {"status": "signed_in", "signed_in_at": now_iso()}
    ).eq("id", induction_id).execute()
    return jsonify({"ok": True})


@induction_bp.route("/sign-out", methods=["POST"])
def sign_out():
    data = request.get_json(force=True)
    induction_id = data.get("induction_id")
    supabase.table("smc_inductions").update(
        {"status": "signed_out", "signed_out_at": now_iso()}
    ).eq("id", induction_id).execute()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------
# 6. Manager roster — everyone signed in today for a project, plus
#    who's overdue a sign-out. This is what the SMC dashboard tab reads.
# ---------------------------------------------------------------------
@induction_bp.route("/roster", methods=["GET"])
def roster():
    project_id = request.args.get("project_id")
    if not project_id:
        return jsonify({"error": "project_id required"}), 400

    result = (
        supabase.table("smc_inductions")
        .select("*, smc_operatives(name, phone, vehicle_reg, smc_contractors(name))")
        .eq("project_id", project_id)
        .order("signed_in_at", desc=True)
        .execute()
    )
    return jsonify(result.data)
