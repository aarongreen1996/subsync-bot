# blueprint.py
import os
import json
import uuid
import traceback
from datetime import datetime, timezone

from flask import (
    Blueprint, request, render_template, jsonify,
    redirect, url_for, send_file, abort, g
)
from supabase import create_client, Client
import io

from .checklist_data import STAGES, get_all_stages_summary, get_stage
from .email_utils import send_manager_notification, send_decision_to_subcontractor
from .qr_utils import generate_qr_png

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "pd-photos")
ADMIN_KEY = os.environ.get("PD_ADMIN_KEY", "change-me-in-env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

pd_bp = Blueprint(
    "pd",
    __name__,
    template_folder="templates",
    url_prefix="/pd",
)


def _require_admin():
    key = request.args.get("key") or request.headers.get("X-Admin-Key")
    if key != ADMIN_KEY:
        abort(403)


def _get_plot(token: str):
    res = supabase.table("pd_plots").select(
        "*, pd_sites(*)"
    ).eq("access_token", token).single().execute()
    if not res.data:
        abort(404)
    return res.data


def _get_submission_by_review_token(token: str):
    res = supabase.table("pd_submissions").select(
        "*, pd_plots(*, pd_sites(*))"
    ).eq("review_token", token).single().execute()
    if not res.data:
        abort(404)
    return res.data


def _upload_photo(file_obj, submission_id: str, item_id: str) -> tuple[str, str]:
    ext = "jpg"
    if hasattr(file_obj, "filename") and "." in file_obj.filename:
        ext = file_obj.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "webp", "heic"):
            ext = "jpg"
    path = f"{submission_id}/{item_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file_bytes = file_obj.read()
    supabase.storage.from_(STORAGE_BUCKET).upload(
        path, file_bytes, {"content-type": f"image/{ext}"}
    )
    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(path)
    return path, public_url


@pd_bp.route("/<token>", methods=["GET"])
def form(token: str):
    plot_row = _get_plot(token)
    plot = {
        "id": plot_row["id"],
        "plot_number": plot_row["plot_number"],
        "token": token,
    }
    site = {
        "name": plot_row["pd_sites"]["name"],
        "address": plot_row["pd_sites"].get("address", ""),
    }
    stages_summary = get_all_stages_summary()
    stages_json = json.dumps(STAGES, ensure_ascii=False)
    return render_template(
        "pd/form.html",
        plot=plot,
        site=site,
        stages_summary=stages_summary,
        stages_json=stages_json,
    )


@pd_bp.route("/<token>/submit", methods=["POST"])
def submit(token: str):
    plot_row = _get_plot(token)
    try:
        data_raw = request.form.get("data", "{}")
        data = json.loads(data_raw)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid form data"}), 400

    stage_number = int(data.get("stage_number", 0))
    if stage_number not in STAGES:
        return jsonify({"ok": False, "error": "Invalid stage"}), 400

    stage = STAGES[stage_number]
    sub_name = data.get("submitted_by_name", "").strip()
    sub_company = data.get("submitted_by_company", "").strip()
    sub_email = data.get("submitted_by_email", "").strip()

    if not all([sub_name, sub_company, sub_email]):
        return jsonify({"ok": False, "error": "Name, company and email are required"}), 400

    answers = data.get("answers", {})
    additional_notes = data.get("additional_notes", "")

    submission_id = str(uuid.uuid4())
    review_token = uuid.uuid4().hex + uuid.uuid4().hex[:8]

    sub_record = {
        "id": submission_id,
        "plot_id": plot_row["id"],
        "stage_number": stage_number,
        "stage_name": stage["name"],
        "submitted_by_name": sub_name,
        "submitted_by_company": sub_company,
        "submitted_by_email": sub_email,
        "answers": answers,
        "additional_notes": additional_notes,
        "status": "pending",
        "review_token": review_token,
    }
    supabase.table("pd_submissions").insert(sub_record).execute()

    photo_records = []
    for key, file_obj in request.files.items():
        if not key.startswith("photo_") or not file_obj.filename:
            continue
        item_id = key[len("photo_"):]
        item_label = next(
            (i["photo_label"] for i in stage.get("items", []) if i["id"] == item_id),
            item_id,
        )
        try:
            storage_path, public_url = _upload_photo(file_obj, submission_id, item_id)
            photo_records.append({
                "submission_id": submission_id,
                "item_id": item_id,
                "item_label": item_label,
                "storage_path": storage_path,
                "public_url": public_url,
            })
        except Exception as e:
            print(f"Photo upload error for {item_id}: {e}")

    if photo_records:
        supabase.table("pd_photos").insert(photo_records).execute()

    site = plot_row["pd_sites"]
    review_url = f"{os.environ.get('PD_BASE_URL', 'http://localhost:5000')}/pd/review/{review_token}"
    try:
        send_manager_notification(sub_record, plot_row, site, review_url)
    except Exception as e:
        print(f"Manager email error: {e}")

    return jsonify({"ok": True, "submission_id": submission_id})


@pd_bp.route("/submitted/<submission_id>")
def submitted(submission_id: str):
    res = supabase.table("pd_submissions").select(
        "*, pd_plots(plot_number, pd_sites(name))"
    ).eq("id", submission_id).single().execute()
    if not res.data:
        abort(404)
    sub = res.data
    return render_template("pd/submitted.html", sub=sub)


@pd_bp.route("/review/<review_token>", methods=["GET"])
def review(review_token: str):
    sub = _get_submission_by_review_token(review_token)
    plot = sub["pd_plots"]
    site = plot["pd_sites"]

    photos_res = supabase.table("pd_photos").select("*").eq(
        "submission_id", sub["id"]
    ).execute()
    photos = photos_res.data or []
    photos_by_item = {}
    for p in photos:
        photos_by_item.setdefault(p["item_id"], []).append(p["public_url"])

    stage = get_stage(sub["stage_number"])

    return render_template(
        "pd/review.html",
        sub=sub,
        plot=plot,
        site=site,
        stage=stage,
        photos_by_item=photos_by_item,
    )


@pd_bp.route("/review/<review_token>/decide", methods=["POST"])
def decide(review_token: str):
    sub = _get_submission_by_review_token(review_token)
    if sub["status"] != "pending":
        return jsonify({"ok": False, "error": "Already reviewed"}), 400

    decision = request.form.get("decision")
    manager_notes = request.form.get("manager_notes", "").strip()
    reviewed_by = request.form.get("reviewed_by", "Site Manager").strip()

    if decision not in ("approved", "rejected"):
        return jsonify({"ok": False, "error": "Invalid decision"}), 400

    supabase.table("pd_submissions").update({
        "status": decision,
        "manager_notes": manager_notes,
        "reviewed_by": reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", sub["id"]).execute()

    plot = sub["pd_plots"]
    site = plot["pd_sites"]
    try:
        send_decision_to_subcontractor(sub, plot, site, decision, manager_notes)
    except Exception as e:
        print(f"Subcontractor email error: {e}")

    return redirect(url_for("pd.review_done", review_token=review_token))


@pd_bp.route("/review/<review_token>/done")
def review_done(review_token: str):
    sub = _get_submission_by_review_token(review_token)
    plot = sub["pd_plots"]
    site = plot["pd_sites"]
    return render_template("pd/review_done.html", sub=sub, plot=plot, site=site)


@pd_bp.route("/admin")
def admin():
    _require_admin()
    sites_res = supabase.table("pd_sites").select("*").order("name").execute()
    sites = sites_res.data or []
    plots_res = supabase.table("pd_plots").select(
        "*, pd_sites(name)"
    ).order("created_at", desc=True).execute()
    plots = plots_res.data or []
    return render_template(
        "pd/admin.html",
        sites=sites,
        plots=plots,
        admin_key=ADMIN_KEY,
    )


@pd_bp.route("/admin/sites", methods=["POST"])
def admin_create_site():
    _require_admin()
    data = request.get_json() or request.form
    record = {
        "name": data.get("name", "").strip(),
        "address": data.get("address", "").strip(),
        "manager_name": data.get("manager_name", "").strip(),
        "manager_email": data.get("manager_email", "").strip(),
        "manager_phone": data.get("manager_phone", "").strip(),
    }
    if not record["name"] or not record["manager_email"]:
        return jsonify({"ok": False, "error": "Site name and manager email are required"}), 400
    res = supabase.table("pd_sites").insert(record).execute()
    return jsonify({"ok": True, "site": res.data[0]})


@pd_bp.route("/admin/plots", methods=["POST"])
def admin_create_plot():
    _require_admin()
    data = request.get_json() or request.form
    record = {
        "site_id": data.get("site_id", "").strip(),
        "plot_number": data.get("plot_number", "").strip(),
    }
    if not record["site_id"] or not record["plot_number"]:
        return jsonify({"ok": False, "error": "Site and plot number are required"}), 400
    res = supabase.table("pd_plots").insert(record).execute()
    return jsonify({"ok": True, "plot": res.data[0]})


@pd_bp.route("/admin/qr/<plot_id>")
def admin_qr(plot_id: str):
    _require_admin()
    res = supabase.table("pd_plots").select(
        "*, pd_sites(name)"
    ).eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    png_bytes = generate_qr_png(
        plot["access_token"],
        plot["plot_number"],
        plot["pd_sites"]["name"],
    )
    return send_file(
        io.BytesIO(png_bytes),
        mimetype="image/png",
        as_attachment=True,
        download_name=f"QR_Plot_{plot['plot_number']}.png",
    )


@pd_bp.route("/admin/submissions")
def admin_submissions():
    _require_admin()
    plot_id = request.args.get("plot_id")
    query = supabase.table("pd_submissions").select(
        "*, pd_plots(plot_number, pd_sites(name))"
    ).order("submitted_at", desc=True)
    if plot_id:
        query = query.eq("plot_id", plot_id)
    res = query.execute()
    return jsonify(res.data or [])
