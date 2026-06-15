# blueprint.py
import os
import json
import uuid
from datetime import datetime, timezone

from flask import (
    Blueprint, request, render_template, jsonify,
    redirect, url_for, send_file, abort
)
from supabase import create_client, Client
import io

from .checklist_data import STAGES, get_all_stages_summary, get_stage, get_stages_by_group
import re as _re

def _natural_sort_plots(plots):
    def _key(p):
        parts = _re.split(r'(\d+)', str(p.get("plot_number") or ""))
        return [int(x) if x.isdigit() else x.lower() for x in parts]
    return sorted(plots, key=_key)
from .email_utils import send_manager_notification, send_decision_to_subcontractor, send_qs_notification
from .qr_utils import generate_qr_png

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "pd-photos")
ADMIN_KEY = os.environ.get("PD_ADMIN_KEY", "change-me-in-env")
BASE_URL = os.environ.get("PD_BASE_URL", "http://localhost:5000")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

pd_bp = Blueprint("pd", __name__, template_folder="templates", url_prefix="/pd")


def _require_admin():
    key = request.args.get("key") or request.headers.get("X-Admin-Key")
    if key != ADMIN_KEY:
        abort(403)


def _get_plot(token: str):
    res = supabase.table("pd_plots").select("*, pd_sites(*)").eq("access_token", token).single().execute()
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


def _upload_photo(file_obj, submission_id: str, item_id: str) -> tuple:
    ext = "jpg"
    if hasattr(file_obj, "filename") and "." in (file_obj.filename or ""):
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


# -- Subcontractor Form

@pd_bp.route("/<token>", methods=["GET"])
def form(token: str):
    plot_row = _get_plot(token)
    plot = {"id": plot_row["id"], "plot_number": plot_row["plot_number"], "token": token}
    site = {"name": plot_row["pd_sites"]["name"], "address": plot_row["pd_sites"].get("address", "")}
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    dwelling_category = plot_row.get("dwelling_category") or "house"

    # Fetch ALL visibility and override data in 2 queries instead of 74 (N+1 fix)
    try:
        vis_res = supabase.table("pd_stage_visibility").select("*").eq("tenant_id", tenant_id).execute()
        vis_map = {v["stage_number"]: v for v in (vis_res.data or [])}
    except Exception:
        vis_map = {}
    try:
        ov_res = supabase.table("pd_checklist_overrides").select("*").eq("tenant_id", tenant_id).execute()
        ov_by_stage = {}
        for o in (ov_res.data or []):
            ov_by_stage.setdefault(o["stage_number"], {})[o["item_id"]] = o
    except Exception:
        ov_by_stage = {}

    # Build stages with overrides applied, dropping any hidden for this dwelling type
    stages_with_overrides = {}
    for k, v in STAGES.items():
        merged = _apply_overrides(k, v, vis_map, ov_by_stage, dwelling_category)
        if merged is not None:
            stages_with_overrides[k] = merged

    # Add tenant custom stages
    custom_stages = _get_custom_stages_from_cache(vis_map, ov_by_stage, dwelling_category)
    stages_with_overrides.update(custom_stages)

    stages_json = json.dumps(stages_with_overrides, ensure_ascii=False)

    # Build stages_by_group safely — get_stages_by_group() may return a dict or list
    raw_groups = get_stages_by_group()
    stages_by_group = []
    if isinstance(raw_groups, dict):
        for group_name, stage_nums in raw_groups.items():
            visible = [n for n in stage_nums if n in stages_with_overrides]
            if visible:
                stages_by_group.append({"name": group_name, "stages": visible})
    else:
        for group in raw_groups:
            if isinstance(group, dict):
                visible = [n for n in group.get("stages", []) if n in stages_with_overrides]
                if visible:
                    stages_by_group.append({**group, "stages": visible})
    if custom_stages:
        stages_by_group.append({"name": "Custom Stages", "stages": list(custom_stages.keys())})

    # Load existing drafts for this plot so form can pre-fill
    try:
        drafts_res = supabase.table("pd_drafts").select("*").eq("plot_id", plot_row["id"]).execute()
        drafts_by_stage = {d["stage_number"]: d for d in (drafts_res.data or [])}
    except Exception:
        drafts_by_stage = {}

    # Load submission statuses per stage
    try:
        subs_res = supabase.table("pd_submissions").select(
            "stage_number, status, submitted_by_name, submitted_at, review_token"
        ).eq("plot_id", plot_row["id"]).order("submitted_at", desc=True).execute()
        stage_statuses = {}
        for s in (subs_res.data or []):
            sn = s["stage_number"]
            if sn not in stage_statuses:
                stage_statuses[sn] = {
                    "status":    s["status"],
                    "name":      s["submitted_by_name"],
                    "submitted": (s["submitted_at"] or "")[:10],
                    "review_token": s["review_token"],
                }
    except Exception:
        stage_statuses = {}

    drafts_json       = json.dumps(drafts_by_stage, ensure_ascii=False)
    stage_status_json = json.dumps(stage_statuses, ensure_ascii=False)

    return render_template(
        "pd/form.html",
        plot=plot,
        site=site,
        stages_by_group=stages_by_group,
        stages_json=stages_json,
        drafts_json=drafts_json,
        stage_status_json=stage_status_json,
    )


@pd_bp.route("/<token>/draft/photo", methods=["POST"])
def save_draft_photo(token: str):
    import uuid as _uuid
    plot_row = _get_plot(token)
    photo    = request.files.get("photo")
    item_id  = request.form.get("item_id", "")
    stage_number = request.form.get("stage_number", "0")

    if not photo or not item_id:
        return jsonify({"ok": False, "error": "Missing photo or item_id"}), 400

    try:
        ext        = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else "jpg"
        fname      = f"drafts/{plot_row['id']}/{stage_number}/{item_id}_{_uuid.uuid4().hex[:8]}.{ext}"
        photo_bytes = photo.read()

        SURL = os.environ.get("SUPABASE_URL", "").rstrip("/")
        SKEY = os.environ.get("SUPABASE_KEY", "")
        import requests as _req
        r = _req.post(
            f"{SURL}/storage/v1/object/pd-photos/{fname}",
            data=photo_bytes,
            headers={"apikey": SKEY, "Authorization": f"Bearer {SKEY}",
                     "Content-Type": f"image/{ext}", "x-upsert": "true"}
        )
        if r.status_code not in (200, 201):
            return jsonify({"ok": False, "error": "Upload failed"}), 500

        url = f"{SURL}/storage/v1/object/public/pd-photos/{fname}"
        return jsonify({"ok": True, "url": url, "item_id": item_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@pd_bp.route("/<token>/draft", methods=["POST"])
def save_draft(token: str):
    plot_row = _get_plot(token)
    try:
        data = json.loads(request.form.get("data", "{}"))
    except Exception:
        return jsonify({"ok": False, "error": "Invalid data"}), 400

    stage_number = int(data.get("stage_number", 0))
    if stage_number not in STAGES:
        return jsonify({"ok": False, "error": "Invalid stage"}), 400

    record = {
        "plot_id":             plot_row["id"],
        "stage_number":        stage_number,
        "device_id":           data.get("device_id", ""),
        "submitted_by_name":   data.get("submitted_by_name", ""),
        "submitted_by_company": data.get("submitted_by_company", ""),
        "submitted_by_email":  data.get("submitted_by_email", ""),
        "answers":             data.get("answers", {}),
        "photo_urls":          data.get("photo_urls", {}),
        "additional_notes":    data.get("additional_notes", ""),
        "updated_at":          datetime.now(timezone.utc).isoformat(),
    }

    try:
        supabase.table("pd_drafts").upsert(
            record, on_conflict="plot_id,stage_number"
        ).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@pd_bp.route("/<token>/draft/<int:stage_number>", methods=["DELETE"])
def delete_draft(token: str, stage_number: int):
    plot_row = _get_plot(token)
    try:
        supabase.table("pd_drafts").delete().eq(
            "plot_id", plot_row["id"]
        ).eq("stage_number", stage_number).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@pd_bp.route("/<token>/submit", methods=["POST"])
def submit(token: str):
    plot_row = _get_plot(token)
    try:
        data = json.loads(request.form.get("data", "{}"))
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

    submission_id = str(uuid.uuid4())
    review_token = uuid.uuid4().hex + uuid.uuid4().hex[:8]

    parent_id = data.get("parent_submission_id") or None
    revision  = 1
    if parent_id:
        try:
            parent_res = supabase.table("pd_submissions").select("revision_number").eq("id", parent_id).single().execute()
            if parent_res.data:
                revision = (parent_res.data.get("revision_number") or 1) + 1
            supabase.table("pd_submissions").update({"resubmit_token": None}).eq("id", parent_id).execute()
        except Exception:
            pass

    # Save signature image to storage
    signature_url = None
    sig_data = data.get("signature")
    if sig_data and sig_data.startswith("data:image/png;base64,"):
        try:
            import base64
            sig_bytes = base64.b64decode(sig_data.split(",", 1)[1])
            sig_path  = f"signatures/{submission_id}.png"
            supabase.storage.from_(STORAGE_BUCKET).upload(
                sig_path, sig_bytes, {"content-type": "image/png"}
            )
            signature_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(sig_path)
        except Exception as e:
            print(f"Signature upload error: {e}")

    sub_record = {
        "id": submission_id,
        "plot_id": plot_row["id"],
        "stage_number": stage_number,
        "stage_name": stage["name"],
        "submitted_by_name": sub_name,
        "submitted_by_company": sub_company,
        "submitted_by_email": sub_email,
        "answers": data.get("answers", {}),
        "additional_notes": data.get("additional_notes", ""),
        "status": "pending",
        "review_token": review_token,
        "revision_number": revision,
        "parent_submission_id": parent_id,
        "signature_url": signature_url,
    }
    supabase.table("pd_submissions").insert(sub_record).execute()

    photo_records = []
    draft_photo_urls_raw = request.form.get("draft_photo_urls", "{}")
    try:
        draft_photo_urls = json.loads(draft_photo_urls_raw)
    except Exception:
        draft_photo_urls = {}

    for item_id, url in draft_photo_urls.items():
        if not url: continue
        item_label = next(
            (i.get("text", item_id)[:60] for i in stage.get("items", []) if i["id"] == item_id),
            item_id,
        )
        photo_records.append({
            "submission_id": submission_id,
            "item_id":       item_id,
            "item_label":    item_label,
            "storage_path":  url,
            "public_url":    url,
        })

    for key, file_obj in request.files.items():
        if not key.startswith("photo_") or not getattr(file_obj, "filename", None):
            continue
        item_id = key[len("photo_"):]
        if item_id in draft_photo_urls:
            continue
        item_label = next(
            (i.get("text", item_id)[:60] for i in stage.get("items", []) if i["id"] == item_id),
            item_id,
        )
        try:
            storage_path, public_url = _upload_photo(file_obj, submission_id, item_id)
            photo_records.append({
                "submission_id": submission_id,
                "item_id":       item_id,
                "item_label":    item_label,
                "storage_path":  storage_path,
                "public_url":    public_url,
            })
        except Exception as e:
            print(f"Photo upload error {item_id}: {e}")

    if photo_records:
        supabase.table("pd_photos").insert(photo_records).execute()

    site = plot_row["pd_sites"]
    review_url = f"{BASE_URL}/pd/review/{review_token}"
    try:
        send_manager_notification(sub_record, plot_row, site, review_url)
    except Exception as e:
        print(f"Manager email error: {e}")

    try:
        supabase.table("pd_drafts").delete().eq(
            "plot_id", plot_row["id"]
        ).eq("stage_number", stage_number).execute()
    except Exception:
        pass

    return jsonify({"ok": True, "submission_id": submission_id})


@pd_bp.route("/submitted/<submission_id>")
def submitted(submission_id: str):
    res = supabase.table("pd_submissions").select(
        "*, pd_plots(plot_number, pd_sites(name))"
    ).eq("id", submission_id).single().execute()
    if not res.data:
        abort(404)
    return render_template("pd/submitted.html", sub=res.data)


# -- Site Manager Review

@pd_bp.route("/review/<review_token>", methods=["GET"])
def review(review_token: str):
    sub = _get_submission_by_review_token(review_token)
    plot = sub["pd_plots"]
    site = plot["pd_sites"]

    photos_res = supabase.table("pd_photos").select("*").eq("submission_id", sub["id"]).execute()
    photos_by_item = {}
    for p in (photos_res.data or []):
        photos_by_item.setdefault(p["item_id"], []).append(p["public_url"])

    stage = get_stage(sub["stage_number"])
    stage_items = stage.get("items", []) if stage else []
    stage_items_json = json.dumps([{"id": i["id"], "text": i["text"]} for i in stage_items])
    return render_template(
        "pd/review.html",
        sub=sub, plot=plot, site=site, stage=stage,
        stage_items_json=stage_items_json,
        photos_by_item=photos_by_item,
    )


@pd_bp.route("/review/<review_token>/decide", methods=["POST"])
def decide(review_token: str):
    sub = _get_submission_by_review_token(review_token)
    if sub["status"] != "pending":
        return redirect(url_for("pd.review_done", review_token=review_token))

    decision = request.form.get("decision")
    manager_notes = request.form.get("manager_notes", "").strip()
    reviewed_by = request.form.get("reviewed_by", "Site Manager").strip()

    if decision not in ("approved", "rejected"):
        return jsonify({"ok": False, "error": "Invalid decision"}), 400

    flagged_items = {}
    if decision == "rejected":
        try:
            flagged_items = json.loads(request.form.get("flagged_items", "{}"))
        except Exception:
            flagged_items = {}

    resubmit_token = None
    if decision == "rejected":
        resubmit_token = uuid.uuid4().hex + uuid.uuid4().hex[:8]

    updates = {
        "status": decision,
        "manager_notes": manager_notes,
        "reviewed_by": reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "flagged_items": flagged_items,
    }
    if resubmit_token:
        updates["resubmit_token"] = resubmit_token

    supabase.table("pd_submissions").update(updates).eq("id", sub["id"]).execute()

    plot = sub["pd_plots"]
    site = plot["pd_sites"]
    resubmit_url = f"{BASE_URL}/pd/resubmit/{resubmit_token}" if resubmit_token else None

    pdf_bytes = None
    if decision == "approved":
        try:
            from .pdf_generator import generate_submission_pdf
            tenant = _get_tenant()
            stage  = get_stage(sub["stage_number"])
            stage_items = stage.get("items", []) if stage else []
            photos_res  = supabase.table("pd_photos").select("*").eq("submission_id", sub["id"]).execute()
            photos_by_item = {}
            for p in (photos_res.data or []):
                photos_by_item.setdefault(p["item_id"], []).append(p["public_url"])
            updated = supabase.table("pd_submissions").select("*").eq("id", sub["id"]).single().execute()
            sub_for_pdf = updated.data if updated.data else sub
            pdf_bytes = generate_submission_pdf(sub_for_pdf, plot, site, stage_items, photos_by_item, tenant)
        except Exception as e:
            print(f"PDF generation for email error: {e}")

    try:
        send_decision_to_subcontractor(sub, plot, site, decision, manager_notes, flagged_items, resubmit_url, pdf_bytes)
    except Exception as e:
        print(f"Subcontractor email error: {e}")

    # If approved and the site has a QS contact, trigger secondary QS approval flow
    if decision == "approved" and site.get("qs_email"):
        qs_token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        supabase.table("pd_submissions").update({
            "qs_review_token": qs_token,
            "qs_status": "pending",
        }).eq("id", sub["id"]).execute()

        qs_review_url = f"{BASE_URL}/pd/qs-review/{qs_token}"
        try:
            updated_sub = supabase.table("pd_submissions").select("*").eq("id", sub["id"]).single().execute().data or sub
            send_qs_notification(updated_sub, plot, site, qs_review_url)
        except Exception as e:
            print(f"QS notification error: {e}")

    return redirect(url_for("pd.review_done", review_token=review_token))


@pd_bp.route("/review/<review_token>/done")
def review_done(review_token: str):
    sub = _get_submission_by_review_token(review_token)
    return render_template("pd/review_done.html", sub=sub, plot=sub["pd_plots"], site=sub["pd_plots"]["pd_sites"])


# -- QS Secondary Approval Flow

@pd_bp.route("/qs-review/<qs_token>", methods=["GET"])
def qs_review(qs_token: str):
    res = supabase.table("pd_submissions").select(
        "*, pd_plots(*, pd_sites(*))"
    ).eq("qs_review_token", qs_token).single().execute()
    if not res.data:
        abort(404)
    sub  = res.data
    plot = sub["pd_plots"]
    site = plot["pd_sites"]

    photos_res = supabase.table("pd_photos").select("*").eq("submission_id", sub["id"]).execute()
    photos_by_item = {}
    for p in (photos_res.data or []):
        photos_by_item.setdefault(p["item_id"], []).append(p["public_url"])

    stage = get_stage(sub["stage_number"])
    stage_items = stage.get("items", []) if stage else []
    stage_items_json = json.dumps([{"id": i["id"], "text": i["text"]} for i in stage_items])

    return render_template(
        "pd/qs_review.html",
        sub=sub, plot=plot, site=site, stage=stage,
        stage_items_json=stage_items_json,
        photos_by_item=photos_by_item,
    )


@pd_bp.route("/qs-review/<qs_token>/decide", methods=["POST"])
def qs_decide(qs_token: str):
    res = supabase.table("pd_submissions").select(
        "*, pd_plots(*, pd_sites(*))"
    ).eq("qs_review_token", qs_token).single().execute()
    if not res.data:
        abort(404)
    sub = res.data

    if sub.get("qs_status") != "pending":
        return redirect(url_for("pd.qs_review_done", qs_token=qs_token))

    decision    = request.form.get("decision")
    qs_notes    = request.form.get("manager_notes", "").strip()
    reviewed_by = request.form.get("reviewed_by", "QS").strip()

    if decision not in ("approved", "rejected"):
        return jsonify({"ok": False, "error": "Invalid decision"}), 400

    qs_signature_url = None
    sig_data = request.form.get("signature")
    if sig_data and sig_data.startswith("data:image/png;base64,"):
        try:
            import base64
            sig_bytes = base64.b64decode(sig_data.split(",", 1)[1])
            sig_path  = f"signatures/qs_{sub['id']}.png"
            supabase.storage.from_(STORAGE_BUCKET).upload(
                sig_path, sig_bytes, {"content-type": "image/png"}
            )
            qs_signature_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(sig_path)
        except Exception as e:
            print(f"QS signature upload error: {e}")

    updates = {
        "qs_status":      "approved" if decision == "approved" else "rejected",
        "qs_reviewed_by": reviewed_by,
        "qs_reviewed_at": datetime.now(timezone.utc).isoformat(),
        "qs_notes":       qs_notes,
    }
    if qs_signature_url:
        updates["qs_signature_url"] = qs_signature_url

    supabase.table("pd_submissions").update(updates).eq("id", sub["id"]).execute()

    return redirect(url_for("pd.qs_review_done", qs_token=qs_token))


@pd_bp.route("/qs-review/<qs_token>/done")
def qs_review_done(qs_token: str):
    res = supabase.table("pd_submissions").select(
        "*, pd_plots(*, pd_sites(*))"
    ).eq("qs_review_token", qs_token).single().execute()
    if not res.data:
        abort(404)
    sub  = res.data
    plot = sub["pd_plots"]
    site = plot["pd_sites"]
    return render_template("pd/qs_review_done.html", sub=sub, plot=plot, site=site)


# -- Resubmission flow

@pd_bp.route("/resubmit/<resubmit_token>", methods=["GET"])
def resubmit_form(resubmit_token: str):
    res = supabase.table("pd_submissions").select(
        "*, pd_plots(*, pd_sites(*))"
    ).eq("resubmit_token", resubmit_token).single().execute()
    if not res.data:
        abort(404)

    sub  = res.data
    plot_row = sub["pd_plots"]
    plot = {"id": plot_row["id"], "plot_number": plot_row["plot_number"], "token": plot_row["access_token"]}
    site = {"name": plot_row["pd_sites"]["name"], "address": plot_row["pd_sites"].get("address", "")}

    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    dwelling_category = plot_row.get("dwelling_category") or "house"

    try:
        vis_res = supabase.table("pd_stage_visibility").select("*").eq("tenant_id", tenant_id).execute()
        vis_map = {v["stage_number"]: v for v in (vis_res.data or [])}
    except Exception:
        vis_map = {}
    try:
        ov_res = supabase.table("pd_checklist_overrides").select("*").eq("tenant_id", tenant_id).execute()
        ov_by_stage = {}
        for o in (ov_res.data or []):
            ov_by_stage.setdefault(o["stage_number"], {})[o["item_id"]] = o
    except Exception:
        ov_by_stage = {}

    stages_with_overrides = {}
    for k, v in STAGES.items():
        merged = _apply_overrides(k, v, vis_map, ov_by_stage, dwelling_category)
        if merged is not None:
            stages_with_overrides[k] = merged

    custom_stages = _get_custom_stages_from_cache(vis_map, ov_by_stage, dwelling_category)
    stages_with_overrides.update(custom_stages)

    stages_json = json.dumps(stages_with_overrides, ensure_ascii=False)

    raw_groups = get_stages_by_group()
    stages_by_group = []
    if isinstance(raw_groups, dict):
        for group_name, stage_nums in raw_groups.items():
            visible = [n for n in stage_nums if n in stages_with_overrides]
            if visible:
                stages_by_group.append({"name": group_name, "stages": visible})
    else:
        for group in raw_groups:
            if isinstance(group, dict):
                visible = [n for n in group.get("stages", []) if n in stages_with_overrides]
                if visible:
                    stages_by_group.append({**group, "stages": visible})
    if custom_stages:
        stages_by_group.append({"name": "Custom Stages", "stages": list(custom_stages.keys())})

    previous_answers = json.dumps(sub.get("answers") or {})
    flagged_items    = json.dumps(sub.get("flagged_items") or {})

    return render_template(
        "pd/form.html",
        plot=plot,
        site=site,
        stages_by_group=stages_by_group,
        stages_json=stages_json,
        previous_answers=previous_answers,
        flagged_items=flagged_items,
        preselected_stage=sub.get("stage_number"),
        parent_submission_id=sub.get("id"),
        resubmit_token=resubmit_token,
        is_resubmit=True,
        manager_notes=sub.get("manager_notes", ""),
    )


# -- Admin

@pd_bp.route("/admin")
def admin():
    _require_admin()
    sites = supabase.table("pd_sites").select("*").order("name").execute().data or []
    plots = supabase.table("pd_plots").select("*, pd_sites(name)").order("created_at", desc=True).execute().data or []
    return render_template("pd/admin.html", sites=sites, plots=plots, admin_key=ADMIN_KEY)


@pd_bp.route("/admin/sites", methods=["POST"])
def admin_create_site():
    _require_admin()
    data = request.get_json() or request.form
    record = {
        "name": (data.get("name") or "").strip(),
        "address": (data.get("address") or "").strip(),
        "manager_name": (data.get("manager_name") or "").strip(),
        "manager_email": (data.get("manager_email") or "").strip(),
        "manager_phone": (data.get("manager_phone") or "").strip(),
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
        "site_id": (data.get("site_id") or "").strip(),
        "plot_number": (data.get("plot_number") or "").strip(),
    }
    if not record["site_id"] or not record["plot_number"]:
        return jsonify({"ok": False, "error": "Site and plot number are required"}), 400
    res = supabase.table("pd_plots").insert(record).execute()
    return jsonify({"ok": True, "plot": res.data[0]})


@pd_bp.route("/admin/submissions")
def admin_submissions():
    _require_admin()
    plot_id = request.args.get("plot_id")
    query = supabase.table("pd_submissions").select(
        "*, pd_plots(plot_number, pd_sites(name))"
    ).order("submitted_at", desc=True)
    if plot_id:
        query = query.eq("plot_id", plot_id)
    return jsonify(query.execute().data or [])


# -- Plot Progress & Photo Album

@pd_bp.route("/admin/plot/<plot_id>/progress")
def plot_progress(plot_id: str):
    _require_admin()
    res = supabase.table("pd_plots").select("*, pd_sites(*)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    site = plot["pd_sites"]

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


@pd_bp.route("/admin/photos")
def admin_photos():
    _require_admin()
    submission_id = request.args.get("submission_id")
    if not submission_id:
        return jsonify([])
    res = supabase.table("pd_photos").select("*").eq("submission_id", submission_id).execute()
    return jsonify(res.data or [])


# -- QR Sheet

@pd_bp.route("/admin/site/<site_id>/qr-sheet")
def qr_sheet(site_id: str):
    _require_admin()
    site_res = supabase.table("pd_sites").select("*").eq("id", site_id).single().execute()
    if not site_res.data:
        abort(404)
    site = site_res.data
    plots_res = supabase.table("pd_plots").select("*").eq("site_id", site_id).execute()
    plots = _natural_sort_plots(plots_res.data or [])
    from datetime import date
    return render_template(
        "pd/qr_sheet.html",
        site=site,
        plots=plots,
        admin_key=ADMIN_KEY,
        base_url=BASE_URL,
        now=date.today().strftime("%d %b %Y"),
    )


@pd_bp.route("/admin/qr/<plot_id>")
def admin_qr(plot_id: str):
    _require_admin()
    res = supabase.table("pd_plots").select("*, pd_sites(name)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    inline = request.args.get("inline") == "1"
    png_bytes = generate_qr_png(plot["access_token"], plot["plot_number"], plot["pd_sites"]["name"])
    return send_file(
        io.BytesIO(png_bytes), mimetype="image/png",
        as_attachment=not inline,
        download_name=f"QR_Plot_{plot['plot_number']}.png",
    )


# -- Plot Report

@pd_bp.route("/admin/plot/<plot_id>/report")
def plot_report(plot_id: str):
    _require_admin()
    res = supabase.table("pd_plots").select("*, pd_sites(*)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    site = plot["pd_sites"]

    from .checklist_data import STAGES, STAGE_GROUPS
    from datetime import date

    subs_res = supabase.table("pd_submissions").select("*").eq("plot_id", plot_id).order("submitted_at", desc=True).execute()
    subs = subs_res.data or []

    stage_map = {}
    for s in reversed(subs):
        stage_map[s["stage_number"]] = s

    stage_rows = []
    stage_to_group = {}
    for group_name, stage_nums in STAGE_GROUPS.items():
        for n in stage_nums:
            stage_to_group[n] = group_name

    approved_count = pending_count = rejected_count = 0
    for n in range(1, 38):
        stage    = STAGES.get(n, {})
        sub      = stage_map.get(n)
        status   = sub["status"] if sub else "not_started"
        if status == "approved":   approved_count += 1
        elif status == "pending":  pending_count  += 1
        elif status == "rejected": rejected_count += 1
        stage_rows.append({
            "stage_number": n,
            "stage_name":   stage.get("name", f"Stage {n}"),
            "applies_to":   stage.get("applies_to", ""),
            "group":        stage_to_group.get(n, ""),
            "status":       status,
            "submitted_by": sub.get("submitted_by_name", "") if sub else "",
            "company":      sub.get("submitted_by_company", "") if sub else "",
            "date":         (sub.get("submitted_at") or "")[:10] if sub else "",
            "reviewed_by":  sub.get("reviewed_by", "") if sub else "",
            "signature_url":sub.get("signature_url", "") if sub else "",
            "review_token": sub.get("review_token", "") if sub else "",
        })

    not_started = 37 - approved_count - pending_count - rejected_count
    pct = round((approved_count / 37) * 100)

    part_l_rows = []
    for sub in stage_map.values():
        if sub.get("status") != "approved":
            continue
        stage   = STAGES.get(sub["stage_number"], {})
        answers = sub.get("answers") or {}
        photos_res = supabase.table("pd_photos").select("*").eq("submission_id", sub["id"]).execute()
        photos_map = {p["item_id"]: p["public_url"] for p in (photos_res.data or [])}
        for item in stage.get("items", []):
            if "PART L" not in item.get("text", ""):
                continue
            a = answers.get(item["id"]) or {}
            part_l_rows.append({
                "stage_name": stage.get("name", ""),
                "item_text":  item["text"][:80],
                "answer":     a.get("value", "") if isinstance(a, dict) else "",
                "photo_url":  photos_map.get(item["id"], ""),
                "date":       (sub.get("submitted_at") or "")[:10],
            })

    return render_template(
        "pd/plot_report.html",
        plot=plot,
        site=site,
        stage_rows=stage_rows,
        part_l_rows=part_l_rows,
        approved_count=approved_count,
        pending_count=pending_count,
        rejected_count=rejected_count,
        not_started=not_started,
        pct=pct,
        admin_key=ADMIN_KEY,
        now=date.today().strftime("%d %b %Y"),
    )


@pd_bp.route("/admin/plot/<plot_id>/report/pdf")
def plot_report_pdf(plot_id: str):
    _require_admin()
    return _generate_plot_report_pdf(plot_id, send_response=True)


def _generate_plot_report_pdf(plot_id: str, send_response: bool = True):
    from .checklist_data import STAGES, STAGE_GROUPS
    from datetime import date

    res = supabase.table("pd_plots").select("*, pd_sites(*)").eq("id", plot_id).single().execute()
    if not res.data:
        abort(404)
    plot = res.data
    site = plot["pd_sites"]
    tenant = _get_tenant()

    subs_res = supabase.table("pd_submissions").select("*").eq("plot_id", plot_id).order("submitted_at", desc=True).execute()
    subs = subs_res.data or []
    stage_map = {}
    for s in reversed(subs):
        stage_map[s["stage_number"]] = s

    stage_to_group = {}
    for gn, nums in STAGE_GROUPS.items():
        for n in nums:
            stage_to_group[n] = gn

    from .pdf_generator import generate_plot_report_pdf
    pdf_bytes = generate_plot_report_pdf(plot, site, stage_map, STAGES, stage_to_group, tenant, supabase)

    filename = f"PD_Report_{site.get('name','').replace(' ','_')}_Plot{plot.get('plot_number','')}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes), mimetype="application/pdf",
        as_attachment=True, download_name=filename,
    )


# -- Checklist Editor Routes

@pd_bp.route("/admin/checklist-editor")
def checklist_editor():
    _require_admin()
    from .checklist_data import STAGES, STAGE_GROUPS
    stages_json = json.dumps({
        str(k): {
            "name": v["name"],
            "applies_to": v["applies_to"],
            "items": [{"id": i["id"], "text": i["text"], "photo": i.get("photo", False)} for i in v["items"]]
        }
        for k, v in STAGES.items()
    })
    groups_json = json.dumps([
        {"name": gn, "stages": sn} for gn, sn in STAGE_GROUPS.items()
    ])
    return render_template(
        "pd/checklist_editor.html",
        stages_json=stages_json,
        groups_json=groups_json,
        admin_key=ADMIN_KEY,
    )


@pd_bp.route("/admin/checklist/<int:stage_number>", methods=["GET"])
def checklist_get(stage_number: int):
    _require_admin()
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify({"overrides": []})
    res = supabase.table("pd_checklist_overrides").select("*").eq(
        "tenant_id", tenant_id
    ).eq("stage_number", stage_number).execute()
    return jsonify({"overrides": res.data or []})


@pd_bp.route("/admin/checklist/<int:stage_number>", methods=["POST"])
def checklist_save(stage_number: int):
    _require_admin()
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if not tenant_id:
        return jsonify({"ok": False, "error": "No tenant"}), 400

    items = request.get_json() or []
    if not isinstance(items, list):
        items = [items]

    for item in items:
        item_id = item.get("item_id")
        if not item_id:
            continue
        record = {
            "tenant_id":    tenant_id,
            "stage_number": stage_number,
            "item_id":      item_id,
        }
        if "text"              in item: record["text"]              = item["text"]
        if "photo_required"    in item: record["photo_required"]    = item["photo_required"]
        if "enabled"           in item: record["enabled"]           = item["enabled"]
        if "example_photo_url" in item: record["example_photo_url"] = item["example_photo_url"]
        if "example_guidance"  in item: record["example_guidance"]  = item["example_guidance"]
        if "sort_order"        in item: record["sort_order"]        = item["sort_order"]
        record["updated_at"] = datetime.now(timezone.utc).isoformat()

        supabase.table("pd_checklist_overrides").upsert(
            record, on_conflict="tenant_id,stage_number,item_id"
        ).execute()

    return jsonify({"ok": True})


@pd_bp.route("/admin/checklist/<int:stage_number>/reset", methods=["DELETE"])
def checklist_reset_stage(stage_number: int):
    _require_admin()
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if tenant_id:
        supabase.table("pd_checklist_overrides").delete().eq(
            "tenant_id", tenant_id
        ).eq("stage_number", stage_number).execute()
    return jsonify({"ok": True})


@pd_bp.route("/admin/checklist/item/<item_id>/reset", methods=["DELETE"])
def checklist_reset_item(item_id: str):
    _require_admin()
    tenant = _get_tenant()
    tenant_id = tenant.get("id")
    if tenant_id:
        supabase.table("pd_checklist_overrides").delete().eq(
            "tenant_id", tenant_id
        ).eq("item_id", item_id).execute()
    return jsonify({"ok": True})


@pd_bp.route("/admin/checklist/upload-example", methods=["POST"])
def checklist_upload_example():
    _require_admin()
    if "photo" not in request.files:
        return jsonify({"ok": False, "error": "No file"}), 400
    file_obj  = request.files["photo"]
    item_id   = request.form.get("item_id", "unknown")
    stage_num = request.form.get("stage_number", "0")
    ext = "jpg"
    if "." in (file_obj.filename or ""):
        ext = file_obj.filename.rsplit(".", 1)[-1].lower()
    path = f"examples/stage_{stage_num}/{item_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file_bytes = file_obj.read()
    supabase.storage.from_(STORAGE_BUCKET).upload(
        path, file_bytes, {"content-type": f"image/{ext}"}
    )
    url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(path)
    return jsonify({"ok": True, "url": url})


def _apply_overrides(stage_number: int, stage: dict, vis_map: dict, ov_by_stage: dict, dwelling_category: str = "house"):
    """Apply pre-fetched visibility + override data to a stage. Returns None if hidden."""
    import copy
    vis = vis_map.get(stage_number, {})
    if vis.get("hidden_globally"):
        return None
    if dwelling_category in (vis.get("hidden_for") or []):
        return None

    stage_copy = copy.deepcopy(stage)
    if vis.get("name_override"):
        stage_copy["name"] = vis["name_override"]
    if vis.get("applies_to_override"):
        stage_copy["applies_to"] = vis["applies_to_override"]

    ov_map = ov_by_stage.get(stage_number, {})
    merged = []
    for item in stage_copy.get("items", []):
        ov = ov_map.get(item["id"], {})
        if ov.get("enabled") is False:
            continue
        if dwelling_category in (ov.get("hidden_for") or []):
            continue
        if "text" in ov and ov["text"]:
            item["text"] = ov["text"]
        if "photo_required" in ov and ov["photo_required"] is not None:
            item["photo"] = bool(ov["photo_required"])
        if ov.get("example_photo_url"):
            item["example_photo_url"] = ov["example_photo_url"]
        if ov.get("example_guidance"):
            item["example_guidance"] = ov["example_guidance"]
        merged.append(item)

    default_ids = {i["id"] for i in stage.get("items", [])}
    for item_id, ov in ov_map.items():
        if item_id in default_ids:
            continue
        if not (ov.get("is_custom") or item_id.startswith("custom_")):
            continue
        if ov.get("enabled") is False:
            continue
        if dwelling_category in (ov.get("hidden_for") or []):
            continue
        merged.append({
            "id": item_id, "text": ov.get("text", ""), "type": "check",
            "photo": ov.get("photo_required", True),
            "example_photo_url": ov.get("example_photo_url", ""),
            "example_guidance": ov.get("example_guidance", ""),
        })

    stage_copy["items"] = merged
    return stage_copy


def _get_custom_stages_from_cache(vis_map: dict, ov_by_stage: dict, dwelling_category: str = "house") -> dict:
    """Return tenant custom stages (number >= 1000) using pre-fetched cache data."""
    custom = {}
    for n, v in vis_map.items():
        if n < 1000:
            continue
        if v.get("hidden_globally"):
            continue
        if dwelling_category in (v.get("hidden_for") or []):
            continue
        items = []
        for item_id, ov in ov_by_stage.get(n, {}).items():
            if ov.get("enabled") is False:
                continue
            if dwelling_category in (ov.get("hidden_for") or []):
                continue
            items.append({
                "id": item_id, "text": ov.get("text", ""), "type": "check",
                "photo": ov.get("photo_required", True),
                "example_photo_url": ov.get("example_photo_url", ""),
                "example_guidance": ov.get("example_guidance", ""),
            })
        custom[n] = {
            "name": v.get("name_override") or f"Custom Stage {n}",
            "applies_to": v.get("applies_to_override", ""),
            "items": items,
        }
    return custom


def _get_stage_with_overrides(stage_number: int, tenant_id: str = None, dwelling_category: str = "house"):
    """Return stage merged with tenant overrides and filtered for the given
    dwelling type. Returns None if the stage is hidden entirely or hidden for
    this dwelling type."""
    from .checklist_data import get_stage
    stage = get_stage(stage_number)
    if not stage:
        return None
    if not tenant_id:
        return stage

    import copy
    stage_copy = copy.deepcopy(stage)

    # Stage-level visibility / name overrides
    try:
        vis_res = supabase.table("pd_stage_visibility").select("*").eq(
            "tenant_id", tenant_id
        ).eq("stage_number", stage_number).execute()
        vis = vis_res.data[0] if vis_res.data else {}
    except Exception:
        vis = {}

    if vis.get("hidden_globally"):
        return None
    hidden_for = vis.get("hidden_for") or []
    if dwelling_category in hidden_for:
        return None
    if vis.get("name_override"):
        stage_copy["name"] = vis["name_override"]
    if vis.get("applies_to_override"):
        stage_copy["applies_to"] = vis["applies_to_override"]

    # Item-level overrides
    try:
        res = supabase.table("pd_checklist_overrides").select("*").eq(
            "tenant_id", tenant_id
        ).eq("stage_number", stage_number).execute()
        ov_map = {o["item_id"]: o for o in (res.data or [])}
    except Exception:
        ov_map = {}

    merged = []
    for item in stage_copy.get("items", []):
        ov = ov_map.get(item["id"], {})
        if ov.get("enabled") is False:
            continue
        item_hidden_for = ov.get("hidden_for") or []
        if dwelling_category in item_hidden_for:
            continue
        if "text" in ov and ov["text"]:
            item["text"] = ov["text"]
        if "photo_required" in ov and ov["photo_required"] is not None:
            item["photo"] = bool(ov["photo_required"])
        if ov.get("example_photo_url"):
            item["example_photo_url"] = ov["example_photo_url"]
        if ov.get("example_guidance"):
            item["example_guidance"] = ov["example_guidance"]
        merged.append(item)

    # Custom items
    default_ids = {i["id"] for i in stage.get("items", [])}
    for item_id, ov in ov_map.items():
        if item_id in default_ids:
            continue
        if not (ov.get("is_custom") or item_id.startswith("custom_")):
            continue
        if ov.get("enabled") is False:
            continue
        item_hidden_for = ov.get("hidden_for") or []
        if dwelling_category in item_hidden_for:
            continue
        merged.append({
            "id":    item_id,
            "text":  ov.get("text", ""),
            "type":  "check",
            "photo": ov.get("photo_required", True),
            "example_photo_url": ov.get("example_photo_url", ""),
            "example_guidance":  ov.get("example_guidance", ""),
        })

    stage_copy["items"] = merged
    return stage_copy


def _get_custom_stages(tenant_id: str, dwelling_category: str = "house") -> dict:
    """Return tenant-created custom stages (stage_number >= 1000) not hidden
    for this dwelling type."""
    if not tenant_id:
        return {}
    try:
        vis_res = supabase.table("pd_stage_visibility").select("*").eq(
            "tenant_id", tenant_id
        ).gte("stage_number", 1000).execute()
        vis_rows = vis_res.data or []
    except Exception:
        vis_rows = []

    if not vis_rows:
        return {}

    try:
        ov_res = supabase.table("pd_checklist_overrides").select("*").eq(
            "tenant_id", tenant_id
        ).execute()
        overrides = ov_res.data or []
    except Exception:
        overrides = []

    ov_by_stage = {}
    for o in overrides:
        ov_by_stage.setdefault(o["stage_number"], {})[o["item_id"]] = o

    custom = {}
    for v in vis_rows:
        n = v["stage_number"]
        if v.get("hidden_globally"):
            continue
        if dwelling_category in (v.get("hidden_for") or []):
            continue
        items = []
        for item_id, ov in ov_by_stage.get(n, {}).items():
            if ov.get("enabled") is False:
                continue
            if dwelling_category in (ov.get("hidden_for") or []):
                continue
            items.append({
                "id": item_id,
                "text": ov.get("text", ""),
                "type": "check",
                "photo": ov.get("photo_required", True),
                "example_photo_url": ov.get("example_photo_url", ""),
                "example_guidance":  ov.get("example_guidance", ""),
            })
        custom[n] = {
            "name": v.get("name_override") or f"Custom Stage {n}",
            "applies_to": v.get("applies_to_override", ""),
            "items": items,
        }
    return custom


# -- Create first portal user

@pd_bp.route("/admin/create-user", methods=["POST"])
def admin_create_user():
    _require_admin()
    import hashlib, json as _json
    data     = request.get_json() or {}
    name     = (data.get("name") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role     = data.get("role") or "tenant_admin"

    if not name or not email or not password:
        return jsonify({"ok": False, "error": "Name, email and password required"}), 400

    tenant_res = supabase.table("pd_tenants").select("id").limit(1).execute()
    if not tenant_res.data:
        return jsonify({"ok": False, "error": "No tenant found — run schema_update.sql first"}), 400
    tenant_id = tenant_res.data[0]["id"]

    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    try:
        res = supabase.table("pd_users").insert({
            "tenant_id":     tenant_id,
            "name":          name,
            "email":         email,
            "password_hash": pw_hash,
            "role":          role,
            "site_ids":      _json.dumps([]),
        }).execute()
        return jsonify({"ok": True, "user": res.data[0]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# -- PDF generation

@pd_bp.route("/review/<review_token>/pdf")
def submission_pdf(review_token: str):
    sub = _get_submission_by_review_token(review_token)
    plot = sub["pd_plots"]
    site = plot["pd_sites"]

    photos_res = supabase.table("pd_photos").select("*").eq("submission_id", sub["id"]).execute()
    photos_by_item = {}
    for p in (photos_res.data or []):
        photos_by_item.setdefault(p["item_id"], []).append(p["public_url"])

    stage = get_stage(sub["stage_number"])
    stage_items = stage.get("items", []) if stage else []
    tenant = _get_tenant()

    from .pdf_generator import generate_submission_pdf
    pdf_bytes = generate_submission_pdf(sub, plot, site, stage_items, photos_by_item, tenant)

    filename = f"PD_{site.get('name','').replace(' ','_')}_Plot{plot.get('plot_number','')}_Stage{sub.get('stage_number','')}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


def _get_tenant() -> dict:
    try:
        res = supabase.table("pd_tenants").select("*").limit(1).execute()
        return res.data[0] if res.data else {}
    except Exception:
        return {}


def _upload_logo(file_obj) -> str:
    ext = "png"
    if hasattr(file_obj, "filename") and "." in (file_obj.filename or ""):
        ext = file_obj.filename.rsplit(".", 1)[-1].lower()
    path = f"logos/tenant_logo_{uuid.uuid4().hex[:8]}.{ext}"
    file_bytes = file_obj.read()
    supabase.storage.from_(STORAGE_BUCKET).upload(
        path, file_bytes, {"content-type": f"image/{ext}"}
    )
    return supabase.storage.from_(STORAGE_BUCKET).get_public_url(path)


# -- Settings

@pd_bp.route("/admin/settings", methods=["GET"])
def admin_settings():
    _require_admin()
    tenant = _get_tenant()
    return jsonify({"ok": True, "tenant": tenant})


@pd_bp.route("/admin/settings", methods=["POST"])
def admin_settings_save():
    _require_admin()
    tenant = _get_tenant()

    logo_url = tenant.get("logo_url", "")
    if "logo" in request.files and request.files["logo"].filename:
        try:
            logo_url = _upload_logo(request.files["logo"])
        except Exception as e:
            print(f"Logo upload error: {e}")

    data = request.form
    updates = {
        "name":            (data.get("name") or "").strip() or tenant.get("name", ""),
        "address":         (data.get("address") or "").strip(),
        "phone":           (data.get("phone") or "").strip(),
        "email":           (data.get("email") or "").strip(),
        "website":         (data.get("website") or "").strip(),
        "primary_color":   (data.get("primary_color") or "#1a1a2e").strip(),
        "secondary_color": (data.get("secondary_color") or "#C5962A").strip(),
        "logo_url":        logo_url,
    }

    if tenant.get("id"):
        supabase.table("pd_tenants").update(updates).eq("id", tenant["id"]).execute()
    else:
        supabase.table("pd_tenants").insert(updates).execute()

    return jsonify({"ok": True})
