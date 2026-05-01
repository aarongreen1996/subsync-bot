import os
import re
import base64
from flask import Blueprint, request, jsonify, send_file
from datetime import datetime
import requests as http_requests
from pdf_generator import generate_pdf
from urllib.parse import quote

dashboard_bp = Blueprint("dashboard", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "noreply@subsync.app")


def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json()


def db_patch(path, payload):
    http_requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        json=payload,
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )


def check_auth():
    return request.headers.get("X-Dashboard-Password", "") == DASHBOARD_PASSWORD


def slugify(text):
    text = str(text).strip().replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_\-]", "", text)[:25]


def normalise(number):
    """Ensure number is in whatsapp:+44... format and URL-safe."""
    number = number.strip()
    # Remove whatsapp: prefix temporarily to clean the number
    if number.startswith("whatsapp:"):
        number = number[9:]
    # Remove any leading spaces and ensure + prefix
    number = number.lstrip(" +")
    number = "whatsapp:+" + number
    return number.replace("+", "%2B")


# ── Serve dashboard ────────────────────────────────────────────────────────────
@dashboard_bp.route("/dashboard")
def serve_dashboard():
    return send_file("dashboard.html")


# ── Debug (temporary) ─────────────────────────────────────────────────────────
@dashboard_bp.route("/api/debug")
def debug():
    number = request.args.get("number", "")
    encoded = normalise(number)
    logs = db_get(f"site_logs?from_number=eq.{encoded}&status=eq.pending&limit=5")
    projects = db_get(f"projects?whatsapp_number=eq.{encoded}&status=eq.active&limit=5")
    company = db_get(f"companies?whatsapp_number=eq.{encoded}&limit=1")
    return jsonify({
        "number_received": number,
        "encoded": encoded,
        "logs_found": len(logs) if isinstance(logs, list) else logs,
        "projects_found": len(projects) if isinstance(projects, list) else projects,
        "company_found": len(company) if isinstance(company, list) else company,
        "sample_log": logs[0] if isinstance(logs, list) and logs else None,
    })


# ── Auth ──────────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/auth", methods=["POST"])
def auth():
    data = request.json or {}
    if data.get("password") == DASHBOARD_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Wrong password"}), 401


# ── Summary ───────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/summary")
def summary():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    number = request.args.get("number", "")
    encoded = normalise(number)

    logs = db_get(f"site_logs?from_number=eq.{encoded}&status=eq.pending&order=created_at.desc")
    projects = db_get(f"projects?whatsapp_number=eq.{encoded}&status=eq.active&order=site_name.asc")
    sent = db_get(f"site_logs?from_number=eq.{encoded}&status=eq.sent&order=created_at.desc&limit=10")
    company = db_get(f"companies?whatsapp_number=eq.{encoded}&limit=1")

    if not isinstance(logs, list):
        logs = []
    if not isinstance(projects, list):
        projects = []
    if not isinstance(sent, list):
        sent = []

    total_value = sum(float(l.get("cost_estimate") or 0) for l in logs)
    total_hours = sum(float(l.get("hours") or 0) for l in logs)

    by_site = {}
    for log in logs:
        site = log.get("site_name") or "Unassigned"
        if site not in by_site:
            by_site[site] = {"count": 0, "value": 0.0, "hours": 0.0, "logs": []}
        by_site[site]["count"] += 1
        by_site[site]["value"] += float(log.get("cost_estimate") or 0)
        by_site[site]["hours"] += float(log.get("hours") or 0)
        by_site[site]["logs"].append(log)

    return jsonify({
        "company": company[0] if isinstance(company, list) and company else {},
        "total_pending": len(logs),
        "total_value": total_value,
        "total_hours": total_hours,
        "sites": len(projects),
        "by_site": by_site,
        "projects": projects,
        "recent_sent": sent,
    })


# ── Generate PDF ──────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/generate", methods=["POST"])
def generate():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    site_name = data.get("site_name")
    doc_type = data.get("doc_type", "VARIATION")
    from_number = data.get("from_number", "")
    encoded = normalise(from_number)

    companies = db_get(f"companies?whatsapp_number=eq.{encoded}&limit=1")
    if not isinstance(companies, list) or not companies:
        return jsonify({"error": "Company not found"}), 404
    company = companies[0]
    company["whatsapp_number"] = from_number

    query = f"site_logs?from_number=eq.{encoded}&status=eq.pending&order=created_at.asc"
    if site_name and site_name != "Unassigned":
        query += f"&site_name=eq.{quote(site_name)}"
    logs = db_get(query)

    if not isinstance(logs, list) or not logs:
        return jsonify({"error": "No pending logs found for this site"}), 404

    prefix_map = {"VARIATION": "VO", "DAYWORK": "DS", "MATERIAL_ORDER": "PO"}
    title_map = {"VARIATION": "Variation Order", "DAYWORK": "Daywork Sheet", "MATERIAL_ORDER": "Purchase Order"}
    prefix = prefix_map.get(doc_type, "VO")
    doc_title = title_map.get(doc_type, "Variation Order")
    site_label = site_name or "All Sites"

    sent = db_get(f"site_logs?from_number=eq.{encoded}&status=eq.sent&select=id")
    doc_number = str(len(sent) + 1).zfill(3) if isinstance(sent, list) else "001"
    doc_ref = f"{prefix}-{doc_number}"
    filename = (
        f"{doc_ref}_{slugify(company.get('company_name', 'Co').split()[0])}"
        f"_{slugify(site_label)}_{datetime.now().strftime('%d%b%Y')}.pdf"
    )

    pdf_bytes = generate_pdf(company, logs, doc_title, doc_ref, site_label)

    upload_url = f"{SUPABASE_URL}/storage/v1/object/documents/{filename}"
    r = http_requests.post(
        upload_url, data=pdf_bytes,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/pdf"
        }
    )
    if r.status_code not in (200, 201):
        return jsonify({"error": f"Upload failed: {r.text}"}), 500

    pdf_url = f"{SUPABASE_URL}/storage/v1/object/public/documents/{filename}"

    for log in logs:
        db_patch(f"site_logs?id=eq.{log['id']}", {"status": "sent"})

    return jsonify({
        "ok": True,
        "pdf_url": pdf_url,
        "filename": filename,
        "doc_ref": doc_ref,
        "items": len(logs),
    })


# ── Send email ────────────────────────────────────────────────────────────────
@dashboard_bp.route("/api/send-email", methods=["POST"])
def send_email():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    if not RESEND_API_KEY:
        return jsonify({"error": "Email not set up. Add RESEND_API_KEY to Railway variables."}), 500

    data = request.json or {}
    to_email = data.get("to_email")
    pdf_url = data.get("pdf_url")
    doc_ref = data.get("doc_ref")
    site_name = data.get("site_name", "Site")
    company_name = data.get("company_name", "Company")
    filename = data.get("filename")

    pdf_r = http_requests.get(pdf_url)
    pdf_b64 = base64.b64encode(pdf_r.content).decode()

    payload = {
        "from": f"{company_name} <{FROM_EMAIL}>",
        "to": [to_email],
        "subject": f"{doc_ref} — {site_name} | {company_name}",
        "html": f"""
            <p>Dear Client,</p>
            <p>Please find attached <strong>{doc_ref}</strong> for works carried out at
            <strong>{site_name}</strong>.</p>
            <p>Please review and approve at your earliest convenience.</p>
            <br>
            <p>Kind regards,<br><strong>{company_name}</strong></p>
            <p style="color:#aaa;font-size:11px;">Sent via SubSync</p>
        """,
        "attachments": [{"filename": filename, "content": pdf_b64}],
    }

    r = http_requests.post(
        "https://api.resend.com/emails",
        json=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        }
    )

    if r.status_code in (200, 201):
        return jsonify({"ok": True})
    return jsonify({"error": f"Email failed: {r.text}"}), 500
