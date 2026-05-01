import os
import json
import uuid
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from supabase import create_client
from datetime import datetime
import requests as http_requests
from pdf_generator import generate_pdf

app = Flask(__name__)

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
  "requested_by": "Name of person who asked (if mentioned)",
  "worker_name": "Name of worker logging this (if mentioned)",
  "materials": ["item 1", "item 2"],
  "supplier": "Supplier name if mentioned",
  "confirmation_message": "A friendly WhatsApp reply summarising what was captured. Start with ✅. Under 3 lines. Use £ for currency."
}

Only include fields relevant to the type. Always include confirmation_message.
"""

# ── Command detection ─────────────────────────────────────────────────────────
GENERATE_KEYWORDS = [
    "generate", "create invoice", "make invoice", "send invoice",
    "variation report", "daywork report", "material report",
    "weekly report", "end of day report", "end of week",
    "produce report", "get variations", "send variations"
]

def is_generate_command(msg):
    return any(kw in msg.lower() for kw in GENERATE_KEYWORDS)

def detect_doc_type(msg):
    msg_lower = msg.lower()
    if "daywork" in msg_lower:
        return "DAYWORK", "Daywork Sheet"
    if "material" in msg_lower or "order" in msg_lower or "purchase" in msg_lower:
        return "MATERIAL_ORDER", "Purchase Order"
    return "VARIATION", "Variation Order"

# ── Supabase Storage ──────────────────────────────────────────────────────────
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

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number  = request.form.get("From", "")

    if not incoming_msg:
        return _reply("Send me a message about what happened on site today 👷")

    if is_generate_command(incoming_msg):
        return handle_generate(from_number, incoming_msg)

    return handle_log(from_number, incoming_msg)


def handle_generate(from_number, msg):
    try:
        encoded_number = from_number.replace("+", "%2B")
        companies = db_get(f"companies?whatsapp_number=eq.{encoded_number}&limit=1")
        if not companies:
            return _reply(f"⚠️ Not registered. Your number: '{from_number}'")
        company = companies[0]

        log_type, doc_title = detect_doc_type(msg)

        encoded_number = from_number.replace("+", "%2B")
        logs = db_get(
            f"site_logs?from_number=eq.{encoded_number}"
            f"&status=eq.pending&order=created_at.asc"
        )

        if not logs:
            return _reply(
                f"📋 No pending {doc_title.lower()}s found.\n"
                f"Log some site activity first, then ask me to generate again."
            )

        pdf_bytes = generate_pdf(company, logs, doc_title)

        filename = (
            f"{log_type.lower()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_{uuid.uuid4().hex[:6]}.pdf"
        )
        pdf_url = upload_pdf(pdf_bytes, filename)

        for log in logs:
            db_patch(f"site_logs?id=eq.{log['id']}", {"status": "sent"})

        resp = MessagingResponse()
        m = resp.message(
            f"📄 *{doc_title}* ready!\n"
            f"{len(logs)} item(s) · {company['company_name']}\n"
            f"Review and forward to your client ✅"
        )
        m.media(pdf_url)
        return Response(str(resp), mimetype="application/xml")

    except Exception as e:
        return _reply(f"⚠️ Couldn't generate document. Error: {str(e)[:120]}")


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

        db_post("site_logs", insert_data)

        reply = data.get("confirmation_message", "✅ Logged! I'll include this in your next document pack.")

    except json.JSONDecodeError:
        reply = "⚠️ I couldn't read that clearly. Try rephrasing — e.g. 'Variation: fitted 3 extra sockets room 4, 2 hours, £40 materials'"
    except Exception as e:
        reply = f"⚠️ Something went wrong. Please try again. ({str(e)[:100]})"

    return _reply(reply)


# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return "SubSync Bot is running ✅", 200


def _reply(msg):
    resp = MessagingResponse()
    resp.message(msg)
    return Response(str(resp), mimetype="application/xml")


if __name__ == "__main__":
    app.run(debug=True, port=5000)

