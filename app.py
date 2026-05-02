import os
import json
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
start_scheduler()

# ── Clients ───────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# ── DB helpers ────────────────────────────────────────────────────────────────
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

def encode_number(n):
    return n.replace("+", "%2B")

def encode_text(text):
    from urllib.parse import quote
    return quote(str(text), safe="")

# ── Project helpers ───────────────────────────────────────────────────────────
def get_projects(from_number):
    result = db_get(
        f"projects?whatsapp_number=eq.{encode_number(from_number)}"
        f"&status=eq.active&order=site_name.asc"
    )
    return result if isinstance(result, list) else []

def match_site(msg, projects):
    msg_lower = msg.lower()
    for p in projects:
        if p["site_name"].lower() in msg_lower:
            return p["site_name"]
        if p.get("client_name", "").lower() in msg_lower:
            return p["site_name"]
    return None

def create_site(from_number, site_name):
    """Auto-create a new site for this company."""
    whatsapp_raw = from_number if from_number.startswith("whatsapp:") else "whatsapp:" + from_number
    try:
        db_post("projects", {
            "whatsapp_number": whatsapp_raw,
            "site_name":       site_name,
            "client_name":     "",
            "status":          "active",
        })
    except Exception as e:
        print(f"Site create error: {e}")

def format_project_list(projects):
    lines = ["Which site is this for?\n"]
    for i, p in enumerate(projects, 1):
        lines.append(f"{i}. {p['site_name']}")
    lines.append("\nReply with a number, or type a new site name and I'll add it automatically.")
    return "\n".join(lines)

# ── Pending state ─────────────────────────────────────────────────────────────
pending_selections = {}

# ── Voice transcription ───────────────────────────────────────────────────────
def transcribe_voice(media_url):
    try:
        twilio_sid  = os.environ.get("TWILIO_ACCOUNT_SID", "")
        twilio_auth = os.environ.get("TWILIO_AUTH_TOKEN", "")
        audio_r = http_requests.get(media_url, auth=(twilio_sid, twilio_auth), timeout=30)
        if audio_r.status_code != 200:
            return None
        files   = {"file": ("audio.ogg", audio_r.content, "audio/ogg")}
        data    = {"model": "whisper-large-v3", "language": "en"}
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        groq_r  = http_requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers, files=files, data=data, timeout=30
        )
        if groq_r.status_code == 200:
            return groq_r.json().get("text", "").strip()
        return None
    except Exception as e:
        print(f"Transcription error: {e}")
        return None

# ── AI Prompt ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are an admin assistant for UK construction subcontractors.
Workers send you informal voice-note transcriptions or text messages from site.
Your job is to extract structured data and classify each message.

Classify into one of:
- VARIATION      → Extra work explicitly requested BY a client or site manager
- DAYWORK        → Worker logging time/hours they have spent on extra work
- MATERIAL_ORDER → Request to order materials, fixings, tools or equipment
- TIMESHEET      → Worker logging their standard hours for the day/week
- UNKNOWN        → Cannot classify

CRITICAL RULE for VARIATION vs DAYWORK:
- Only use VARIATION if the message EXPLICITLY states a client, site manager, or customer ASKED for or REQUESTED the work (e.g. "site manager asked", "client wants", "been asked to")
- Use DAYWORK if the worker is simply logging hours or work they did, even if it was extra work
- If the message mentions hours worked but does NOT clearly say someone requested it, set needs_clarification to true
- When in ANY doubt between VARIATION and DAYWORK, set needs_clarification to true

The message may contain ONE item or MULTIPLE items listed together.

If the message contains MULTIPLE separate items (separated by line breaks, numbers, bullet points, or commas listing different tasks), respond with a JSON array of objects.
If it is a SINGLE item, respond with a single JSON object.

Respond ONLY with valid JSON. No explanation, no markdown, just raw JSON.

Single item structure:
{
  "type": "VARIATION",
  "description": "Short clear description",
  "hours": 2.5,
  "cost_estimate": 40.00,
  "location": "Room 4",
  "site_name": "Site name if clearly mentioned, otherwise null",
  "requested_by": "Name of person who asked (if mentioned)",
  "worker_name": "Name of worker (if mentioned)",
  "materials": ["item 1"],
  "supplier": "Supplier if mentioned",
  "needs_clarification": false,
  "confirmation_message": "Friendly ✅ summary. Under 3 lines. Use £."
}

Multiple items: return a JSON array [...] of the above objects, one per item.
Each item gets its own type, description, hours, cost etc.
For multiple items, set confirmation_message only on the LAST item in the array, summarising the total count.
Always include needs_clarification on every item.
"""

# ── Command keywords ──────────────────────────────────────────────────────────
GENERATE_KEYWORDS  = ["generate variations", "generate dayworks", "generate purchase",
                      "generate report", "create invoice", "make invoice", "send invoice",
                      "variation report", "daywork report", "material report",
                      "produce report", "get variations", "send variations"]
HELP_KEYWORDS      = ["help", "guide", "how do i", "what can you do", "commands"]
DASHBOARD_KEYWORDS = ["dashboard", "my password", "password", "login", "log in", "sign in", "portal"]

def is_generate_command(msg):  return any(kw in msg.lower() for kw in GENERATE_KEYWORDS)
def is_help_command(msg):      return any(kw in msg.lower() for kw in HELP_KEYWORDS)
def is_dashboard_command(msg): return any(kw in msg.lower() for kw in DASHBOARD_KEYWORDS)

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
        raise Exception(f"Storage upload failed: {r.text}")
    return f"{SUPABASE_URL}/storage/v1/object/public/documents/{filename}"

HELP_TEXT = """👷 *SubSync Bot — Quick Guide*

*LOGGING ITEMS*
Just describe what happened — text or voice note:
• "Site manager asked me to fit extra sockets in room 4, 2 hrs, £40 materials"
• "Need to order 50 joist hangers from Screwfix"
• "Boarded loft flat 5, 3 hours, £80 materials"

*MENTIONING THE SITE*
Say the site name and I'll tag it automatically:
• "Brookfield Site — extra consumer unit in garage"
• "Flat 3 Refurb — extra spotlights kitchen"

If you forget, I'll ask. Type a new site name and I'll create it.

*GENERATING DOCUMENTS*
• "Generate variations for Brookfield Site"
• "Generate dayworks for Flat 3 Refurb"
• "Generate purchase orders"

*OTHER COMMANDS*
• Reply *Dashboard* — get your login link
• Reply *Help* — see this guide

*DOCUMENT TYPES*
• *Variation Order (VO)* — client requested extra work
• *Daywork Sheet (DS)* — time-based extra work you logged
• *Purchase Order (PO)* — materials & equipment"""

def read_html(filename):
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, filename), "r", encoding="utf-8") as f:
        return f.read()

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number  = request.form.get("From", "")
    num_media    = int(request.form.get("NumMedia", 0))
    media_url    = request.form.get("MediaUrl0", "")
    media_type   = request.form.get("MediaContentType0", "")

    # Voice note
    if num_media > 0 and media_url and "audio" in media_type:
        transcript = transcribe_voice(media_url)
        if transcript:
            incoming_msg = transcript
        else:
            return _reply("⚠️ Couldn't transcribe that voice note. Please try again or type it.")

    if not incoming_msg:
        return _reply("Send me a message or voice note about what happened on site today 👷")

    if from_number in pending_selections:
        return handle_pending(from_number, incoming_msg)

    if is_dashboard_command(incoming_msg):
        return handle_dashboard_command(from_number)

    if is_help_command(incoming_msg):
        return _reply(HELP_TEXT)

    if is_generate_command(incoming_msg):
        return handle_generate(from_number, incoming_msg)

    return handle_log(from_number, incoming_msg)


# ── Pending handler (type clarification + site selection) ─────────────────────
def handle_pending(from_number, msg):
    state    = pending_selections[from_number]
    projects = state["projects"]
    log_data = state["pending_log"]

    # Step 1: type clarification
    if state.get("awaiting_type"):
        if msg.strip() == "1":
            log_data["type"] = "VARIATION"
        elif msg.strip() == "2":
            log_data["type"] = "DAYWORK"
        else:
            return _reply("Please reply *1* for Variation or *2* for Daywork")

        # Move to site selection if needed
        site_name = match_site(log_data.get("raw_message", ""), projects)
        if site_name:
            log_data["site_name"] = site_name
            del pending_selections[from_number]
            db_post("site_logs", log_data)
            return _reply(f"✅ Logged as *{log_data['type']}* for *{site_name}*!")
        elif projects:
            pending_selections[from_number] = {
                "pending_log":    log_data,
                "projects":       projects,
                "awaiting_type":  False,
                "awaiting_site":  True,
            }
            return _reply(format_project_list(projects))
        else:
            del pending_selections[from_number]
            db_post("site_logs", log_data)
            return _reply(f"✅ Logged as *{log_data['type']}*!")

    # Step 2: site selection
    if state.get("awaiting_site"):
        site_name = None

        # Try number selection
        if msg.strip().isdigit():
            idx = int(msg.strip()) - 1
            if 0 <= idx < len(projects):
                site_name = projects[idx]["site_name"]

        # Try name match
        if not site_name:
            msg_lower = msg.lower()
            for p in projects:
                if p["site_name"].lower() in msg_lower or msg_lower in p["site_name"].lower():
                    site_name = p["site_name"]
                    break

        # Auto-create if no match — no blockers
        if not site_name:
            site_name = msg.strip().title()
            create_site(from_number, site_name)

        log_data["site_name"] = site_name
        del pending_selections[from_number]

        try:
            db_post("site_logs", log_data)
            return _reply(
                f"✅ Logged for *{site_name}*!\n"
                f"{log_data.get('description', 'Item')} saved."
            )
        except Exception as e:
            return _reply(f"⚠️ Couldn't save. ({str(e)[:80]})")

    # Fallback
    del pending_selections[from_number]
    return _reply("Something went wrong. Please try sending that again.")


# ── Dashboard command ─────────────────────────────────────────────────────────
def handle_dashboard_command(from_number):
    app_url  = os.environ.get("APP_URL", "https://www.subsync.co.uk")
    password = os.environ.get("DASHBOARD_PASSWORD", "changeme")
    number   = from_number.replace("whatsapp:", "")
    return _reply(
        "📊 *Your SubSync Dashboard*\n\n"
        f"Link: {app_url}/dashboard\n"
        f"Number: {number}\n"
        f"Password: {password}\n\n"
        "Bookmark it so you can check in anytime 👍"
    )


# ── Generate handler ──────────────────────────────────────────────────────────
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

        query = (
            f"site_logs?from_number=eq.{encode_number(from_number)}"
            f"&status=eq.pending&order=created_at.asc"
        )
        if site_name:
            query += f"&site_name=eq.{encode_text(site_name)}"

        logs = db_get(query)
        if not isinstance(logs, list) or not logs:
            return _reply(
                f"📋 No pending items{' for *' + site_name + '*' if site_name else ''}.\n"
                f"Log some site activity first then ask me to generate."
            )

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
        return _reply(f"⚠️ Couldn't generate document. ({str(e)[:150]})")


# ── Log handler ───────────────────────────────────────────────────────────────
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

        projects            = get_projects(from_number)
        needs_clarification = data.get("needs_clarification", False)
        confirmation        = data.get("confirmation_message", "✅ Got it!")

        # Match site from message
        site_name = match_site(data.get("site_name") or "", projects)
        if not site_name:
            site_name = match_site(incoming_msg, projects)

        # Step 1: Ask type if ambiguous
        if needs_clarification:
            pending_selections[from_number] = {
                "pending_log":   insert_data,
                "projects":      projects,
                "awaiting_type": True,
                "awaiting_site": False,
            }
            return _reply(
                confirmation + "\n\n"
                "Is this a:\n"
                "1. *Variation* — extra work the client/manager requested\n"
                "2. *Daywork* — hours you logged on extra work\n\n"
                "Reply 1 or 2"
            )

        # Step 2: Save with site or ask
        if site_name:
            insert_data["site_name"] = site_name
            db_post("site_logs", insert_data)
            return _reply(confirmation + f"\n📍 Site: *{site_name}*")
        elif projects:
            pending_selections[from_number] = {
                "pending_log":   insert_data,
                "projects":      projects,
                "awaiting_type": False,
                "awaiting_site": True,
            }
            return _reply(confirmation + "\n\n" + format_project_list(projects))
        else:
            db_post("site_logs", insert_data)
            return _reply(confirmation)

    except json.JSONDecodeError:
        return _reply("⚠️ I couldn't read that clearly. Try rephrasing.")
    except Exception as e:
        return _reply(f"⚠️ Something went wrong. ({str(e)[:100]})")


# ── Pages ─────────────────────────────────────────────────────────────────────
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
