import os
import json
import uuid
import re
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from supabase import create_client
from datetime import datetime
import requests as http_requests
from pdf_generator import generate_pdf
from dashboard import dashboard_bp
from onboarding import onboarding_bp
from admin import admin_bp
from scheduler import start_scheduler
from account import account_bp

app = Flask(__name__)
app.register_blueprint(dashboard_bp)
app.register_blueprint(onboarding_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(account_bp)

# Start background scheduler
start_scheduler()

# ── Clients ───────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# ── Helpers ───────────────────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json()

def db_post(path, payload):
    r = http_requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        json=payload,
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )
    if r.status_code not in (200, 201):
        raise Exception(f"DB Error {r.status_code}: {r.text}")

def db_patch(path, payload):
    http_requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        json=payload,
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )

def encode_number(number):
    return number.replace("+", "%2B")

def encode_text(text):
    from urllib.parse import quote
    return quote(str(text), safe="")

def get_projects(from_number):
    return db_get(
        f"projects?whatsapp_number=eq.{encode_number(from_number)}"
        f"&status=eq.active&order=site_name.asc"
    )

def match_site(msg, projects):
    msg_lower = msg.lower()
    for p in projects:
        if p["site_name"].lower() in msg_lower:
            return p["site_name"]
        if p.get("client_name", "").lower() in msg_lower:
            return p["site_name"]
    return None

def format_project_list(projects):
    lines = ["Which site is this for? Reply with the number:\n"]
    for i, p in enumerate(projects, 1):
        lines.append(f"{i}. {p['site_name']}")
    lines.append("\nOr type the site name directly.")
    return "\n".join(lines)

pending_selections = {}

# ── AI Prompt ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are an admin assistant for UK construction subcontractors.
Workers send you informal voice-note transcriptions or text messages from site.
Your job is to extract structured data and classify each message.

Classify into one of:
- VARIATION      → Extra work requested by client or site manager, not in original contract
- DAYWORK        → Time-based extra work; worker logging hours spent on an extra task
- MATERIAL_ORDER → Request to order materials, fixings, tools or equipment
- TIMESHEET      → Worker logging their standard hours for the day/week
- UNKNOWN        → Cannot classify

Respond ONLY with a valid JSON object. No explanation, no markdown, just raw JSON.

JSON structure:
{
  "type": "VARIATION",
  "description": "Short clear description of the task or item",
  "hours": 2.5,
  "cost_estimate": 40.00,
  "location": "Room 4",
  "site_name": "Site name if clearly mentioned in message, otherwise null",
  "requested_by": "Name of person who asked (if mentioned)",
  "worker_name": "Name of worker logging this (if mentioned)",
  "materials": ["item 1", "item 2"],
  "supplier": "Supplier name if mentioned",
  "confirmation_message": "A friendly WhatsApp reply summarising what was captured. Start with ✅. Under 3 lines. Use £ for currency."
}

Only include fields relevant to the type. Always include confirmation_message.
"""

GENERATE_KEYWORDS = [
    "generate", "create invoice", "make invoice", "send invoice",
    "variation report", "daywork report", "material report",
    "weekly report", "end of day report", "end of week",
    "produce report", "get variations", "send variations"
]

HELP_KEYWORDS = ["help", "guide", "how do i", "what can you do", "commands"]
DASHBOARD_KEYWORDS = ["dashboard", "my password", "password", "login", "log in", "sign in", "portal"]

def is_generate_command(msg):
    return any(kw in msg.lower() for kw in GENERATE_KEYWORDS)

def is_dashboard_command(msg):
    return any(kw in msg.lower() for kw in DASHBOARD_KEYWORDS)

def is_help_command(msg):
    return any(kw in msg.lower() for kw in HELP_KEYWORDS)

def detect_doc_type(msg):
    msg_lower = msg.lower()
    if "daywork" in msg_lower:
        return "DAYWORK", "Daywork Sheet", "DS"
    if "material" in msg_lower or "order" in msg_lower or "purchase" in msg_lower:
        return "MATERIAL_ORDER", "Purchase Order", "PO"
    return "VARIATION", "Variation Order", "VO"

def slugify(text):
    text = text.strip().replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_\-]", "", text)[:25]

def make_doc_ref_and_filename(company, logs, prefix, site_name):
    sent = db_get(
        f"site_logs?from_number=eq.{encode_number(company.get('whatsapp_number',''))}"
        f"&status=eq.sent&select=id"
    )
    doc_number   = str(len(sent) + 1).zfill(3)
    site_slug    = slugify(site_name) if site_name else "AllSites"
    company_slug = slugify(company.get("company_name", "Company").split()[0])
    date_str     = datetime.now().strftime("%d%b%Y")
    ref_str      = f"{prefix}-{doc_number}"
    filename     = f"{ref_str}_{company_slug}_{site_slug}_{date_str}.pdf"
    return ref_str, filename

def upload_pdf(pdf_bytes, filename):
    url = f"{SUPABASE_URL}/storage/v1/object/documents/{filename}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/pdf",
    }
    r = http_requests.post(url, data=pdf_bytes, headers=headers)
    if r.status_code not in (200, 201):
        raise Exception(f"Storage upload failed {r.status_code}: {r.text}")
    return f"{SUPABASE_URL}/storage/v1/object/public/documents/{filename}"

HELP_TEXT = """👷 *SubSync Bot — Quick Guide*

*LOGGING ITEMS*
Just describe what happened naturally:
• "Site manager asked me to fit extra sockets in room 4, 2 hrs, £40 materials"
• "Need to order 50 joist hangers from Travis Perkins"
• "Logged 8 hours today on Brookfield Site"

*MENTIONING THE SITE*
Include the site name in your message:
• "Variation on Brookfield Site — extra spotlights kitchen"
• "Flat 3 Refurb — boarded loft, 3 hrs, £60"

If you forget, I'll ask you which site it's for.

*GENERATING DOCUMENTS*
• "Generate variations for Brookfield Site"
• "Generate dayworks for Flat 3 Refurb"
• "Generate purchase orders for Brookfield Site"

*DOCUMENT TYPES*
• *Variation Order (VO)* — extra work not in contract
• *Daywork Sheet (DS)* — time-based extra work
• *Purchase Order (PO)* — materials & equipment

Reply *Help* anytime to see this guide again."""

def read_html(filename):
    """Read an HTML file from the same directory as this script."""
    base = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base, filename)
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number  = request.form.get("From", "")

    if not incoming_msg:
        return _reply("Send me a message about what happened on site today 👷")

    if from_number in pending_selections:
        return handle_site_selection(from_number, incoming_msg)

    if is_dashboard_command(incoming_msg):
        return handle_dashboard_command(from_number)

    if is_help_command(incoming_msg):
        return _reply(HELP_TEXT)

    if is_generate_command(incoming_msg):
        return handle_generate(from_number, incoming_msg)

    return handle_log(from_number, incoming_msg)



def handle_dashboard_command(from_number):
    app_url  = os.environ.get("APP_URL", "https://www.subsync.co.uk")
    password = os.environ.get("DASHBOARD_PASSWORD", "changeme")
    number   = from_number.replace("whatsapp:", "")
    msg = (
        "Dashboard link: " + app_url + "/dashboard\n"
        "Number: " + number + "\n"
        "Password: " + password + "\n\n"
        "Bookmark it so you can check in anytime"
    )
    return _reply(msg)

def handle_site_selection(from_number, msg):
    state    = pending_selections[from_number]
    projects = state["projects"]
    log_data = state["pending_log"]

    site_name = None
    if msg.strip().isdigit():
        idx = int(msg.strip()) - 1
        if 0 <= idx < len(projects):
            site_name = projects[idx]["site_name"]

    if not site_name:
        msg_lower = msg.lower()
        for p in projects:
            if p["site_name"].lower() in msg_lower or msg_lower in p["site_name"].lower():
                site_name = p["site_name"]
                break

    if not site_name:
        return _reply(
            "I didn't recognise that site. Please reply with a number:\n\n"
            + "\n".join([f"{i+1}. {p['site_name']}" for i, p in enumerate(projects)])
        )

    log_data["site_name"] = site_name
    del pending_selections[from_number]

    try:
        db_post("site_logs", log_data)
        return _reply(
            f"✅ Logged for *{site_name}*!\n"
            f"{log_data.get('description', 'Item')} saved."
        )
    except Exception as e:
        return _reply(f"⚠️ Couldn't save. Please try again. ({str(e)[:80]})")


def handle_generate(from_number, msg):
    try:
        companies = db_get(f"companies?whatsapp_number=eq.{encode_number(from_number)}&limit=1")
        if not companies:
            return _reply("⚠️ Your company isn't registered yet. Contact your admin.")
        company = companies[0]
        company["whatsapp_number"] = from_number

        log_type, doc_title, prefix = detect_doc_type(msg)
        projects   = get_projects(from_number)
        site_name  = match_site(msg, projects)
        site_label = site_name or "All Sites"

        query = (f"site_logs?from_number=eq.{encode_number(from_number)}"
                 f"&status=eq.pending&order=created_at.asc")
        if site_name:
            query += f"&site_name=eq.{encode_text(site_name)}"

        logs = db_get(query)

        if not logs:
            if site_name:
                return _reply(f"📋 No pending items for *{site_name}*.")
            return _reply("📋 No pending items found.")

        doc_ref, filename = make_doc_ref_and_filename(company, logs, prefix, site_name)
        company["site_label"] = site_label
        pdf_bytes = generate_pdf(company, logs, doc_title, doc_ref, site_label)
        pdf_url   = upload_pdf(pdf_bytes, filename)

        for log in logs:
            db_patch(f"site_logs?id=eq.{log['id']}", {"status": "sent"})

        resp = MessagingResponse()
        m = resp.message(
            f"📄 *{doc_ref}* — {doc_title}\n"
            f"Site: {site_label} · {len(logs)} item(s)\n"
            f"File: {filename}\n"
            f"Review and forward to your client ✅"
        )
        m.media(pdf_url)
        return Response(str(resp), mimetype="application/xml")

    except Exception as e:
        return _reply(f"⚠️ Couldn't generate document. Error: {str(e)[:150]}")


def handle_log(from_number, incoming_msg):
    try:
        ai_response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": incoming_msg}]
        )

        raw_json = ai_response.content[0].text.strip()
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
        raw_json = raw_json.strip()

        data = json.loads(raw_json)

        insert_data = {
            "from_number": from_number,
            "raw_message": incoming_msg,
            "type":        data.get("type", "UNKNOWN"),
            "description": data.get("description", ""),
            "status":      "pending",
        }
        if data.get("hours"):         insert_data["hours"]         = float(data["hours"])
        if data.get("cost_estimate"): insert_data["cost_estimate"] = float(data["cost_estimate"])
        if data.get("location"):      insert_data["location"]      = str(data["location"])
        if data.get("requested_by"):  insert_data["requested_by"]  = str(data["requested_by"])
        if data.get("worker_name"):   insert_data["worker_name"]   = str(data["worker_name"])
        if data.get("materials"):     insert_data["materials"]     = json.dumps(data["materials"])
        if data.get("supplier"):      insert_data["supplier"]      = str(data["supplier"])

        ai_site  = data.get("site_name")
        projects = get_projects(from_number)

        site_name = None
        if ai_site:
            site_name = match_site(ai_site, projects)
        if not site_name:
            site_name = match_site(incoming_msg, projects)

        if site_name:
            insert_data["site_name"] = site_name
            db_post("site_logs", insert_data)
            reply = data.get("confirmation_message", "✅ Logged!")
            reply += f"\n📍 Site: *{site_name}*"
        elif projects:
            pending_selections[from_number] = {
                "pending_log": insert_data,
                "projects":    projects,
            }
            reply = (data.get("confirmation_message", "✅ Got it!") + "\n\n"
                     + format_project_list(projects))
        else:
            db_post("site_logs", insert_data)
            reply = data.get("confirmation_message", "✅ Logged!")

    except json.JSONDecodeError:
        reply = "⚠️ I couldn't read that clearly. Try rephrasing."
    except Exception as e:
        reply = f"⚠️ Something went wrong. ({str(e)[:100]})"

    return _reply(reply)


# ── Pages served directly from app.py ────────────────────────────────────────
@app.route("/")
def landing():
    return Response(read_html("landing.html"), mimetype="text/html")

@app.route("/signup")
def signup():
    return Response(read_html("signup.html"), mimetype="text/html")

@app.route("/dashboard")
def dashboard_page():
    return Response(read_html("dashboard.html"), mimetype="text/html")

@app.route("/admin")
def admin_page():
    return Response(read_html("admin.html"), mimetype="text/html")

@app.route("/welcome")
def welcome():
    return Response(read_html("welcome.html"), mimetype="text/html")

@app.route("/account")
def account_page():
    return Response(read_html("account.html"), mimetype="text/html")

@app.route("/health")
def health():
    return "SubSync is running ✅", 200


def _reply(msg):
    resp = MessagingResponse()
    resp.message(msg)
    return Response(str(resp), mimetype="application/xml")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
