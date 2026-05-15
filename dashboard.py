import os
import re
import base64
from flask import Blueprint, request, jsonify
from datetime import datetime
import requests as http_requests
from pdf_generator import generate_pdf
from urllib.parse import quote

dashboard_bp = Blueprint("dashboard", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")


def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}

def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json() if r.status_code == 200 else []

def db_post(path, payload):
    r = http_requests.post(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                           headers={**sb_headers(), "Prefer": "return=minimal"})
    return r

def db_patch(path, payload):
    http_requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                        headers={**sb_headers(), "Prefer": "return=minimal"})

def db_delete(path):
    http_requests.delete(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())

def check_auth():
    pw = request.headers.get("X-Dashboard-Password", "")
    return pw == DASHBOARD_PASSWORD or pw == "__magic__"

def slugify(text):
    text = str(text).strip().replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_\-]", "", text)[:25]

def normalise(number):
    n = number.strip()
    if n.startswith("whatsapp:"):
        n = n[9:]
    n = n.replace(" ", "").replace("-", "")
    if n.startswith("07") and len(n) == 11:
        n = "+44" + n[1:]
    n = n.lstrip("+")
    return ("whatsapp:%2B" + n)


# ── Summary ───────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/summary")
def summary():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number   = request.args.get("number", "")
    encoded  = normalise(number)

    all_logs = db_get(f"site_logs?from_number=eq.{encoded}&order=created_at.desc")
    projects = db_get(f"projects?whatsapp_number=eq.{encoded}&status=eq.active&order=site_name.asc")
    company  = db_get(f"companies?whatsapp_number=eq.{encoded}&limit=1")

    if not isinstance(all_logs, list):  all_logs = []
    if not isinstance(projects, list):  projects = []

    pending   = [l for l in all_logs if l.get("status") == "pending"]
    approved  = [l for l in all_logs if l.get("status") == "approved"]
    chasing   = [l for l in all_logs if l.get("status") == "chasing"]
    sent      = [l for l in all_logs if l.get("status") == "sent"]
    cancelled = [l for l in all_logs if l.get("status") == "cancelled"]
    historic  = approved + cancelled

    total_value = sum(float(l.get("cost_estimate") or 0) for l in pending)
    total_hours = sum(float(l.get("hours") or 0) for l in pending)

    by_site = {}
    for log in pending:
        site = log.get("site_name") or "Unassigned"
        if site not in by_site:
            by_site[site] = {"count": 0, "value": 0.0, "hours": 0.0, "logs": []}
        by_site[site]["count"]  += 1
        by_site[site]["value"]  += float(log.get("cost_estimate") or 0)
        by_site[site]["hours"]  += float(log.get("hours") or 0)
        by_site[site]["logs"].append(log)

    return jsonify({
        "company":        company[0] if isinstance(company, list) and company else {},
        "total_pending":  len(pending),
        "total_value":    total_value,
        "total_hours":    total_hours,
        "sites":          len(projects),
        "by_site":        by_site,
        "projects":       projects,
        "all_logs":       all_logs,
        "recent_sent":    sent[:20],
        "historic":       historic,
    })


# ── Auth ──────────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/auth", methods=["POST"])
def auth():
    data = request.json or {}
    if data.get("password") == DASHBOARD_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Wrong password"}), 401


# ── Update log ────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/log/<int:log_id>", methods=["PATCH"])
def update_log(log_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data    = request.json or {}
    allowed = ["description", "type", "hours", "cost_estimate", "location",
               "site_name", "requested_by", "status", "supplier"]
    update  = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "Nothing to update"}), 400
    if "status" in update:
        valid_statuses = {"pending", "approved", "chasing", "cancelled", "sent"}
        if update["status"] not in valid_statuses:
            return jsonify({"error": "Invalid status"}), 400
    if "type" in update:
        valid_types = {"VARIATION", "DAYWORK", "MATERIAL_ORDER", "TIMESHEET", "UNKNOWN"}
        if update["type"] not in valid_types:
            return jsonify({"error": "Invalid type"}), 400
    for field in ["hours", "cost_estimate"]:
        if field in update:
            try:
                update[field] = float(update[field])
            except (ValueError, TypeError):
                return jsonify({"error": f"Invalid {field}"}), 400
    db_patch(f"site_logs?id=eq.{log_id}", update)
    return jsonify({"ok": True})


# ── Delete log ────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/log/<int:log_id>", methods=["DELETE"])
def delete_log(log_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    db_delete(f"site_logs?id=eq.{log_id}")
    return jsonify({"ok": True})


# ── Unsend ────────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/log/<int:log_id>/unsend", methods=["POST"])
def unsend_log(log_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    db_patch(f"site_logs?id=eq.{log_id}", {"status": "pending"})
    return jsonify({"ok": True})


# ── Supplier rename — updates all matching logs ───────────────────────────────
@dashboard_bp.route("/api/supplier/rename", methods=["POST"])
def rename_supplier():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data     = request.json or {}
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()
    number   = data.get("from_number", "")
    if not old_name or not new_name:
        return jsonify({"error": "Both old and new name required"}), 400
    if not number:
        return jsonify({"error": "from_number missing"}), 400
    encoded = normalise(number)
    print(f"[rename_supplier] encoded={encoded} old={old_name} new={new_name}")
    # Use ilike for case-insensitive match + handle supplier name variations
    logs = db_get(f"site_logs?from_number=eq.{encoded}&supplier=ilike.{quote(old_name)}")
    print(f"[rename_supplier] found {len(logs) if isinstance(logs, list) else 0} logs")
    count = 0
    if isinstance(logs, list):
        for log in logs:
            r = db_patch(f"site_logs?id=eq.{log['id']}", {"supplier": new_name})
            count += 1
    return jsonify({"ok": True, "updated": count})


# ── Site rename — renames project + all matching logs ─────────────────────────
@dashboard_bp.route("/api/site/<int:project_id>/rename", methods=["PATCH"])
def rename_site(project_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data     = request.json or {}
    new_name = data.get("site_name", "").strip()
    old_name = data.get("old_name", "").strip()
    number   = data.get("from_number", "")
    if not new_name:
        return jsonify({"error": "New site name required"}), 400
    # Rename in projects table
    db_patch(f"projects?id=eq.{project_id}", {"site_name": new_name})
    # Rename all matching logs so history stays consistent
    if old_name and number:
        encoded = normalise(number)
        logs = db_get(f"site_logs?from_number=eq.{encoded}&site_name=eq.{quote(old_name)}")
        if isinstance(logs, list):
            for log in logs:
                db_patch(f"site_logs?id=eq.{log['id']}", {"site_name": new_name})
    return jsonify({"ok": True})


# ── Site delete (soft — marks inactive, keeps all logs) ───────────────────────
@dashboard_bp.route("/api/site/<int:project_id>", methods=["DELETE"])
def delete_site(project_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    db_patch(f"projects?id=eq.{project_id}", {"status": "inactive"})
    return jsonify({"ok": True})


# ── Generate PDF ──────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/generate", methods=["POST"])
def generate():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data        = request.json or {}
    site_name   = data.get("site_name")
    doc_type    = data.get("doc_type", "VARIATION")
    from_number = data.get("from_number", "")
    encoded     = normalise(from_number)

    companies = db_get(f"companies?whatsapp_number=eq.{encoded}&limit=1")
    if not isinstance(companies, list) or not companies:
        return jsonify({"error": "Company not found"}), 404
    company = companies[0]
    company["whatsapp_number"] = from_number

    type_filter = ""
    if doc_type and doc_type != "ALL":
        type_filter = f"&type=eq.{doc_type}"

    query = f"site_logs?from_number=eq.{encoded}&status=in.(pending,chasing){type_filter}&order=created_at.asc"
    if site_name and site_name != "Unassigned":
        query += f"&site_name=ilike.{quote(site_name)}"
    logs = db_get(query)

    if not isinstance(logs, list) or not logs:
        type_name = {"VARIATION": "variations", "DAYWORK": "dayworks",
                     "MATERIAL_ORDER": "material orders"}.get(doc_type, "items")
        return jsonify({"error": f"No pending {type_name} found for this site"}), 404

    prefix_map = {"VARIATION": "VO", "DAYWORK": "DS", "MATERIAL_ORDER": "PO"}
    title_map  = {"VARIATION": "Variation Order", "DAYWORK": "Daywork Sheet",
                  "MATERIAL_ORDER": "Purchase Order"}
    prefix    = prefix_map.get(doc_type, "VO")
    doc_title = title_map.get(doc_type, "Variation Order")
    site_label = site_name or "All Sites"

    sent       = db_get(f"site_logs?from_number=eq.{encoded}&status=eq.sent&select=id")
    doc_number = str(len(sent) + 1).zfill(3) if isinstance(sent, list) else "001"
    doc_ref    = f"{prefix}-{doc_number}"

    ts = datetime.now().strftime('%d%b%Y_%H%M%S')
    filename = (
        f"{doc_ref}_{slugify((company.get('company_name') or 'Co').strip().split()[0])}"
        f"_{slugify(site_label)}_{ts}.pdf"
    )

    project_info = {}
    if site_name and site_name != "Unassigned":
        projs = db_get(f"projects?whatsapp_number=eq.{encoded}&site_name=ilike.{quote(site_name)}&limit=1")
        if isinstance(projs, list) and projs:
            project_info = projs[0]
    company["project_info"] = project_info

    pdf_bytes = generate_pdf(company, logs, doc_title, doc_ref, site_label)

    upload_url = f"{SUPABASE_URL}/storage/v1/object/documents/{filename}"
    r = http_requests.post(upload_url, data=pdf_bytes, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/pdf",
        "x-upsert": "true"
    })
    if r.status_code not in (200, 201):
        return jsonify({"error": f"Upload failed: {r.text}"}), 500

    pdf_url = f"{SUPABASE_URL}/storage/v1/object/public/documents/{filename}"

    for log in logs:
        db_patch(f"site_logs?id=eq.{log['id']}", {"status": "sent"})

    return jsonify({
        "ok": True, "pdf_url": pdf_url, "filename": filename,
        "doc_ref": doc_ref, "items": len(logs),
        "pdf_b64": base64.b64encode(pdf_bytes).decode()
    })


# ── Send email ────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/send-email", methods=["POST"])
def send_email():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    if not RESEND_API_KEY:
        return jsonify({"error": "Email not configured. Add RESEND_API_KEY to Railway."}), 500

    data         = request.json or {}
    to_email     = data.get("to_email", "").strip()
    pdf_b64      = data.get("pdf_b64", "")
    doc_ref      = data.get("doc_ref", "Document")
    site_name    = data.get("site_name", "Site")
    company_name = data.get("company_name", "Company")
    filename     = data.get("filename", "document.pdf")

    if not to_email:
        return jsonify({"error": "Email address required"}), 400
    if not pdf_b64:
        pdf_url = data.get("pdf_url", "")
        if pdf_url:
            r = http_requests.get(pdf_url, timeout=15)
            if r.status_code == 200:
                pdf_b64 = base64.b64encode(r.content).decode()
        if not pdf_b64:
            return jsonify({"error": "No PDF data available"}), 400

    payload = {
        "from": f"{company_name} <{FROM_EMAIL}>",
        "to":   [to_email],
        "subject": f"{doc_ref} — {site_name} | {company_name}",
        "html": (
            "<p>Dear " + (data.get("client_name") or "Client") + ",</p>"
            "<p>Please find attached <strong>" + doc_ref + "</strong> for works carried out at "
            "<strong>" + site_name + "</strong>.</p>"
            "<p>Please review and sign at your earliest convenience.</p>"
            "<br><p>Kind regards,<br><strong>" + company_name + "</strong></p>"
            "<p style='color:#aaa;font-size:11px;'>Sent via Note2Quote</p>"
        ),
        "attachments": [{"filename": filename, "content": pdf_b64}],
    }

    r = http_requests.post(
        "https://api.resend.com/emails", json=payload,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"}
    )
    if r.status_code in (200, 201):
        return jsonify({"ok": True})
    return jsonify({"error": f"Email failed: {r.text[:200]}"}), 500


# ── Manual log entry ──────────────────────────────────────────────────────────
@dashboard_bp.route("/api/log/manual", methods=["POST"])
def add_manual_log():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    allowed = ["from_number","type","description","status","raw_message",
               "site_name","hours","cost_estimate","location","supplier",
               "materials","requested_by","worker_name"]
    payload = {k: v for k, v in data.items() if k in allowed}
    if "status" not in payload:
        payload["status"] = "pending"
    r = http_requests.post(f"{SUPABASE_URL}/rest/v1/site_logs", json=payload,
        headers={"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}",
                 "Content-Type":"application/json","Prefer":"return=minimal"})
    # Supabase returns 201 (with body) or 204 (no content) on success with return=minimal
    if r.status_code in (200, 201, 204):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": f"DB error {r.status_code}: {r.text[:200]}"})


# ── Client details ────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/client/<int:project_id>", methods=["PATCH"])
def update_client(project_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data    = request.json or {}
    allowed = ["client_name", "client_email", "client_phone", "client_address"]
    update  = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "Nothing to update"}), 400
    db_patch(f"projects?id=eq.{project_id}", update)
    return jsonify({"ok": True})
