import os
import json
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from supabase import create_client
from datetime import datetime

app = Flask(__name__)

# ── Clients ──────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
supabase = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_KEY")
)

# ── AI Prompt ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are an admin assistant for UK construction subcontractors.
Workers send you informal voice-note transcriptions or text messages from site.
Your job is to extract structured data and classify each message.

Classify into one of:
- VARIATION   → Extra work requested by client or site manager, not in original contract
- DAYWORK     → Time-based extra work; worker logging hours spent on an extra task
- MATERIAL_ORDER → Request to order materials, fixings, tools or equipment
- TIMESHEET   → Worker logging their standard hours for the day/week
- UNKNOWN     → Cannot classify

Respond ONLY with a valid JSON object. No explanation, no markdown, just raw JSON.

JSON structure:
{
  "type": "VARIATION",
  "description": "Short clear description of the task or item",
  "hours": 2.5,
  "cost_estimate": 40.00,
  "location": "Room 4 / Level 2 / etc",
  "requested_by": "Name of person who asked (if mentioned)",
  "worker_name": "Name of worker logging this (if mentioned)",
  "materials": ["item 1", "item 2"],
  "supplier": "Supplier name if mentioned",
  "quantity": null,
  "confirmation_message": "A friendly plain-English WhatsApp reply summarising what was captured. Use ✅ emoji at the start. Keep it under 3 lines. Use £ for currency."
}

Only include fields relevant to the type — omit irrelevant ones.
Always include confirmation_message.
If cost or hours are not mentioned, omit those fields.
"""

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number  = request.form.get("From", "")

    if not incoming_msg:
        return _reply("Send me a message describing what happened on site today 👷")

    try:
        # Ask Claude to classify and extract
        ai_response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": incoming_msg}]
        )

        raw_json = ai_response.content[0].text.strip()
        # Strip markdown code blocks if model wraps response in them
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
        raw_json = raw_json.strip()
        data = json.loads(raw_json)

        # Save to Supabase — minimal fields to avoid type issues
        insert_data = {
            "from_number": from_number,
            "raw_message": incoming_msg,
            "type":        data.get("type", "UNKNOWN"),
            "description": data.get("description", ""),
            "status":      "pending",
        }
        if data.get("hours"):        insert_data["hours"]         = float(data["hours"])
        if data.get("cost_estimate"): insert_data["cost_estimate"] = float(data["cost_estimate"])
        if data.get("location"):     insert_data["location"]      = str(data["location"])
        if data.get("requested_by"): insert_data["requested_by"]  = str(data["requested_by"])
        if data.get("worker_name"):  insert_data["worker_name"]   = str(data["worker_name"])
        if data.get("materials"):    insert_data["materials"]     = json.dumps(data["materials"])
        if data.get("supplier"):     insert_data["supplier"]      = str(data["supplier"])

        result = supabase.table("site_logs").insert(insert_data).execute()

        reply = data.get("confirmation_message", "✅ Logged! I'll include this in your next document pack.")

    except json.JSONDecodeError:
        reply = "⚠️ I couldn't read that clearly. Try rephrasing — e.g. 'Variation: fitted 3 extra sockets room 4, 2 hours, £40 materials'"
    except Exception as e:
        reply = f"⚠️ Something went wrong. Please try again. ({str(e)[:80]})"

    return _reply(reply)


# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return "SubSync Bot is running ✅", 200


# ── Helper ────────────────────────────────────────────────────────────────────
def _reply(msg: str) -> Response:
    resp = MessagingResponse()
    resp.message(msg)
    return Response(str(resp), mimetype="application/xml")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
