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
from auth import auth_bp, create_magic_token

app = Flask(__name__)
app.register_blueprint(dashboard_bp)
app.register_blueprint(onboarding_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(account_bp)
app.register_blueprint(auth_bp)
start_scheduler()

# ── Clients ───────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# ── DB helpers ────────────────────────────────────────────────────────────────
def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}

def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json()

def db_post(path, payload):
    r = http_requests.post(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                           headers={**sb_headers(), "Prefer": "return=minimal"})
    if r.status_code not in (200, 201):
        raise Exception(f"DB Error {r.status_code}: {r.text}")

def db_patch(path, payload):
    http_requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                        headers={**sb_headers(), "Prefer": "return=minimal"})

def encode_number(n): return n.replace("+", "%2B")

def encode_text(text):
    from urllib.parse import quote
    return quote(str(text), safe="")

# ── Project helpers ───────────────────────────────────────────────────────────
def get_projects(from_number):
    result = db_get(f"projects?whatsapp_number=eq.{encode_number(from_number)}"
                    f"&status=eq.active&order=site_name.asc")
    return result if isinstance(result, list) else []

def match_site(msg, projects):
    if not msg: return None
    msg_lower = msg.lower()
    for p in projects:
        if p["site_name"].lower() in msg_lower: return p["site_name"]
        if p.get("client_name","").lower() in msg_lower: return p["site_name"]
    return None

def create_site(from_number, site_name):
    whatsapp_raw = from_number if from_number.startswith("whatsapp:") else "whatsapp:" + from_number
    try:
        db_post("projects", {"whatsapp_number": whatsapp_raw, "site_name": site_name,
                             "client_name": "", "status": "active"})
    except Exception as e:
        print(f"Site create error: {e}")

def format_project_list(projects):
    lines = ["Which site is this for?\n"]
    for i, p in enumerate(projects, 1):
        lines.append(f"{i}. {p['site_name']}")
    lines.append("\nReply with a number, or type a new site name and I'll add it automatically.")
    return "\n".join(lines)

pending_selections = {}

# ── Voice transcription ───────────────────────────────────────────────────────
def transcribe_voice(media_url):
    try:
        twilio_sid  = os.environ.get("TWILIO_ACCOUNT_SID", "")
        twilio_auth = os.environ.get("TWILIO_AUTH_TOKEN", "")
        audio_r = http_requests.get(media_url, auth=(twilio_sid, twilio_auth), timeout=30)
        if audio_r.status_code != 200: return None
        files   = {"file": ("audio.ogg", audio_r.content, "audio/ogg")}
        data    = {"model": "whisper-large-v3", "language": "en"}
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        groq_r  = http_requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                                     headers=headers, files=files, data=data, timeout=30)
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
- DAYWORK        → Worker logging time/hours they spent on extra work
- MATERIAL_ORDER → Request to order materials, fixings, tools or equipment
- TIMESHEET      → Worker logging their standard hours for the day/week
- UNKNOWN        → Cannot classify

CRITICAL RULE for VARIATION vs DAYWORK:
- Only VARIATION if message EXPLICITLY says a client/site manager ASKED or REQUESTED the work
- DAYWORK if the worker is simply logging hours/work they did
- If hours mentioned but no clear request from client/manager, set needs_clarification to true
- When in ANY doubt between VARIATION and DAYWORK, set needs_clarification to true

MULTIPLE ITEMS: If the message contains multiple separate items (numbered list, bullet points, or multiple tasks), respond with a JSON ARRAY of objects, one per item.
SINGLE ITEM: respond with a single JSON object.

Respond ONLY with valid JSON. No explanation, no markdown, just raw JSON.

Single item structure:
{
  "type": "VARIATION",
  "description": "Short clear description",
  "hours": 2.5,
  "cost_estimate": 40.00,
  "location": "Room 4",
  "site_name": "Site name if mentioned, otherwise null",
  "requested_by": "Name if mentioned",
  "worker_name": "Worker name if mentioned",
  "materials": ["item"],
  "supplier": "Supplier if mentioned",
  "needs_clarification": false,
  "confirmation_message": "Friendly ✅ reply. Under 3 lines. Use £."
}

Multiple items: return JSON array [...] of above objects.
For arrays, only the LAST item needs confirmation_message summarising the total logged.
Always include needs_clarification on every item.
"""

# ── Keywords ──────────────────────────────────────────────────────────────────
GENERATE_KEYWORDS  = ["generate variations", "generate dayworks", "generate purchase",
                      "generate report", "create invoice", "make invoice", "send invoice",
                      "variation report", "daywork report", "material report",
                      "produce report", "get variations", "send variations"]
HELP_KEYWORDS      = ["help", "guide", "how do i", "what can you do", "commands"]
DASHBOARD_KEYWORDS = ["my dashboard", "get dashboard", "dashboard link", "my password", "get login", "login link"]
SUMMARY_KEYWORDS   = ["summary", "overview", "my summary", "show summary", "stats", "how am i doing", "my stats"]
PENDING_KEYWORDS   = ["pending", "what's pending", "show pending", "outstanding", "not approved"]
APPROVE_KEYWORDS   = ["approve", "approved", "mark approved"]
CHASE_KEYWORDS     = ["chasing", "chase", "mark chasing", "follow up"]
CANCEL_KEYWORDS    = ["cancel", "cancelled", "mark cancelled", "mark canceled"]

def is_generate_command(msg):  return any(kw in msg.lower() for kw in GENERATE_KEYWORDS)
def is_help_command(msg):      return any(kw in msg.lower() for kw in HELP_KEYWORDS)
def is_dashboard_command(msg): return any(kw in msg.lower() for kw in DASHBOARD_KEYWORDS)
def is_summary_command(msg):   return any(kw in msg.lower() for kw in SUMMARY_KEYWORDS)
def is_pending_command(msg):   return any(kw in msg.lower() for kw in PENDING_KEYWORDS)
def is_status_command(msg):    return any(kw in msg.lower() for kw in APPROVE_KEYWORDS + CHASE_KEYWORDS + CANCEL_KEYWORDS)

def detect_doc_type(msg):
    msg_lower = msg.lower()
    if "daywork" in msg_lower: return "DAYWORK", "Daywork Sheet", "DS"
    if "material" in msg_lower or "order" in msg_lower or "purchase" in msg_lower:
        return "MATERIAL_ORDER", "Purchase Order", "PO"
    return "VARIATION", "Variation Order", "VO"

def slugify(text):
    text = text.strip().replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_\-]", "", text)[:25]

def make_doc_ref_and_filename(company, logs, prefix, site_name):
    sent = db_get(f"site_logs?from_number=eq.{encode_number(company.get('whatsapp_number',''))}"
                  f"&status=eq.sent&select=id")
    doc_number   = str(len(sent) + 1).zfill(3)
    site_slug    = slugify(site_name) if site_name else "AllSites"
    company_slug = slugify(company.get("company_name", "Company").split()[0])
    date_str     = datetime.now().strftime("%d%b%Y")
    ref_str      = f"{prefix}-{doc_number}"
    return ref_str, f"{ref_str}_{company_slug}_{site_slug}_{date_str}.pdf"

def upload_pdf(pdf_bytes, filename):
    url = f"{SUPABASE_URL}/storage/v1/object/documents/{filename}"
    r = http_requests.post(url, data=pdf_bytes, headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/pdf"})
    if r.status_code not in (200, 201):
        raise Exception(f"Storage upload failed: {r.text}")
    return f"{SUPABASE_URL}/storage/v1/object/public/documents/{filename}"

HELP_TEXT = """👷 *Note2Quote Bot — Quick Guide*

*LOGGING ITEMS*
Text or voice note — single or a list:
• "Site manager asked me to fit extra sockets in room 4, 2 hrs, £40"
• "Boarded loft flat 5, 3 hours, £80 materials"
• Send a numbered list to log multiple items at once

*MENTIONING THE SITE*
• "Brookfield Site — extra consumer unit in garage"
• If you forget, I'll ask. Type a new name and I'll create it.

*GENERATING DOCUMENTS*
• "Generate variations for Brookfield Site"
• "Generate dayworks for Flat 3 Refurb"

*OTHER COMMANDS*
• *Dashboard* — get your login link
• *Help* — see this guide"""

def read_html(filename):
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, filename), "r", encoding="utf-8") as f:
        return f.read()

# ── Build insert dict from AI item ────────────────────────────────────────────
def build_insert(from_number, raw_message, item):
    d = {
        "from_number": from_number,
        "raw_message": raw_message,
        "type":        item.get("type", "UNKNOWN"),
        "description": item.get("description", ""),
        "status":      "pending",
    }
    if item.get("hours"):         d["hours"]         = float(item["hours"])
    if item.get("cost_estimate"): d["cost_estimate"] = float(item["cost_estimate"])
    if item.get("location"):      d["location"]      = str(item["location"])
    if item.get("requested_by"):  d["requested_by"]  = str(item["requested_by"])
    if item.get("worker_name"):   d["worker_name"]   = str(item["worker_name"])
    if item.get("materials"):     d["materials"]     = json.dumps(item["materials"])
    if item.get("supplier"):      d["supplier"]      = str(item["supplier"])
    return d

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number  = request.form.get("From", "")
    num_media    = int(request.form.get("NumMedia", 0))
    media_url    = request.form.get("MediaUrl0", "")
    media_type   = request.form.get("MediaContentType0", "")

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

    if is_dashboard_command(incoming_msg): return handle_dashboard_command(from_number)
    if is_summary_command(incoming_msg):   return handle_summary(from_number)
    if is_pending_command(incoming_msg):   return handle_pending_summary(from_number)
    if is_status_command(incoming_msg):    return handle_status_update(from_number, incoming_msg)
    if is_help_command(incoming_msg):      return _reply(HELP_TEXT)
    if is_generate_command(incoming_msg):  return handle_generate(from_number, incoming_msg)

    return handle_log(from_number, incoming_msg)


# ── Pending handler ───────────────────────────────────────────────────────────
def handle_pending(from_number, msg):
    state    = pending_selections[from_number]
    projects = state["projects"]
    log_data = state["pending_log"]

    # Type clarification step
    if state.get("awaiting_type"):
        if msg.strip() == "1":
            log_data["type"] = "VARIATION"
        elif msg.strip() == "2":
            log_data["type"] = "DAYWORK"
        else:
            return _reply("Please reply *1* for Variation or *2* for Daywork")

        site_name = match_site(log_data.get("raw_message", ""), projects)
        if site_name:
            log_data["site_name"] = site_name
            del pending_selections[from_number]
            db_post("site_logs", log_data)
            return _reply(f"✅ Logged as *{log_data['type']}* for *{site_name}*!")
        elif projects:
            pending_selections[from_number] = {
                "pending_log": log_data, "projects": projects,
                "awaiting_type": False, "awaiting_site": True,
            }
            return _reply(format_project_list(projects))
        else:
            del pending_selections[from_number]
            db_post("site_logs", log_data)
            return _reply(f"✅ Logged as *{log_data['type']}*!")

    # Site selection step
    if state.get("awaiting_site"):
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

        # Auto-create new site if no match
        if not site_name:
            site_name = msg.strip().title()
            create_site(from_number, site_name)

        del pending_selections[from_number]

        # Status update flow (not a normal log)
        if log_data.get("_status_update"):
            from urllib.parse import quote as _q
            encoded = encode_number(from_number)
            new_status = log_data.get("new_status", "pending")
            past_tense = log_data.get("past_tense", "updated")
            emoji      = log_data.get("emoji", "✅")
            logs = db_get("site_logs?from_number=eq." + encoded + "&status=eq.pending&site_name=eq." + _q(site_name))
            if isinstance(logs, list) and logs:
                for log in logs:
                    db_patch("site_logs?id=eq." + str(log["id"]), {"status": new_status})
                total_val = sum(float(l.get("cost_estimate") or 0) for l in logs)
                total_val = sum(float(l.get("cost_estimate") or 0) for l in logs)
                out = emoji + " *" + str(len(logs)) + " item(s) " + past_tense + "* for *" + site_name + "*"
                out += "\nTotal value: £" + ("%.2f" % total_val)
                return _reply(out)
                return _reply(out)
                return _reply("No pending items found for *" + site_name + "*.")

        # Multi-item bulk save
        if log_data.get("_multi"):
            saved = 0
            total_cost = 0.0
            for item in log_data.get("_items", []):
                item["site_name"] = site_name
                try:
                    db_post("site_logs", item)
                    saved += 1
                    total_cost += float(item.get("cost_estimate") or 0)
                except Exception:
                    pass
            cost_tag = f" · Est. £{total_cost:.0f}" if total_cost else ""
            return _reply(f"✅ Logged {saved} item(s) for *{site_name}*{cost_tag}!")

        # Single item save
        log_data["site_name"] = site_name
        try:
            db_post("site_logs", log_data)
            return _reply(f"✅ Logged for *{site_name}*!\n{log_data.get('description','Item')} saved.")
        except Exception as e:
            return _reply(f"⚠️ Couldn't save. ({str(e)[:80]})")

    del pending_selections[from_number]
    return _reply("Something went wrong. Please try sending that again.")


# ── Dashboard command ─────────────────────────────────────────────────────────
def handle_dashboard_command(from_number):
    app_url  = os.environ.get("APP_URL", "https://www.note2quote.co.uk")
    password = os.environ.get("DASHBOARD_PASSWORD", "changeme")
    number   = from_number.replace("whatsapp:", "")
    return _reply(
        "📊 *Your Note2Quote Dashboard*\n\n"
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

        query = (f"site_logs?from_number=eq.{encode_number(from_number)}"
                 f"&status=eq.pending&order=created_at.asc")
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
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": incoming_msg}]
        )

        raw_json = ai_response.content[0].text.strip()
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
        raw_json = raw_json.strip()

        parsed   = json.loads(raw_json)
        projects = get_projects(from_number)

        # ── Multi-item list ───────────────────────────────────────────────────
        if isinstance(parsed, list) and len(parsed) > 0:
            site_name  = match_site(incoming_msg, projects)
            items      = [build_insert(from_number, incoming_msg, i) for i in parsed]
            total_cost = sum(float(i.get("cost_estimate") or 0) for i in parsed)

            # If we know the site, save everything now
            if site_name:
                saved = 0
                for item in items:
                    item["site_name"] = site_name
                    try:
                        db_post("site_logs", item)
                        saved += 1
                    except Exception:
                        pass
                cost_tag = f" · Est. £{total_cost:.0f}" if total_cost else ""
                return _reply(f"✅ Logged {saved} item(s) for *{site_name}*{cost_tag}!\n"
                              f"I'll include these in your next document pack.")

            # Ask for site once for all items
            elif projects:
                pending_selections[from_number] = {
                    "pending_log":   {"_multi": True, "_items": items},
                    "projects":      projects,
                    "awaiting_type": False,
                    "awaiting_site": True,
                }
                return _reply(
                    f"✅ Got {len(parsed)} items — which site are these all for?\n\n"
                    + format_project_list(projects)
                )
            else:
                saved = 0
                for item in items:
                    try:
                        db_post("site_logs", item)
                        saved += 1
                    except Exception:
                        pass
                return _reply(f"✅ Logged {saved} item(s)! I'll include these in your next pack.")

        # ── Single item ───────────────────────────────────────────────────────
        if isinstance(parsed, dict):
            data                = parsed
            insert_data         = build_insert(from_number, incoming_msg, data)
            needs_clarification = data.get("needs_clarification", False)
            confirmation        = data.get("confirmation_message", "✅ Got it!")

            site_name = match_site(data.get("site_name") or "", projects)
            if not site_name:
                site_name = match_site(incoming_msg, projects)

            if needs_clarification:
                pending_selections[from_number] = {
                    "pending_log": insert_data, "projects": projects,
                    "awaiting_type": True, "awaiting_site": False,
                }
                return _reply(
                    confirmation + "\n\n"
                    "Is this a:\n"
                    "1. *Variation* — extra work the client/manager requested\n"
                    "2. *Daywork* — hours you logged on extra work\n\n"
                    "Reply 1 or 2"
                )

            if site_name:
                insert_data["site_name"] = site_name
                db_post("site_logs", insert_data)
                return _reply(confirmation + f"\n📍 Site: *{site_name}*")
            elif projects:
                pending_selections[from_number] = {
                    "pending_log": insert_data, "projects": projects,
                    "awaiting_type": False, "awaiting_site": True,
                }
                return _reply(confirmation + "\n\n" + format_project_list(projects))
            else:
                db_post("site_logs", insert_data)
                return _reply(confirmation)

        return _reply("⚠️ I couldn't understand that. Try rephrasing.")

    except json.JSONDecodeError:
        return _reply("⚠️ I couldn't read that clearly. Try rephrasing.")
    except Exception as e:
        return _reply(f"⚠️ Something went wrong. ({str(e)[:100]})")



# ── Landing HTML (embedded) ─────────────────────────────────────────────────
LANDING_HTML = '''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Note2Quote — From Voice Note to Polished PDF. Instantly.</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --amber:#f59e0b;--amber-dark:#d97706;--amber-light:#fbbf24;
  --amber-dim:rgba(245,158,11,0.12);--amber-glow:rgba(245,158,11,0.25);
}
[data-theme="light"]{
  --bg:#f5f3ef;--bg2:#ffffff;--bg3:#eceae5;--bg4:#e2dfd8;
  --text:#0c0d10;--text2:#2d2f36;--muted:#6b7280;--faint:#a0a5b0;
  --border:#dddad4;--border2:#c8c4bc;--card:#ffffff;
  --nav:rgba(245,243,239,0.95);
  --shadow:rgba(0,0,0,0.06);--shadow2:rgba(0,0,0,0.12);
}
[data-theme="dark"]{
  --bg:#0c0d10;--bg2:#131419;--bg3:#1a1b22;--bg4:#21232c;
  --text:#f0ede8;--text2:#c8c4bc;--muted:#8b8fa8;--faint:#4a4d5e;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);--card:#131419;
  --nav:rgba(12,13,16,0.95);
  --shadow:rgba(0,0,0,0.4);--shadow2:rgba(0,0,0,0.6);
}
*{box-sizing:border-box;margin:0;padding:0;}
html{scroll-behavior:smooth;}
body{font-family:'Manrope',sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;overflow-x:hidden;transition:background .3s,color .3s;}

/* NAV */
nav{position:fixed;top:0;width:100%;z-index:100;padding:0 48px;height:68px;display:flex;align-items:center;justify-content:space-between;background:var(--nav);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);transition:background .3s,border-color .3s;}
.nav-logo{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:3px;color:var(--text);display:flex;align-items:center;gap:10px;text-decoration:none;}
.nav-links{display:flex;gap:28px;list-style:none;}
.nav-links a{color:var(--muted);font-size:13px;font-weight:500;text-decoration:none;transition:color .2s;}
.nav-links a:hover{color:var(--amber);}
.nav-right{display:flex;align-items:center;gap:10px;}
.theme-toggle{width:36px;height:36px;border-radius:8px;border:1px solid var(--border);background:var(--card);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:15px;transition:all .2s;}
.theme-toggle:hover{border-color:var(--amber);}
.nav-cta{background:var(--amber);color:#0c0d10;padding:9px 22px;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;transition:all .2s;}
.nav-cta:hover{background:var(--amber-light);transform:translateY(-1px);}
@media(max-width:768px){nav{padding:0 20px;}.nav-links{display:none;}}

/* HERO */
.hero{min-height:100vh;display:flex;align-items:center;padding:100px 48px 80px;background:var(--bg);position:relative;overflow:hidden;}
.hero-orb{position:absolute;top:50%;right:-100px;transform:translateY(-50%);width:600px;height:600px;background:radial-gradient(circle,var(--amber-glow) 0%,transparent 65%);pointer-events:none;animation:orb-pulse 6s ease-in-out infinite;}
@keyframes orb-pulse{0%,100%{transform:translateY(-50%) scale(1);opacity:.6;}50%{transform:translateY(-52%) scale(1.08);opacity:1;}}
.hero-inner{max-width:1200px;margin:0 auto;width:100%;display:grid;grid-template-columns:1fr 480px;gap:80px;align-items:center;position:relative;z-index:2;}
@media(max-width:1060px){.hero-inner{grid-template-columns:1fr;}.hero-right{display:none;}}
@media(max-width:768px){.hero{padding:100px 20px 60px;}}

.hero-badge{display:inline-flex;align-items:center;gap:8px;background:var(--amber-dim);border:1px solid rgba(245,158,11,0.3);padding:6px 16px;border-radius:100px;font-size:11px;font-weight:700;color:var(--amber-dark);text-transform:uppercase;letter-spacing:2px;margin-bottom:24px;}
.badge-dot{width:6px;height:6px;background:var(--amber);border-radius:50%;display:inline-block;animation:blink 2s ease infinite;}
@keyframes blink{0%,100%{opacity:1;}50%{opacity:.2;}}

h1{font-family:'Bebas Neue',sans-serif;font-size:clamp(64px,9vw,120px);line-height:.92;letter-spacing:4px;color:var(--text);margin-bottom:24px;transition:color .3s;}
h1 .accent{color:var(--amber);}
.hero-sub{font-size:16px;line-height:1.75;color:var(--muted);max-width:460px;margin-bottom:40px;font-weight:400;}
.hero-actions{display:flex;gap:14px;flex-wrap:wrap;}
.btn-3d{padding:14px 26px;border-radius:10px;font-size:14px;font-weight:700;text-decoration:none;display:inline-flex;align-items:center;gap:8px;font-family:'Manrope',sans-serif;letter-spacing:.3px;transition:all .15s;}
.btn-3d-primary{background:var(--amber);color:#0c0d10;box-shadow:0 4px 0 var(--amber-dark),0 8px 20px var(--amber-glow);}
.btn-3d-primary:hover{transform:translateY(-2px);box-shadow:0 6px 0 var(--amber-dark),0 14px 28px var(--amber-glow);}
.btn-3d-primary:active{transform:translateY(1px);box-shadow:0 2px 0 var(--amber-dark);}
.btn-3d-secondary{background:var(--card);color:var(--text);border:1px solid var(--border);box-shadow:0 4px 0 var(--border2),0 6px 16px var(--shadow);}
.btn-3d-secondary:hover{transform:translateY(-2px);box-shadow:0 6px 0 var(--border2),0 10px 20px var(--shadow2);}
.hero-note{font-size:11px;color:var(--faint);margin-top:16px;letter-spacing:.5px;}

/* HERO PHONE */
.hero-right{position:relative;}
.phone-scene{animation:float 5s ease-in-out infinite;}
@keyframes float{0%,100%{transform:translateY(0);}50%{transform:translateY(-14px);}}
.phone-3d{background:#0c0d10;border-radius:36px;padding:12px;box-shadow:0 0 0 1px rgba(255,255,255,0.06),0 40px 80px rgba(0,0,0,.45);transform:rotateX(4deg) rotateY(-12deg);transition:transform .5s cubic-bezier(.34,1.56,.64,1);max-width:290px;margin:0 auto;}
.phone-notch{width:70px;height:20px;background:#0c0d10;border-radius:10px;margin:0 auto 10px;}
.phone-screen{background:#1a1b22;border-radius:26px;overflow:hidden;}
.wa-bar{background:#0d0f14;padding:12px 14px;display:flex;align-items:center;gap:10px;}
.wa-av{width:30px;height:30px;background:var(--amber);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0;}
.wa-n{font-family:'Bebas Neue',sans-serif;font-size:13px;letter-spacing:1px;color:#fff;}
.wa-s{font-size:10px;color:rgba(255,255,255,.4);}
.chat-area{padding:12px;display:flex;flex-direction:column;gap:8px;background:#1e2028;}
.msg{display:flex;flex-direction:column;}
.msg.out{align-items:flex-end;}
.bub{padding:8px 11px;border-radius:13px;font-size:11px;line-height:1.5;max-width:90%;}
.bub.out{background:#1a3a00;color:#a3e635;border-bottom-right-radius:3px;border:1px solid rgba(163,230,53,.1);}
.bub.in{background:rgba(245,158,11,.08);color:rgba(255,255,255,.85);border:1px solid rgba(245,158,11,.15);border-bottom-left-radius:3px;}
.bub-time{font-size:9px;color:rgba(255,255,255,.2);margin-top:2px;}
.float-card{position:absolute;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 16px;box-shadow:0 16px 36px var(--shadow2);transition:background .3s,border-color .3s;}
.fc1{right:-30px;top:20px;animation:fc1 4s ease-in-out infinite;}
.fc2{left:-40px;bottom:50px;animation:fc2 5s ease-in-out infinite;}
@keyframes fc1{0%,100%{transform:rotate(2deg);}50%{transform:translateY(-8px) rotate(2deg);}}
@keyframes fc2{0%,100%{transform:rotate(-1deg);}50%{transform:translateY(-6px) rotate(-1deg);}}
.fc-label{font-size:9px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:3px;}
.fc-val{font-family:'Bebas Neue',sans-serif;font-size:24px;letter-spacing:1.5px;color:var(--amber);}
.fc-sub{font-size:10px;color:var(--muted);margin-top:1px;}

/* MARQUEE */
.marquee-wrap{border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:14px 0;overflow:hidden;background:var(--bg2);transition:background .3s,border-color .3s;}
.marquee-track{display:flex;animation:marquee 38s linear infinite;width:max-content;}
.marquee-item{display:flex;align-items:center;gap:10px;padding:0 28px;font-size:10px;font-weight:700;color:var(--faint);white-space:nowrap;border-right:1px solid var(--border);letter-spacing:2.5px;text-transform:uppercase;}
.marquee-dot{width:4px;height:4px;background:var(--amber);border-radius:50%;flex-shrink:0;}
@keyframes marquee{from{transform:translateX(0);}to{transform:translateX(-50%);}}

/* STATS */
.stats-section{padding:80px 48px;background:#0c0d10;}
.stats-grid{max-width:1200px;margin:0 auto;display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.04);border-radius:20px;overflow:hidden;}
@media(max-width:700px){.stats-grid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:480px){.stats-section{padding:60px 20px;}}
.stat-item{background:#131419;padding:36px 24px;text-align:center;transition:background .2s;}
.stat-item:hover{background:#1a1b22;}
.stat-num{font-family:'Bebas Neue',sans-serif;font-size:52px;letter-spacing:2px;color:var(--amber);line-height:1;}
.stat-label{font-size:12px;color:rgba(255,255,255,.35);margin-top:8px;line-height:1.5;}

/* SECTIONS */
.section{padding:96px 48px;}
@media(max-width:768px){.section{padding:64px 20px;}}
.section-inner{max-width:1200px;margin:0 auto;}
.section-alt{background:var(--bg3);transition:background .3s;}
.section-dark{background:#0c0d10;}
.eyebrow{display:inline-block;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:3px;color:var(--amber);margin-bottom:14px;}
.sh{font-family:'Bebas Neue',sans-serif;font-size:clamp(44px,6vw,80px);letter-spacing:3px;color:var(--text);line-height:.95;margin-bottom:14px;transition:color .3s;}
.sh-inv{color:#ffffff;}
.sl{font-size:16px;color:var(--muted);max-width:540px;line-height:1.7;margin-bottom:48px;transition:color .3s;}
.sl-inv{color:rgba(255,255,255,.4);}

/* PROBLEMS */
.problems{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;}
@media(max-width:768px){.problems{grid-template-columns:1fr;}}
.problem-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:36px 28px;transition:all .3s;position:relative;overflow:hidden;}
.problem-card:hover{transform:translateY(-6px);box-shadow:0 20px 40px var(--shadow2);}
.problem-num{font-family:'Bebas Neue',sans-serif;font-size:68px;letter-spacing:2px;color:var(--amber);opacity:.15;line-height:1;margin-bottom:14px;}
.problem-card h3{font-family:'Bebas Neue',sans-serif;font-size:26px;letter-spacing:1.5px;color:var(--text);margin-bottom:10px;transition:color .3s;}
.problem-card p{font-size:14px;color:var(--muted);line-height:1.7;}

/* STEPS */
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.04);border-radius:20px;overflow:hidden;}
@media(max-width:900px){.steps{grid-template-columns:repeat(2,1fr);}}
@media(max-width:500px){.steps{grid-template-columns:1fr;}}
.step{background:#131419;padding:32px 24px;transition:background .2s;position:relative;overflow:hidden;}
.step::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--amber),transparent);transform:scaleX(0);transition:transform .4s;}
.step:hover{background:#1a1b22;}
.step:hover::after{transform:scaleX(1);}
.step-num{font-size:10px;font-weight:700;color:var(--amber);letter-spacing:2px;text-transform:uppercase;margin-bottom:18px;display:flex;align-items:center;gap:8px;}
.step-num::before{content:'';width:16px;height:1px;background:var(--amber);}
.step-icon{font-size:28px;margin-bottom:12px;display:block;transition:transform .3s;}
.step:hover .step-icon{transform:scale(1.15) rotate(-5deg);}
.step h3{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:1.5px;color:#fff;margin-bottom:8px;}
.step p{font-size:13px;color:rgba(255,255,255,.4);line-height:1.7;}

/* FEATURES */
.features{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;}
@media(max-width:768px){.features{grid-template-columns:1fr;}}
.feature{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:30px 26px;transition:all .3s;overflow:hidden;position:relative;}
.feature-shine{position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,255,255,0),rgba(255,255,255,.05),rgba(255,255,255,0));transform:translateX(-100%);transition:transform .5s;}
.feature:hover .feature-shine{transform:translateX(100%);}
.feature:hover{transform:translateY(-5px);box-shadow:0 18px 36px var(--shadow2);}
.feature-icon{width:44px;height:44px;background:var(--amber-dim);border:1px solid rgba(245,158,11,.2);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;margin-bottom:16px;transition:transform .3s;}
.feature:hover .feature-icon{transform:scale(1.1) rotate(-5deg);}
.feature h3{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:1.5px;color:var(--text);margin-bottom:8px;transition:color .3s;}
.feature p{font-size:13px;color:var(--muted);line-height:1.7;}

/* TESTIMONIAL */
.testimonial-section{background:var(--amber);padding:96px 48px;position:relative;overflow:hidden;}
.testimonial-section::before{content:'"';font-family:'Bebas Neue',sans-serif;font-size:320px;color:rgba(0,0,0,.06);position:absolute;top:-60px;left:-10px;line-height:1;pointer-events:none;}
@media(max-width:768px){.testimonial-section{padding:64px 20px;}}
.t-inner{max-width:780px;margin:0 auto;text-align:center;position:relative;z-index:1;}
.t-stars{font-size:22px;letter-spacing:4px;margin-bottom:18px;}
.t-quote{font-family:'Bebas Neue',sans-serif;font-size:clamp(26px,4vw,50px);letter-spacing:2px;color:#0c0d10;line-height:1.1;margin-bottom:24px;}
.t-author{font-size:13px;color:rgba(12,13,16,.5);letter-spacing:.5px;font-weight:500;}

/* PRICING */
.price-wrap{max-width:480px;margin:0 auto;}
.price-card{background:var(--card);border:1.5px solid var(--border);border-radius:24px;overflow:hidden;box-shadow:0 24px 56px var(--shadow2);transition:all .3s;}
.price-card:hover{transform:translateY(-4px);box-shadow:0 36px 72px var(--shadow2);}
.price-top{background:linear-gradient(135deg,#0c0d10,#1a1b22);padding:36px 40px;position:relative;overflow:hidden;}
.price-top::after{content:'';position:absolute;top:-60%;right:-20%;width:300px;height:300px;background:radial-gradient(circle,rgba(245,158,11,.1),transparent 65%);animation:price-glow 6s ease-in-out infinite;}
@keyframes price-glow{0%,100%{transform:translate(0,0);}50%{transform:translate(-5%,-5%);}}
.price-tag{display:inline-block;background:var(--amber);color:#0c0d10;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;padding:4px 12px;border-radius:100px;margin-bottom:18px;position:relative;z-index:1;}
.price-amount{font-family:'Bebas Neue',sans-serif;font-size:84px;letter-spacing:2px;color:#fff;line-height:1;position:relative;z-index:1;}
.price-amount sup{font-size:36px;vertical-align:top;margin-top:18px;display:inline-block;letter-spacing:0;}
.price-period{font-size:13px;color:rgba(255,255,255,.35);margin-top:4px;position:relative;z-index:1;}
.price-trial{font-size:14px;color:var(--amber);font-weight:600;margin-top:8px;position:relative;z-index:1;}
.price-bottom{padding:32px 40px;}
.price-feature{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);font-size:14px;color:var(--text);font-weight:500;transition:padding-left .2s,border-color .3s,color .3s;}
.price-feature:hover{padding-left:4px;}
.price-feature:last-of-type{border-bottom:none;margin-bottom:24px;}
.check{width:20px;height:20px;background:var(--amber);border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:10px;color:#0c0d10;font-weight:800;box-shadow:0 2px 8px var(--amber-glow);}
.btn-pricing{display:block;width:100%;background:var(--amber);color:#0c0d10;padding:15px;border-radius:10px;font-size:14px;font-weight:700;text-align:center;text-decoration:none;transition:all .2s;font-family:'Manrope',sans-serif;letter-spacing:.5px;box-shadow:0 4px 0 var(--amber-dark),0 8px 20px var(--amber-glow);}
.btn-pricing:hover{transform:translateY(-2px);box-shadow:0 6px 0 var(--amber-dark),0 14px 28px var(--amber-glow);}
.price-note{font-size:11px;color:var(--faint);text-align:center;margin-top:12px;}

/* CTA */
.cta-section{background:#0c0d10;padding:96px 48px;text-align:center;position:relative;overflow:hidden;}
.cta-section::before{content:'';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:500px;height:500px;background:radial-gradient(circle,rgba(245,158,11,.07),transparent 70%);pointer-events:none;}
@media(max-width:768px){.cta-section{padding:64px 20px;}}
.cta-h{font-family:'Bebas Neue',sans-serif;font-size:clamp(52px,8vw,100px);letter-spacing:4px;color:#fff;line-height:.92;margin-bottom:16px;position:relative;z-index:1;}
.cta-h .accent{color:var(--amber);}
.cta-sub{font-size:16px;color:rgba(255,255,255,.4);margin-bottom:36px;position:relative;z-index:1;}
.btn-cta{display:inline-block;background:var(--amber);color:#0c0d10;padding:16px 40px;border-radius:10px;font-size:15px;font-weight:700;text-decoration:none;font-family:'Manrope',sans-serif;letter-spacing:.5px;box-shadow:0 6px 0 var(--amber-dark),0 12px 36px var(--amber-glow);transition:all .2s;position:relative;z-index:1;}
.btn-cta:hover{transform:translateY(-3px);box-shadow:0 9px 0 var(--amber-dark),0 20px 44px var(--amber-glow);}
.btn-cta:active{transform:translateY(1px);box-shadow:0 2px 0 var(--amber-dark);}

/* FOOTER */
footer{background:#0c0d10;padding:36px 48px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px;border-top:1px solid rgba(255,255,255,.06);}
.footer-logo{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:2px;color:#fff;display:flex;align-items:center;gap:10px;}
.footer-note{font-size:12px;color:rgba(255,255,255,.2);}
@media(max-width:480px){footer{padding:28px 20px;}}

/* SCROLL REVEAL */
.reveal{opacity:0;transform:translateY(32px);transition:opacity .7s cubic-bezier(.16,1,.3,1),transform .7s cubic-bezier(.16,1,.3,1);}
.reveal.visible{opacity:1;transform:translateY(0);}
.reveal-d1{transition-delay:.1s;}.reveal-d2{transition-delay:.2s;}.reveal-d3{transition-delay:.3s;}
.reveal-d4{transition-delay:.4s;}.reveal-d5{transition-delay:.5s;}

/* HERO ANIMATIONS */
.hero-badge{animation:fadeUp .6s ease .05s both;}
h1{animation:fadeUp .7s cubic-bezier(.16,1,.3,1) .15s both;}
.hero-sub{animation:fadeUp .7s ease .25s both;}
.hero-actions{animation:fadeUp .7s ease .35s both;}
.hero-note{animation:fadeUp .7s ease .45s both;}
.hero-right{animation:fadeUp .8s ease .2s both;}
@keyframes fadeUp{from{opacity:0;transform:translateY(24px);}to{opacity:1;transform:translateY(0);}}

/* DESKTOP cursor */
@media(pointer:fine){
  body{cursor:none;}
  #n2q-cursor{position:fixed;width:10px;height:10px;background:var(--amber);border-radius:50%;pointer-events:none;z-index:9999;transform:translate(-50%,-50%);transition:width .2s,height .2s;}
  #n2q-ring{position:fixed;width:36px;height:36px;border:1.5px solid var(--amber);border-radius:50%;pointer-events:none;z-index:9998;transform:translate(-50%,-50%);opacity:.5;transition:width .2s,height .2s;}
}
</style>
</head>
<body>

<!-- SVG Logo Symbol -->
<svg style="display:none" aria-hidden="true">
  <symbol id="n2q" viewBox="0 0 40 40">
    <path d="M20 5h10l9 9v18a2 2 0 01-2 2H20a2 2 0 01-2-2V7a2 2 0 012-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M30 5v9h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="23" y1="20" x2="35" y2="20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <line x1="23" y1="25" x2="31" y2="25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <rect x="3" y="11" width="10" height="14" rx="5" fill="#f59e0b"/>
    <path d="M1 21.5a7 7 0 0014 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="8" y1="28.5" x2="8" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="5" y1="33" x2="11" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  </symbol>
</svg>

<!-- Desktop cursor (hidden on mobile via CSS) -->
<div id="n2q-cursor"></div>
<div id="n2q-ring"></div>

<!-- NAV -->
<nav>
  <a href="/" class="nav-logo">
    <svg width="26" height="26" viewBox="0 0 40 40"><use href="#n2q" color="currentColor"/></svg>
    Note2Quote
  </a>
  <ul class="nav-links">
    <li><a href="#how">How it works</a></li>
    <li><a href="#features">Features</a></li>
    <li><a href="#pricing">Pricing</a></li>
  </ul>
  <div class="nav-right">
    <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn">🌙</button>
    <a href="/signup" class="nav-cta">Start free trial →</a>
  </div>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-orb"></div>
  <div class="hero-inner">
    <div>
      <div class="hero-badge"><span class="badge-dot"></span> ⚡ Built for UK Subcontractors</div>
      <h1>FROM A<br>VOICE<br>NOTE TO A<br><span class="accent">POLISHED</span><br><span class="accent">PDF.</span><br>INSTANTLY.</h1>
      <p class="hero-sub">Your site chat, automatically converted into professional quotes, invoices, and variation orders. Zero logging. Zero desk time.</p>
      <div class="hero-actions">
        <a href="/signup" class="btn-3d btn-3d-primary">Start 14-day free trial ↗</a>
        <a href="#how" class="btn-3d btn-3d-secondary">See how it works</a>
      </div>
      <p class="hero-note">No app · No contract · Cancel anytime</p>
    </div>
    <div class="hero-right">
      <div class="phone-scene" id="phone-scene">
        <div class="phone-3d" id="phone-3d">
          <div class="phone-notch"></div>
          <div class="phone-screen">
            <div class="wa-bar">
              <div class="wa-av"><svg width="13" height="13" viewBox="0 0 40 40"><use href="#n2q" color="#0c0d10"/></svg></div>
              <div><div class="wa-n">Note2Quote</div><div class="wa-s">● online</div></div>
            </div>
            <div class="chat-area">
              <div class="msg out"><div class="bub out">🎙 Voice note 0:09</div><div class="bub-time">15:23</div></div>
              <div class="msg"><div class="bub in">✅ Extra consumer unit · garage · 4hrs · £120 mats<br><br>📍 Brookfield Site</div><div class="bub-time">15:23</div></div>
              <div class="msg out"><div class="bub out">Generate variations Brookfield</div><div class="bub-time">17:30</div></div>
              <div class="msg"><div class="bub in">📄 <strong>VO-003</strong> · 4 items · £340 · Ready ✅</div><div class="bub-time">17:30</div></div>
            </div>
          </div>
        </div>
      </div>
      <div class="float-card fc1">
        <div class="fc-label">Saved this week</div>
        <div class="fc-val">£1,240</div>
        <div class="fc-sub">3 variation orders</div>
      </div>
      <div class="float-card fc2">
        <div class="fc-label">Time saved</div>
        <div class="fc-val">5.2HRS</div>
        <div class="fc-sub">This week</div>
      </div>
    </div>
  </div>
</section>

<!-- MARQUEE -->
<div class="marquee-wrap">
  <div class="marquee-track">
    <div class="marquee-item"><span class="marquee-dot"></span>Variation Orders</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Daywork Sheets</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Purchase Orders</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Voice Notes</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Branded PDFs</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Multi-Site Tracking</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Client Emails</div>
    <div class="marquee-item"><span class="marquee-dot"></span>WhatsApp Native</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Instant Quotes</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Variation Orders</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Daywork Sheets</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Purchase Orders</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Voice Notes</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Branded PDFs</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Multi-Site Tracking</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Client Emails</div>
    <div class="marquee-item"><span class="marquee-dot"></span>WhatsApp Native</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Instant Quotes</div>
  </div>
</div>

<!-- STATS -->
<section class="stats-section">
  <div class="stats-grid">
    <div class="stat-item reveal"><div class="stat-num" data-target="4800">£4.8K</div><div class="stat-label">Average lost per year on forgotten site extras</div></div>
    <div class="stat-item reveal reveal-d1"><div class="stat-num" data-target="5">5HRS</div><div class="stat-label">Admin time saved every single week</div></div>
    <div class="stat-item reveal reveal-d2"><div class="stat-num" data-target="10">10SEC</div><div class="stat-label">To log anything from site via WhatsApp</div></div>
    <div class="stat-item reveal reveal-d3"><div class="stat-num" data-target="49">£49</div><div class="stat-label">Per month — less than 2 hours labour</div></div>
  </div>
</section>

<!-- PROBLEM -->
<section class="section section-alt">
  <div class="section-inner">
    <span class="eyebrow reveal">The Problem</span>
    <h2 class="sh reveal">Sound familiar?</h2>
    <p class="sl reveal">Every subcontractor knows the feeling. You do the extra work. You forget to log it. The invoice goes out short.</p>
    <div class="problems">
      <div class="problem-card reveal"><div class="problem-num">01</div><h3>Forgotten variations</h3><p>Site manager asks for something extra. By the time you're home writing invoices, it's gone from your head.</p></div>
      <div class="problem-card reveal reveal-d2"><div class="problem-num">02</div><h3>Evening admin nightmare</h3><p>Hours every evening on paperwork. Invoices, daywork sheets, purchase orders — all built from scratch after a full day's graft.</p></div>
      <div class="problem-card reveal reveal-d3"><div class="problem-num">03</div><h3>Material order chaos</h3><p>Workers ask for materials on site, you say you'll sort it. Half the time it slips. Job stops. Everyone waits. Money lost.</p></div>
    </div>
  </div>
</section>

<!-- HOW -->
<section class="section section-dark" id="how">
  <div class="section-inner">
    <span class="eyebrow reveal">How It Works</span>
    <h2 class="sh sh-inv reveal">Simple as sending a text</h2>
    <p class="sl sl-inv reveal">No app to learn. No forms to fill. Just WhatsApp — the tool already in your pocket on site.</p>
    <div class="steps">
      <div class="step reveal"><div class="step-num">Step 01</div><span class="step-icon">🎙</span><h3>Voice or text</h3><p>Send a message to Note2Quote on WhatsApp. Speak naturally — exactly like you'd tell a mate on site.</p></div>
      <div class="step reveal reveal-d1"><div class="step-num">Step 02</div><span class="step-icon">🧠</span><h3>AI classifies it</h3><p>Note2Quote reads your message, works out the type and saves it tagged to the right site automatically.</p></div>
      <div class="step reveal reveal-d2"><div class="step-num">Step 03</div><span class="step-icon">📄</span><h3>PDF generated</h3><p>Say "generate variations for Site X." Branded PDF in seconds — ready to download or send.</p></div>
      <div class="step reveal reveal-d3"><div class="step-num">Step 04</div><span class="step-icon">✉️</span><h3>Send to client</h3><p>Review on your dashboard, send to your client. Done in under a minute.</p></div>
    </div>
  </div>
</section>

<!-- FEATURES -->
<section class="section" id="features">
  <div class="section-inner">
    <span class="eyebrow reveal">Features</span>
    <h2 class="sh reveal">Everything you need,<br>nothing you don't</h2>
    <p class="sl reveal">Built for the way tradespeople actually work — on site, hands dirty, no time for admin.</p>
    <div class="features">
      <div class="feature reveal"><div class="feature-shine"></div><div class="feature-icon">🎙</div><h3>Voice Notes</h3><p>Send a voice note and we transcribe it automatically. Hands dirty, no typing needed.</p></div>
      <div class="feature reveal reveal-d1"><div class="feature-shine"></div><div class="feature-icon">💬</div><h3>WhatsApp Native</h3><p>No new app. Works on any phone. Your whole team logs from day one with zero training.</p></div>
      <div class="feature reveal reveal-d2"><div class="feature-shine"></div><div class="feature-icon">🎨</div><h3>Fully Branded</h3><p>Your logo, colours, address, VAT on every document. Looks professional every time.</p></div>
      <div class="feature reveal reveal-d1"><div class="feature-shine"></div><div class="feature-icon">📍</div><h3>Multi-Site</h3><p>Every log tagged to the right client and site. No mixing, no confusion, no lost invoices.</p></div>
      <div class="feature reveal reveal-d2"><div class="feature-shine"></div><div class="feature-icon">📊</div><h3>Boss Dashboard</h3><p>Edit entries, generate PDFs, download or email directly to clients in one click.</p></div>
      <div class="feature reveal reveal-d3"><div class="feature-shine"></div><div class="feature-icon">📦</div><h3>Bulk Logging</h3><p>Send a numbered list and we log every item at once. End of day dump in one message.</p></div>
    </div>
  </div>
</section>

<!-- TESTIMONIAL -->
<section class="testimonial-section">
  <div class="t-inner reveal">
    <div class="t-stars">⭐⭐⭐⭐⭐</div>
    <p class="t-quote">"I USED TO SPEND THREE HOURS EVERY SUNDAY DOING PAPERWORK. NOW IT TAKES TEN MINUTES."</p>
    <p class="t-author">Beta tester · Electrical Subcontractor · Essex</p>
  </div>
</section>

<!-- PRICING -->
<section class="section" id="pricing">
  <div class="section-inner">
    <span class="eyebrow reveal" style="display:block;text-align:center">Pricing</span>
    <h2 class="sh reveal" style="text-align:center;margin-bottom:8px">One price.<br>Everything included.</h2>
    <p class="sl reveal" style="text-align:center;margin:0 auto 44px">No setup fees. No per-document charges. No surprises.</p>
    <div class="price-wrap reveal">
      <div class="price-card">
        <div class="price-top">
          <div class="price-tag">Most popular</div>
          <div class="price-amount"><sup>£</sup>49</div>
          <div class="price-period">per month, billed monthly</div>
          <div class="price-trial">✓ 14 days free — no card charged upfront</div>
        </div>
        <div class="price-bottom">
          <div class="price-feature"><span class="check">✓</span>Unlimited WhatsApp logging</div>
          <div class="price-feature"><span class="check">✓</span>Voice note transcription</div>
          <div class="price-feature"><span class="check">✓</span>Variation orders, dayworks & purchase orders</div>
          <div class="price-feature"><span class="check">✓</span>Branded PDFs with your logo</div>
          <div class="price-feature"><span class="check">✓</span>Multi-site tracking</div>
          <div class="price-feature"><span class="check">✓</span>Boss dashboard with inline editing</div>
          <div class="price-feature"><span class="check">✓</span>Direct client email sending</div>
          <div class="price-feature"><span class="check">✓</span>Unlimited team members</div>
          <a href="/signup" class="btn-pricing">Start 14-day free trial →</a>
          <p class="price-note">Cancel anytime. No contract. No setup fee.</p>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- CTA -->
<section class="cta-section">
  <h2 class="cta-h reveal">Stop losing<br><span class="accent">money.</span></h2>
  <p class="cta-sub reveal">Join subcontractors across the UK saving hours every week.</p>
  <a href="/signup" class="btn-cta reveal">Start your free trial →</a>
</section>

<!-- FOOTER -->
<footer>
  <div class="footer-logo"><svg width="22" height="22" viewBox="0 0 40 40"><use href="#n2q" color="white"/></svg>Note2Quote</div>
  <p class="footer-note">© 2026 Note2Quote · Built for UK Subcontractors</p>
</footer>

<script>
/* Theme */
function toggleTheme(){
  var h=document.documentElement,b=document.getElementById('theme-btn');
  var n=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',n);
  b.textContent=n==='dark'?'☀️':'🌙';
  localStorage.setItem('n2q-theme',n);
}
var saved=localStorage.getItem('n2q-theme');
if(saved){document.documentElement.setAttribute('data-theme',saved);var tb=document.getElementById('theme-btn');if(tb)tb.textContent=saved==='dark'?'☀️':'🌙';}

/* Scroll reveal */
if('IntersectionObserver' in window){
  var io=new IntersectionObserver(function(entries){entries.forEach(function(e){if(e.isIntersecting)e.target.classList.add('visible');});},{threshold:.1,rootMargin:'0px 0px -40px 0px'});
  document.querySelectorAll('.reveal').forEach(function(el){io.observe(el);});
}else{
  document.querySelectorAll('.reveal').forEach(function(el){el.classList.add('visible');});
}

/* Desktop-only effects */
if(window.matchMedia('(pointer:fine)').matches){
  /* Cursor */
  var cur=document.getElementById('n2q-cursor'),ring=document.getElementById('n2q-ring');
  var mx=0,my=0,rx=0,ry=0;
  document.addEventListener('mousemove',function(e){mx=e.clientX;my=e.clientY;cur.style.left=mx+'px';cur.style.top=my+'px';});
  (function animR(){rx+=(mx-rx)*.12;ry+=(my-ry)*.12;ring.style.left=rx+'px';ring.style.top=ry+'px';requestAnimationFrame(animR);})();

  /* 3D phone tilt */
  var phone=document.getElementById('phone-3d'),scene=document.getElementById('phone-scene');
  if(scene&&phone){
    scene.addEventListener('mousemove',function(e){var r=scene.getBoundingClientRect();var dx=(e.clientX-r.left-r.width/2)/(r.width/2);var dy=(e.clientY-r.top-r.height/2)/(r.height/2);phone.style.transform='rotateX('+(-dy*12)+'deg) rotateY('+(dx*16)+'deg)';});
    scene.addEventListener('mouseleave',function(){phone.style.transform='rotateX(4deg) rotateY(-12deg)';});
  }

  /* Magnetic buttons */
  document.querySelectorAll('.btn-3d').forEach(function(btn){
    btn.addEventListener('mousemove',function(e){var r=btn.getBoundingClientRect();var dx=e.clientX-(r.left+r.width/2);var dy=e.clientY-(r.top+r.height/2);btn.style.transform='translate('+(dx*.15)+'px,'+(dy*.15)+'px)';});
    btn.addEventListener('mouseleave',function(){btn.style.transform='';});
  });
}
</script>
</body>
</html>
'''



# ── Summary command ─────────────────────────────────────────────────────────
def handle_summary(from_number):
    try:
        encoded   = encode_number(from_number)
        logs      = db_get("site_logs?from_number=eq." + encoded + "&order=created_at.desc")
        companies = db_get("companies?whatsapp_number=eq." + encoded + "&limit=1")
        company_name = companies[0].get("company_name", "Your Company") if isinstance(companies, list) and companies else "Your Company"
        if not isinstance(logs, list): logs = []
        pending   = [l for l in logs if l.get("status") == "pending"]
        approved  = [l for l in logs if l.get("status") == "approved"]
        chasing   = [l for l in logs if l.get("status") == "chasing"]
        sent      = [l for l in logs if l.get("status") == "sent"]
        pend_val  = sum(float(l.get("cost_estimate") or 0) for l in pending)
        appr_val  = sum(float(l.get("cost_estimate") or 0) for l in approved)
        chase_val = sum(float(l.get("cost_estimate") or 0) for l in chasing)
        site_map  = {}
        for l in pending:
            site = l.get("site_name") or "Unassigned"
            if site not in site_map: site_map[site] = {"count": 0, "value": 0.0}
            site_map[site]["count"] += 1
            site_map[site]["value"] += float(l.get("cost_estimate") or 0)
        out = []
        out.append("📊 *" + company_name + " — Dashboard Summary*")
        out.append("")
        out.append("📋 *Pending:* " + str(len(pending)) + " items · £" + ("%.2f" % pend_val))
        out.append("✅ *Approved:* " + str(len(approved)) + " items · £" + ("%.2f" % appr_val))
        out.append("⏰ *Chasing:* " + str(len(chasing)) + " items · £" + ("%.2f" % chase_val))
        out.append("📄 *Docs sent:* " + str(len(sent)) + " total")
        out.append("")
        if site_map:
            out.append("*By Site (pending):*")
            for site, info in sorted(site_map.items(), key=lambda x: -x[1]["value"]):
                out.append("📍 " + site + " — " + str(info["count"]) + " items · £" + ("%.2f" % info["value"]))
            out.append("")
        out.append("*Quick commands:*")
        out.append("Reply *pending* — see full list")
        out.append("Reply *approve [site]* — mark as approved")
        out.append("Reply *my dashboard* — get login link")
        return _reply("\n".join(out))
    except Exception as e:
        return _reply("⚠️ Couldn't get summary. (" + str(e)[:80] + ")")


# ── Pending list command ──────────────────────────────────────────────────────
def handle_pending_summary(from_number):
    try:
        encoded = encode_number(from_number)
        logs    = db_get("site_logs?from_number=eq." + encoded + "&status=eq.pending&order=created_at.desc&limit=20")
        if not isinstance(logs, list) or not logs:
            return _reply("✅ No pending items! All caught up.")
        total_val = sum(float(l.get("cost_estimate") or 0) for l in logs)
        out = ["📋 *Pending Items (" + str(len(logs)) + ") · £" + ("%.2f" % total_val) + "*", ""]
        for i, l in enumerate(logs, 1):
            site  = l.get("site_name") or "No site"
            desc  = (l.get("description") or "—")[:40]
            cost  = "£" + ("%.2f" % float(l.get("cost_estimate") or 0)) if l.get("cost_estimate") else ""
            ltype = (l.get("type") or "?").replace("_", " ").title()
            out.append(str(i) + ". *" + ltype + "* — " + desc)
            out.append("   📍 " + site + (" · " + cost if cost else ""))
        out.append("")
        out.append("Reply *approve [site name]* to mark as approved")
        out.append("Reply *summary* for full overview")
        return _reply("\n".join(out))
    except Exception as e:
        return _reply("⚠️ Couldn't get pending list. (" + str(e)[:80] + ")")


# ── Status update command ─────────────────────────────────────────────────────
def handle_status_update(from_number, msg):
    try:
        from urllib.parse import quote as _quote
        msg_lower = msg.lower()
        projects  = get_projects(from_number)
        encoded   = encode_number(from_number)
        if any(kw in msg_lower for kw in APPROVE_KEYWORDS):
            new_status, past_tense, emoji = "approved", "approved", "✅"
        elif any(kw in msg_lower for kw in CHASE_KEYWORDS):
            new_status, past_tense, emoji = "chasing", "marked as chasing", "⏰"
        elif any(kw in msg_lower for kw in CANCEL_KEYWORDS):
            new_status, past_tense, emoji = "cancelled", "cancelled", "❌"
        else:
            return _reply("Could not understand status command. Try: *approve [site name]*")
        site_name = match_site(msg, projects)
        if site_name:
            logs = db_get("site_logs?from_number=eq." + encoded + "&status=eq.pending&site_name=eq." + _quote(site_name))
            if not isinstance(logs, list) or not logs:
                return _reply("No pending items found for *" + site_name + "*.")
            for log in logs:
                db_patch("site_logs?id=eq." + str(log["id"]), {"status": new_status})
            total_val = sum(float(l.get("cost_estimate") or 0) for l in logs)
            return _reply(emoji + " *" + str(len(logs)) + " item(s) " + past_tense + "* for *" + site_name + "*\n"
                          + "Total value: £" + ("%.2f" % total_val) + "\n\n"
                          + "Reply *summary* to see your full overview.")
        elif projects:
            pending_selections[from_number] = {
                "pending_log": {"_status_update": True, "new_status": new_status, "past_tense": past_tense, "emoji": emoji},
                "projects": projects, "awaiting_type": False, "awaiting_site": True,
            }
            out = ["Which site do you want to " + new_status + "?\n"]
            for i, p in enumerate(projects, 1):
                out.append(str(i) + ". " + p["site_name"])
            out.append("\nReply with a number or site name.")
            return _reply("\n".join(out))
        else:
            return _reply("No sites found. Add sites via WhatsApp or your account portal.")
    except Exception as e:
        return _reply("⚠️ Couldn't update status. (" + str(e)[:80] + ")")



# ── Pages ─────────────────────────────────────────────────────────────────────
LANDING_HTML   = '''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Note2Quote — From Voice Note to Polished PDF. Instantly.</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --amber:#f59e0b;--amber-dark:#d97706;--amber-light:#fbbf24;
  --amber-dim:rgba(245,158,11,0.12);--amber-glow:rgba(245,158,11,0.25);
}
[data-theme="light"]{
  --bg:#f5f3ef;--bg2:#ffffff;--bg3:#eceae5;--bg4:#e2dfd8;
  --text:#0c0d10;--text2:#2d2f36;--muted:#6b7280;--faint:#a0a5b0;
  --border:#dddad4;--border2:#c8c4bc;--card:#ffffff;
  --nav:rgba(245,243,239,0.95);
  --shadow:rgba(0,0,0,0.06);--shadow2:rgba(0,0,0,0.12);
}
[data-theme="dark"]{
  --bg:#0c0d10;--bg2:#131419;--bg3:#1a1b22;--bg4:#21232c;
  --text:#f0ede8;--text2:#c8c4bc;--muted:#8b8fa8;--faint:#4a4d5e;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);--card:#131419;
  --nav:rgba(12,13,16,0.95);
  --shadow:rgba(0,0,0,0.4);--shadow2:rgba(0,0,0,0.6);
}
*{box-sizing:border-box;margin:0;padding:0;}
html{scroll-behavior:smooth;}
body{font-family:'Manrope',sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;overflow-x:hidden;transition:background .3s,color .3s;}

/* NAV */
nav{position:fixed;top:0;width:100%;z-index:100;padding:0 48px;height:68px;display:flex;align-items:center;justify-content:space-between;background:var(--nav);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);transition:background .3s,border-color .3s;}
.nav-logo{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:3px;color:var(--text);display:flex;align-items:center;gap:10px;text-decoration:none;}
.nav-links{display:flex;gap:28px;list-style:none;}
.nav-links a{color:var(--muted);font-size:13px;font-weight:500;text-decoration:none;transition:color .2s;}
.nav-links a:hover{color:var(--amber);}
.nav-right{display:flex;align-items:center;gap:10px;}
.theme-toggle{width:36px;height:36px;border-radius:8px;border:1px solid var(--border);background:var(--card);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:15px;transition:all .2s;}
.theme-toggle:hover{border-color:var(--amber);}
.nav-cta{background:var(--amber);color:#0c0d10;padding:9px 22px;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;transition:all .2s;}
.nav-cta:hover{background:var(--amber-light);transform:translateY(-1px);}
@media(max-width:768px){nav{padding:0 20px;}.nav-links{display:none;}}

/* HERO */
.hero{min-height:100vh;display:flex;align-items:center;padding:100px 48px 80px;background:var(--bg);position:relative;overflow:hidden;}
.hero-orb{position:absolute;top:50%;right:-100px;transform:translateY(-50%);width:600px;height:600px;background:radial-gradient(circle,var(--amber-glow) 0%,transparent 65%);pointer-events:none;animation:orb-pulse 6s ease-in-out infinite;}
@keyframes orb-pulse{0%,100%{transform:translateY(-50%) scale(1);opacity:.6;}50%{transform:translateY(-52%) scale(1.08);opacity:1;}}
.hero-inner{max-width:1200px;margin:0 auto;width:100%;display:grid;grid-template-columns:1fr 480px;gap:80px;align-items:center;position:relative;z-index:2;}
@media(max-width:1060px){.hero-inner{grid-template-columns:1fr;}.hero-right{display:none;}}
@media(max-width:768px){.hero{padding:100px 20px 60px;}}

.hero-badge{display:inline-flex;align-items:center;gap:8px;background:var(--amber-dim);border:1px solid rgba(245,158,11,0.3);padding:6px 16px;border-radius:100px;font-size:11px;font-weight:700;color:var(--amber-dark);text-transform:uppercase;letter-spacing:2px;margin-bottom:24px;}
.badge-dot{width:6px;height:6px;background:var(--amber);border-radius:50%;display:inline-block;animation:blink 2s ease infinite;}
@keyframes blink{0%,100%{opacity:1;}50%{opacity:.2;}}

h1{font-family:'Bebas Neue',sans-serif;font-size:clamp(64px,9vw,120px);line-height:.92;letter-spacing:4px;color:var(--text);margin-bottom:24px;transition:color .3s;}
h1 .accent{color:var(--amber);}
.hero-sub{font-size:16px;line-height:1.75;color:var(--muted);max-width:460px;margin-bottom:40px;font-weight:400;}
.hero-actions{display:flex;gap:14px;flex-wrap:wrap;}
.btn-3d{padding:14px 26px;border-radius:10px;font-size:14px;font-weight:700;text-decoration:none;display:inline-flex;align-items:center;gap:8px;font-family:'Manrope',sans-serif;letter-spacing:.3px;transition:all .15s;}
.btn-3d-primary{background:var(--amber);color:#0c0d10;box-shadow:0 4px 0 var(--amber-dark),0 8px 20px var(--amber-glow);}
.btn-3d-primary:hover{transform:translateY(-2px);box-shadow:0 6px 0 var(--amber-dark),0 14px 28px var(--amber-glow);}
.btn-3d-primary:active{transform:translateY(1px);box-shadow:0 2px 0 var(--amber-dark);}
.btn-3d-secondary{background:var(--card);color:var(--text);border:1px solid var(--border);box-shadow:0 4px 0 var(--border2),0 6px 16px var(--shadow);}
.btn-3d-secondary:hover{transform:translateY(-2px);box-shadow:0 6px 0 var(--border2),0 10px 20px var(--shadow2);}
.hero-note{font-size:11px;color:var(--faint);margin-top:16px;letter-spacing:.5px;}

/* HERO PHONE */
.hero-right{position:relative;}
.phone-scene{animation:float 5s ease-in-out infinite;}
@keyframes float{0%,100%{transform:translateY(0);}50%{transform:translateY(-14px);}}
.phone-3d{background:#0c0d10;border-radius:36px;padding:12px;box-shadow:0 0 0 1px rgba(255,255,255,0.06),0 40px 80px rgba(0,0,0,.45);transform:rotateX(4deg) rotateY(-12deg);transition:transform .5s cubic-bezier(.34,1.56,.64,1);max-width:290px;margin:0 auto;}
.phone-notch{width:70px;height:20px;background:#0c0d10;border-radius:10px;margin:0 auto 10px;}
.phone-screen{background:#1a1b22;border-radius:26px;overflow:hidden;}
.wa-bar{background:#0d0f14;padding:12px 14px;display:flex;align-items:center;gap:10px;}
.wa-av{width:30px;height:30px;background:var(--amber);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0;}
.wa-n{font-family:'Bebas Neue',sans-serif;font-size:13px;letter-spacing:1px;color:#fff;}
.wa-s{font-size:10px;color:rgba(255,255,255,.4);}
.chat-area{padding:12px;display:flex;flex-direction:column;gap:8px;background:#1e2028;}
.msg{display:flex;flex-direction:column;}
.msg.out{align-items:flex-end;}
.bub{padding:8px 11px;border-radius:13px;font-size:11px;line-height:1.5;max-width:90%;}
.bub.out{background:#1a3a00;color:#a3e635;border-bottom-right-radius:3px;border:1px solid rgba(163,230,53,.1);}
.bub.in{background:rgba(245,158,11,.08);color:rgba(255,255,255,.85);border:1px solid rgba(245,158,11,.15);border-bottom-left-radius:3px;}
.bub-time{font-size:9px;color:rgba(255,255,255,.2);margin-top:2px;}
.float-card{position:absolute;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 16px;box-shadow:0 16px 36px var(--shadow2);transition:background .3s,border-color .3s;}
.fc1{right:-30px;top:20px;animation:fc1 4s ease-in-out infinite;}
.fc2{left:-40px;bottom:50px;animation:fc2 5s ease-in-out infinite;}
@keyframes fc1{0%,100%{transform:rotate(2deg);}50%{transform:translateY(-8px) rotate(2deg);}}
@keyframes fc2{0%,100%{transform:rotate(-1deg);}50%{transform:translateY(-6px) rotate(-1deg);}}
.fc-label{font-size:9px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:3px;}
.fc-val{font-family:'Bebas Neue',sans-serif;font-size:24px;letter-spacing:1.5px;color:var(--amber);}
.fc-sub{font-size:10px;color:var(--muted);margin-top:1px;}

/* MARQUEE */
.marquee-wrap{border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:14px 0;overflow:hidden;background:var(--bg2);transition:background .3s,border-color .3s;}
.marquee-track{display:flex;animation:marquee 38s linear infinite;width:max-content;}
.marquee-item{display:flex;align-items:center;gap:10px;padding:0 28px;font-size:10px;font-weight:700;color:var(--faint);white-space:nowrap;border-right:1px solid var(--border);letter-spacing:2.5px;text-transform:uppercase;}
.marquee-dot{width:4px;height:4px;background:var(--amber);border-radius:50%;flex-shrink:0;}
@keyframes marquee{from{transform:translateX(0);}to{transform:translateX(-50%);}}

/* STATS */
.stats-section{padding:80px 48px;background:#0c0d10;}
.stats-grid{max-width:1200px;margin:0 auto;display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.04);border-radius:20px;overflow:hidden;}
@media(max-width:700px){.stats-grid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:480px){.stats-section{padding:60px 20px;}}
.stat-item{background:#131419;padding:36px 24px;text-align:center;transition:background .2s;}
.stat-item:hover{background:#1a1b22;}
.stat-num{font-family:'Bebas Neue',sans-serif;font-size:52px;letter-spacing:2px;color:var(--amber);line-height:1;}
.stat-label{font-size:12px;color:rgba(255,255,255,.35);margin-top:8px;line-height:1.5;}

/* SECTIONS */
.section{padding:96px 48px;}
@media(max-width:768px){.section{padding:64px 20px;}}
.section-inner{max-width:1200px;margin:0 auto;}
.section-alt{background:var(--bg3);transition:background .3s;}
.section-dark{background:#0c0d10;}
.eyebrow{display:inline-block;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:3px;color:var(--amber);margin-bottom:14px;}
.sh{font-family:'Bebas Neue',sans-serif;font-size:clamp(44px,6vw,80px);letter-spacing:3px;color:var(--text);line-height:.95;margin-bottom:14px;transition:color .3s;}
.sh-inv{color:#ffffff;}
.sl{font-size:16px;color:var(--muted);max-width:540px;line-height:1.7;margin-bottom:48px;transition:color .3s;}
.sl-inv{color:rgba(255,255,255,.4);}

/* PROBLEMS */
.problems{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;}
@media(max-width:768px){.problems{grid-template-columns:1fr;}}
.problem-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:36px 28px;transition:all .3s;position:relative;overflow:hidden;}
.problem-card:hover{transform:translateY(-6px);box-shadow:0 20px 40px var(--shadow2);}
.problem-num{font-family:'Bebas Neue',sans-serif;font-size:68px;letter-spacing:2px;color:var(--amber);opacity:.15;line-height:1;margin-bottom:14px;}
.problem-card h3{font-family:'Bebas Neue',sans-serif;font-size:26px;letter-spacing:1.5px;color:var(--text);margin-bottom:10px;transition:color .3s;}
.problem-card p{font-size:14px;color:var(--muted);line-height:1.7;}

/* STEPS */
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.04);border-radius:20px;overflow:hidden;}
@media(max-width:900px){.steps{grid-template-columns:repeat(2,1fr);}}
@media(max-width:500px){.steps{grid-template-columns:1fr;}}
.step{background:#131419;padding:32px 24px;transition:background .2s;position:relative;overflow:hidden;}
.step::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--amber),transparent);transform:scaleX(0);transition:transform .4s;}
.step:hover{background:#1a1b22;}
.step:hover::after{transform:scaleX(1);}
.step-num{font-size:10px;font-weight:700;color:var(--amber);letter-spacing:2px;text-transform:uppercase;margin-bottom:18px;display:flex;align-items:center;gap:8px;}
.step-num::before{content:'';width:16px;height:1px;background:var(--amber);}
.step-icon{font-size:28px;margin-bottom:12px;display:block;transition:transform .3s;}
.step:hover .step-icon{transform:scale(1.15) rotate(-5deg);}
.step h3{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:1.5px;color:#fff;margin-bottom:8px;}
.step p{font-size:13px;color:rgba(255,255,255,.4);line-height:1.7;}

/* FEATURES */
.features{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;}
@media(max-width:768px){.features{grid-template-columns:1fr;}}
.feature{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:30px 26px;transition:all .3s;overflow:hidden;position:relative;}
.feature-shine{position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,255,255,0),rgba(255,255,255,.05),rgba(255,255,255,0));transform:translateX(-100%);transition:transform .5s;}
.feature:hover .feature-shine{transform:translateX(100%);}
.feature:hover{transform:translateY(-5px);box-shadow:0 18px 36px var(--shadow2);}
.feature-icon{width:44px;height:44px;background:var(--amber-dim);border:1px solid rgba(245,158,11,.2);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;margin-bottom:16px;transition:transform .3s;}
.feature:hover .feature-icon{transform:scale(1.1) rotate(-5deg);}
.feature h3{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:1.5px;color:var(--text);margin-bottom:8px;transition:color .3s;}
.feature p{font-size:13px;color:var(--muted);line-height:1.7;}

/* TESTIMONIAL */
.testimonial-section{background:var(--amber);padding:96px 48px;position:relative;overflow:hidden;}
.testimonial-section::before{content:'"';font-family:'Bebas Neue',sans-serif;font-size:320px;color:rgba(0,0,0,.06);position:absolute;top:-60px;left:-10px;line-height:1;pointer-events:none;}
@media(max-width:768px){.testimonial-section{padding:64px 20px;}}
.t-inner{max-width:780px;margin:0 auto;text-align:center;position:relative;z-index:1;}
.t-stars{font-size:22px;letter-spacing:4px;margin-bottom:18px;}
.t-quote{font-family:'Bebas Neue',sans-serif;font-size:clamp(26px,4vw,50px);letter-spacing:2px;color:#0c0d10;line-height:1.1;margin-bottom:24px;}
.t-author{font-size:13px;color:rgba(12,13,16,.5);letter-spacing:.5px;font-weight:500;}

/* PRICING */
.price-wrap{max-width:480px;margin:0 auto;}
.price-card{background:var(--card);border:1.5px solid var(--border);border-radius:24px;overflow:hidden;box-shadow:0 24px 56px var(--shadow2);transition:all .3s;}
.price-card:hover{transform:translateY(-4px);box-shadow:0 36px 72px var(--shadow2);}
.price-top{background:linear-gradient(135deg,#0c0d10,#1a1b22);padding:36px 40px;position:relative;overflow:hidden;}
.price-top::after{content:'';position:absolute;top:-60%;right:-20%;width:300px;height:300px;background:radial-gradient(circle,rgba(245,158,11,.1),transparent 65%);animation:price-glow 6s ease-in-out infinite;}
@keyframes price-glow{0%,100%{transform:translate(0,0);}50%{transform:translate(-5%,-5%);}}
.price-tag{display:inline-block;background:var(--amber);color:#0c0d10;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;padding:4px 12px;border-radius:100px;margin-bottom:18px;position:relative;z-index:1;}
.price-amount{font-family:'Bebas Neue',sans-serif;font-size:84px;letter-spacing:2px;color:#fff;line-height:1;position:relative;z-index:1;}
.price-amount sup{font-size:36px;vertical-align:top;margin-top:18px;display:inline-block;letter-spacing:0;}
.price-period{font-size:13px;color:rgba(255,255,255,.35);margin-top:4px;position:relative;z-index:1;}
.price-trial{font-size:14px;color:var(--amber);font-weight:600;margin-top:8px;position:relative;z-index:1;}
.price-bottom{padding:32px 40px;}
.price-feature{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);font-size:14px;color:var(--text);font-weight:500;transition:padding-left .2s,border-color .3s,color .3s;}
.price-feature:hover{padding-left:4px;}
.price-feature:last-of-type{border-bottom:none;margin-bottom:24px;}
.check{width:20px;height:20px;background:var(--amber);border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:10px;color:#0c0d10;font-weight:800;box-shadow:0 2px 8px var(--amber-glow);}
.btn-pricing{display:block;width:100%;background:var(--amber);color:#0c0d10;padding:15px;border-radius:10px;font-size:14px;font-weight:700;text-align:center;text-decoration:none;transition:all .2s;font-family:'Manrope',sans-serif;letter-spacing:.5px;box-shadow:0 4px 0 var(--amber-dark),0 8px 20px var(--amber-glow);}
.btn-pricing:hover{transform:translateY(-2px);box-shadow:0 6px 0 var(--amber-dark),0 14px 28px var(--amber-glow);}
.price-note{font-size:11px;color:var(--faint);text-align:center;margin-top:12px;}

/* CTA */
.cta-section{background:#0c0d10;padding:96px 48px;text-align:center;position:relative;overflow:hidden;}
.cta-section::before{content:'';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:500px;height:500px;background:radial-gradient(circle,rgba(245,158,11,.07),transparent 70%);pointer-events:none;}
@media(max-width:768px){.cta-section{padding:64px 20px;}}
.cta-h{font-family:'Bebas Neue',sans-serif;font-size:clamp(52px,8vw,100px);letter-spacing:4px;color:#fff;line-height:.92;margin-bottom:16px;position:relative;z-index:1;}
.cta-h .accent{color:var(--amber);}
.cta-sub{font-size:16px;color:rgba(255,255,255,.4);margin-bottom:36px;position:relative;z-index:1;}
.btn-cta{display:inline-block;background:var(--amber);color:#0c0d10;padding:16px 40px;border-radius:10px;font-size:15px;font-weight:700;text-decoration:none;font-family:'Manrope',sans-serif;letter-spacing:.5px;box-shadow:0 6px 0 var(--amber-dark),0 12px 36px var(--amber-glow);transition:all .2s;position:relative;z-index:1;}
.btn-cta:hover{transform:translateY(-3px);box-shadow:0 9px 0 var(--amber-dark),0 20px 44px var(--amber-glow);}
.btn-cta:active{transform:translateY(1px);box-shadow:0 2px 0 var(--amber-dark);}

/* FOOTER */
footer{background:#0c0d10;padding:36px 48px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px;border-top:1px solid rgba(255,255,255,.06);}
.footer-logo{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:2px;color:#fff;display:flex;align-items:center;gap:10px;}
.footer-note{font-size:12px;color:rgba(255,255,255,.2);}
@media(max-width:480px){footer{padding:28px 20px;}}

/* SCROLL REVEAL */
.reveal{opacity:0;transform:translateY(32px);transition:opacity .7s cubic-bezier(.16,1,.3,1),transform .7s cubic-bezier(.16,1,.3,1);}
.reveal.visible{opacity:1;transform:translateY(0);}
.reveal-d1{transition-delay:.1s;}.reveal-d2{transition-delay:.2s;}.reveal-d3{transition-delay:.3s;}
.reveal-d4{transition-delay:.4s;}.reveal-d5{transition-delay:.5s;}

/* HERO ANIMATIONS */
.hero-badge{animation:fadeUp .6s ease .05s both;}
h1{animation:fadeUp .7s cubic-bezier(.16,1,.3,1) .15s both;}
.hero-sub{animation:fadeUp .7s ease .25s both;}
.hero-actions{animation:fadeUp .7s ease .35s both;}
.hero-note{animation:fadeUp .7s ease .45s both;}
.hero-right{animation:fadeUp .8s ease .2s both;}
@keyframes fadeUp{from{opacity:0;transform:translateY(24px);}to{opacity:1;transform:translateY(0);}}

/* DESKTOP cursor */
@media(pointer:fine){
  body{cursor:none;}
  #n2q-cursor{position:fixed;width:10px;height:10px;background:var(--amber);border-radius:50%;pointer-events:none;z-index:9999;transform:translate(-50%,-50%);transition:width .2s,height .2s;}
  #n2q-ring{position:fixed;width:36px;height:36px;border:1.5px solid var(--amber);border-radius:50%;pointer-events:none;z-index:9998;transform:translate(-50%,-50%);opacity:.5;transition:width .2s,height .2s;}
}
</style>
</head>
<body>

<!-- SVG Logo Symbol -->
<svg style="display:none" aria-hidden="true">
  <symbol id="n2q" viewBox="0 0 40 40">
    <path d="M20 5h10l9 9v18a2 2 0 01-2 2H20a2 2 0 01-2-2V7a2 2 0 012-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M30 5v9h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="23" y1="20" x2="35" y2="20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <line x1="23" y1="25" x2="31" y2="25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <rect x="3" y="11" width="10" height="14" rx="5" fill="#f59e0b"/>
    <path d="M1 21.5a7 7 0 0014 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="8" y1="28.5" x2="8" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="5" y1="33" x2="11" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  </symbol>
</svg>

<!-- Desktop cursor (hidden on mobile via CSS) -->
<div id="n2q-cursor"></div>
<div id="n2q-ring"></div>

<!-- NAV -->
<nav>
  <a href="/" class="nav-logo">
    <svg width="26" height="26" viewBox="0 0 40 40"><use href="#n2q" color="currentColor"/></svg>
    Note2Quote
  </a>
  <ul class="nav-links">
    <li><a href="#how">How it works</a></li>
    <li><a href="#features">Features</a></li>
    <li><a href="#pricing">Pricing</a></li>
  </ul>
  <div class="nav-right">
    <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn">🌙</button>
    <a href="/signup" class="nav-cta">Start free trial →</a>
  </div>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-orb"></div>
  <div class="hero-inner">
    <div>
      <div class="hero-badge"><span class="badge-dot"></span> ⚡ Built for UK Subcontractors</div>
      <h1>FROM A<br>VOICE<br>NOTE TO A<br><span class="accent">POLISHED</span><br><span class="accent">PDF.</span><br>INSTANTLY.</h1>
      <p class="hero-sub">Your site chat, automatically converted into professional quotes, invoices, and variation orders. Zero logging. Zero desk time.</p>
      <div class="hero-actions">
        <a href="/signup" class="btn-3d btn-3d-primary">Start 14-day free trial ↗</a>
        <a href="#how" class="btn-3d btn-3d-secondary">See how it works</a>
      </div>
      <p class="hero-note">No app · No contract · Cancel anytime</p>
    </div>
    <div class="hero-right">
      <div class="phone-scene" id="phone-scene">
        <div class="phone-3d" id="phone-3d">
          <div class="phone-notch"></div>
          <div class="phone-screen">
            <div class="wa-bar">
              <div class="wa-av"><svg width="13" height="13" viewBox="0 0 40 40"><use href="#n2q" color="#0c0d10"/></svg></div>
              <div><div class="wa-n">Note2Quote</div><div class="wa-s">● online</div></div>
            </div>
            <div class="chat-area">
              <div class="msg out"><div class="bub out">🎙 Voice note 0:09</div><div class="bub-time">15:23</div></div>
              <div class="msg"><div class="bub in">✅ Extra consumer unit · garage · 4hrs · £120 mats<br><br>📍 Brookfield Site</div><div class="bub-time">15:23</div></div>
              <div class="msg out"><div class="bub out">Generate variations Brookfield</div><div class="bub-time">17:30</div></div>
              <div class="msg"><div class="bub in">📄 <strong>VO-003</strong> · 4 items · £340 · Ready ✅</div><div class="bub-time">17:30</div></div>
            </div>
          </div>
        </div>
      </div>
      <div class="float-card fc1">
        <div class="fc-label">Saved this week</div>
        <div class="fc-val">£1,240</div>
        <div class="fc-sub">3 variation orders</div>
      </div>
      <div class="float-card fc2">
        <div class="fc-label">Time saved</div>
        <div class="fc-val">5.2HRS</div>
        <div class="fc-sub">This week</div>
      </div>
    </div>
  </div>
</section>

<!-- MARQUEE -->
<div class="marquee-wrap">
  <div class="marquee-track">
    <div class="marquee-item"><span class="marquee-dot"></span>Variation Orders</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Daywork Sheets</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Purchase Orders</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Voice Notes</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Branded PDFs</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Multi-Site Tracking</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Client Emails</div>
    <div class="marquee-item"><span class="marquee-dot"></span>WhatsApp Native</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Instant Quotes</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Variation Orders</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Daywork Sheets</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Purchase Orders</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Voice Notes</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Branded PDFs</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Multi-Site Tracking</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Client Emails</div>
    <div class="marquee-item"><span class="marquee-dot"></span>WhatsApp Native</div>
    <div class="marquee-item"><span class="marquee-dot"></span>Instant Quotes</div>
  </div>
</div>

<!-- STATS -->
<section class="stats-section">
  <div class="stats-grid">
    <div class="stat-item reveal"><div class="stat-num" data-target="4800">£4.8K</div><div class="stat-label">Average lost per year on forgotten site extras</div></div>
    <div class="stat-item reveal reveal-d1"><div class="stat-num" data-target="5">5HRS</div><div class="stat-label">Admin time saved every single week</div></div>
    <div class="stat-item reveal reveal-d2"><div class="stat-num" data-target="10">10SEC</div><div class="stat-label">To log anything from site via WhatsApp</div></div>
    <div class="stat-item reveal reveal-d3"><div class="stat-num" data-target="49">£49</div><div class="stat-label">Per month — less than 2 hours labour</div></div>
  </div>
</section>

<!-- PROBLEM -->
<section class="section section-alt">
  <div class="section-inner">
    <span class="eyebrow reveal">The Problem</span>
    <h2 class="sh reveal">Sound familiar?</h2>
    <p class="sl reveal">Every subcontractor knows the feeling. You do the extra work. You forget to log it. The invoice goes out short.</p>
    <div class="problems">
      <div class="problem-card reveal"><div class="problem-num">01</div><h3>Forgotten variations</h3><p>Site manager asks for something extra. By the time you're home writing invoices, it's gone from your head.</p></div>
      <div class="problem-card reveal reveal-d2"><div class="problem-num">02</div><h3>Evening admin nightmare</h3><p>Hours every evening on paperwork. Invoices, daywork sheets, purchase orders — all built from scratch after a full day's graft.</p></div>
      <div class="problem-card reveal reveal-d3"><div class="problem-num">03</div><h3>Material order chaos</h3><p>Workers ask for materials on site, you say you'll sort it. Half the time it slips. Job stops. Everyone waits. Money lost.</p></div>
    </div>
  </div>
</section>

<!-- HOW -->
<section class="section section-dark" id="how">
  <div class="section-inner">
    <span class="eyebrow reveal">How It Works</span>
    <h2 class="sh sh-inv reveal">Simple as sending a text</h2>
    <p class="sl sl-inv reveal">No app to learn. No forms to fill. Just WhatsApp — the tool already in your pocket on site.</p>
    <div class="steps">
      <div class="step reveal"><div class="step-num">Step 01</div><span class="step-icon">🎙</span><h3>Voice or text</h3><p>Send a message to Note2Quote on WhatsApp. Speak naturally — exactly like you'd tell a mate on site.</p></div>
      <div class="step reveal reveal-d1"><div class="step-num">Step 02</div><span class="step-icon">🧠</span><h3>AI classifies it</h3><p>Note2Quote reads your message, works out the type and saves it tagged to the right site automatically.</p></div>
      <div class="step reveal reveal-d2"><div class="step-num">Step 03</div><span class="step-icon">📄</span><h3>PDF generated</h3><p>Say "generate variations for Site X." Branded PDF in seconds — ready to download or send.</p></div>
      <div class="step reveal reveal-d3"><div class="step-num">Step 04</div><span class="step-icon">✉️</span><h3>Send to client</h3><p>Review on your dashboard, send to your client. Done in under a minute.</p></div>
    </div>
  </div>
</section>

<!-- FEATURES -->
<section class="section" id="features">
  <div class="section-inner">
    <span class="eyebrow reveal">Features</span>
    <h2 class="sh reveal">Everything you need,<br>nothing you don't</h2>
    <p class="sl reveal">Built for the way tradespeople actually work — on site, hands dirty, no time for admin.</p>
    <div class="features">
      <div class="feature reveal"><div class="feature-shine"></div><div class="feature-icon">🎙</div><h3>Voice Notes</h3><p>Send a voice note and we transcribe it automatically. Hands dirty, no typing needed.</p></div>
      <div class="feature reveal reveal-d1"><div class="feature-shine"></div><div class="feature-icon">💬</div><h3>WhatsApp Native</h3><p>No new app. Works on any phone. Your whole team logs from day one with zero training.</p></div>
      <div class="feature reveal reveal-d2"><div class="feature-shine"></div><div class="feature-icon">🎨</div><h3>Fully Branded</h3><p>Your logo, colours, address, VAT on every document. Looks professional every time.</p></div>
      <div class="feature reveal reveal-d1"><div class="feature-shine"></div><div class="feature-icon">📍</div><h3>Multi-Site</h3><p>Every log tagged to the right client and site. No mixing, no confusion, no lost invoices.</p></div>
      <div class="feature reveal reveal-d2"><div class="feature-shine"></div><div class="feature-icon">📊</div><h3>Boss Dashboard</h3><p>Edit entries, generate PDFs, download or email directly to clients in one click.</p></div>
      <div class="feature reveal reveal-d3"><div class="feature-shine"></div><div class="feature-icon">📦</div><h3>Bulk Logging</h3><p>Send a numbered list and we log every item at once. End of day dump in one message.</p></div>
    </div>
  </div>
</section>

<!-- TESTIMONIAL -->
<section class="testimonial-section">
  <div class="t-inner reveal">
    <div class="t-stars">⭐⭐⭐⭐⭐</div>
    <p class="t-quote">"I USED TO SPEND THREE HOURS EVERY SUNDAY DOING PAPERWORK. NOW IT TAKES TEN MINUTES."</p>
    <p class="t-author">Beta tester · Electrical Subcontractor · Essex</p>
  </div>
</section>

<!-- PRICING -->
<section class="section" id="pricing">
  <div class="section-inner">
    <span class="eyebrow reveal" style="display:block;text-align:center">Pricing</span>
    <h2 class="sh reveal" style="text-align:center;margin-bottom:8px">One price.<br>Everything included.</h2>
    <p class="sl reveal" style="text-align:center;margin:0 auto 44px">No setup fees. No per-document charges. No surprises.</p>
    <div class="price-wrap reveal">
      <div class="price-card">
        <div class="price-top">
          <div class="price-tag">Most popular</div>
          <div class="price-amount"><sup>£</sup>49</div>
          <div class="price-period">per month, billed monthly</div>
          <div class="price-trial">✓ 14 days free — no card charged upfront</div>
        </div>
        <div class="price-bottom">
          <div class="price-feature"><span class="check">✓</span>Unlimited WhatsApp logging</div>
          <div class="price-feature"><span class="check">✓</span>Voice note transcription</div>
          <div class="price-feature"><span class="check">✓</span>Variation orders, dayworks & purchase orders</div>
          <div class="price-feature"><span class="check">✓</span>Branded PDFs with your logo</div>
          <div class="price-feature"><span class="check">✓</span>Multi-site tracking</div>
          <div class="price-feature"><span class="check">✓</span>Boss dashboard with inline editing</div>
          <div class="price-feature"><span class="check">✓</span>Direct client email sending</div>
          <div class="price-feature"><span class="check">✓</span>Unlimited team members</div>
          <a href="/signup" class="btn-pricing">Start 14-day free trial →</a>
          <p class="price-note">Cancel anytime. No contract. No setup fee.</p>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- CTA -->
<section class="cta-section">
  <h2 class="cta-h reveal">Stop losing<br><span class="accent">money.</span></h2>
  <p class="cta-sub reveal">Join subcontractors across the UK saving hours every week.</p>
  <a href="/signup" class="btn-cta reveal">Start your free trial →</a>
</section>

<!-- FOOTER -->
<footer>
  <div class="footer-logo"><svg width="22" height="22" viewBox="0 0 40 40"><use href="#n2q" color="white"/></svg>Note2Quote</div>
  <p class="footer-note">© 2026 Note2Quote · Built for UK Subcontractors</p>
</footer>

<script>
/* Theme */
function toggleTheme(){
  var h=document.documentElement,b=document.getElementById('theme-btn');
  var n=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',n);
  b.textContent=n==='dark'?'☀️':'🌙';
  localStorage.setItem('n2q-theme',n);
}
var saved=localStorage.getItem('n2q-theme');
if(saved){document.documentElement.setAttribute('data-theme',saved);var tb=document.getElementById('theme-btn');if(tb)tb.textContent=saved==='dark'?'☀️':'🌙';}

/* Scroll reveal */
if('IntersectionObserver' in window){
  var io=new IntersectionObserver(function(entries){entries.forEach(function(e){if(e.isIntersecting)e.target.classList.add('visible');});},{threshold:.1,rootMargin:'0px 0px -40px 0px'});
  document.querySelectorAll('.reveal').forEach(function(el){io.observe(el);});
}else{
  document.querySelectorAll('.reveal').forEach(function(el){el.classList.add('visible');});
}

/* Desktop-only effects */
if(window.matchMedia('(pointer:fine)').matches){
  /* Cursor */
  var cur=document.getElementById('n2q-cursor'),ring=document.getElementById('n2q-ring');
  var mx=0,my=0,rx=0,ry=0;
  document.addEventListener('mousemove',function(e){mx=e.clientX;my=e.clientY;cur.style.left=mx+'px';cur.style.top=my+'px';});
  (function animR(){rx+=(mx-rx)*.12;ry+=(my-ry)*.12;ring.style.left=rx+'px';ring.style.top=ry+'px';requestAnimationFrame(animR);})();

  /* 3D phone tilt */
  var phone=document.getElementById('phone-3d'),scene=document.getElementById('phone-scene');
  if(scene&&phone){
    scene.addEventListener('mousemove',function(e){var r=scene.getBoundingClientRect();var dx=(e.clientX-r.left-r.width/2)/(r.width/2);var dy=(e.clientY-r.top-r.height/2)/(r.height/2);phone.style.transform='rotateX('+(-dy*12)+'deg) rotateY('+(dx*16)+'deg)';});
    scene.addEventListener('mouseleave',function(){phone.style.transform='rotateX(4deg) rotateY(-12deg)';});
  }

  /* Magnetic buttons */
  document.querySelectorAll('.btn-3d').forEach(function(btn){
    btn.addEventListener('mousemove',function(e){var r=btn.getBoundingClientRect();var dx=e.clientX-(r.left+r.width/2);var dy=e.clientY-(r.top+r.height/2);btn.style.transform='translate('+(dx*.15)+'px,'+(dy*.15)+'px)';});
    btn.addEventListener('mouseleave',function(){btn.style.transform='';});
  });
}
</script>
</body>
</html>
'''
SIGNUP_HTML    = '''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Start Free Trial — Note2Quote</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script>var _t=localStorage.getItem('n2q-theme');if(_t)document.documentElement.setAttribute('data-theme',_t);</script>
<style>
:root{--amber:#f59e0b;--amber-dark:#d97706;--amber-light:#fbbf24;--amber-dim:rgba(245,158,11,0.12);--amber-glow:rgba(245,158,11,0.25);}
[data-theme="light"]{--bg:#f5f3ef;--bg2:#ffffff;--bg3:#eceae5;--text:#0c0d10;--text2:#2d2f36;--muted:#6b7280;--faint:#a0a5b0;--border:#dddad4;--border2:#c8c4bc;--card:#ffffff;--nav:rgba(245,243,239,0.97);--shadow:rgba(0,0,0,0.06);--shadow2:rgba(0,0,0,0.12);--green:#16a34a;}
[data-theme="dark"]{--bg:#0c0d10;--bg2:#131419;--bg3:#1a1b22;--text:#f0ede8;--text2:#c8c4bc;--muted:#8b8fa8;--faint:#4a4d5e;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);--card:#131419;--nav:rgba(12,13,16,0.97);--shadow:rgba(0,0,0,0.4);--shadow2:rgba(0,0,0,0.6);--green:#22c55e;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Manrope',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased;transition:background .3s,color .3s;}

/* NAV */
nav{background:var(--nav);border-bottom:1px solid var(--border);padding:0 48px;height:68px;display:flex;align-items:center;justify-content:space-between;backdrop-filter:blur(20px);position:sticky;top:0;z-index:100;transition:background .3s,border-color .3s;}
.nav-logo{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:3px;color:var(--text);display:flex;align-items:center;gap:10px;text-decoration:none;transition:color .3s;}
.nav-right{display:flex;align-items:center;gap:10px;}
.nav-note{font-size:12px;color:var(--muted);font-weight:500;}
.theme-toggle{width:34px;height:34px;border-radius:8px;border:1px solid var(--border);background:var(--card);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;transition:all .2s;}
.theme-toggle:hover{border-color:var(--amber);}
@media(max-width:600px){nav{padding:0 20px;}.nav-note{display:none;}}

/* LAYOUT */
main{max-width:1080px;margin:0 auto;padding:48px 24px;display:grid;grid-template-columns:1fr 340px;gap:52px;align-items:start;}
@media(max-width:900px){main{grid-template-columns:1fr;}.side-info{display:none;}}
@media(max-width:600px){main{padding:24px 16px;}}

/* FORM CARD */
.form-card{background:var(--card);border:1px solid var(--border);border-radius:20px;overflow:hidden;box-shadow:0 24px 48px var(--shadow);transition:background .3s,border-color .3s,box-shadow .3s;animation:slideUp .6s cubic-bezier(.16,1,.3,1) both;}
@keyframes slideUp{from{opacity:0;transform:translateY(32px);}to{opacity:1;transform:translateY(0);}}

.form-header{background:linear-gradient(135deg,#0c0d10,#1a1b22);padding:36px 40px;position:relative;overflow:hidden;}
.form-header::after{content:'';position:absolute;top:-40%;right:-20%;width:280px;height:280px;background:radial-gradient(circle,rgba(245,158,11,0.12),transparent 65%);pointer-events:none;}
.form-header h1{font-family:'Bebas Neue',sans-serif;font-size:40px;letter-spacing:3px;color:#ffffff;margin-bottom:6px;position:relative;z-index:1;}
.form-header p{font-size:14px;color:rgba(255,255,255,0.45);font-weight:300;position:relative;z-index:1;}
.trial-pill{display:inline-flex;align-items:center;gap:8px;background:rgba(34,197,94,0.12);border:1px solid rgba(34,197,94,0.25);padding:6px 14px;border-radius:100px;font-size:12px;font-weight:700;color:var(--green);margin-top:14px;position:relative;z-index:1;}
.trial-dot{width:6px;height:6px;background:var(--green);border-radius:50%;animation:blink 2s ease infinite;}
@keyframes blink{0%,100%{opacity:1;}50%{opacity:.2;}}

.form-body{padding:32px 40px;}
@media(max-width:600px){.form-header{padding:28px 24px;}.form-body{padding:24px 20px;}}

.section-label{font-size:10px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:var(--amber);margin:28px 0 14px;display:flex;align-items:center;gap:10px;}
.section-label::after{content:'';flex:1;height:1px;background:var(--border);}
.section-label:first-child{margin-top:0;}

.form-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
@media(max-width:560px){.form-row{grid-template-columns:1fr;}}
.form-group{margin-bottom:14px;}
.form-group label{display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:6px;letter-spacing:0.5px;text-transform:uppercase;}
.form-group input,.form-group select{
  width:100%;padding:11px 14px;
  border:1.5px solid var(--border);border-radius:10px;
  font-size:14px;font-family:'Manrope',sans-serif;
  outline:none;transition:border-color .15s,box-shadow .15s;
  background:var(--bg3);color:var(--text);
}
.form-group input:focus,.form-group select:focus{border-color:var(--amber);box-shadow:0 0 0 3px var(--amber-dim);}
.form-group select option{background:var(--bg3);}
.hint{font-size:11px;color:var(--faint);margin-top:5px;}
.color-row{display:flex;gap:10px;align-items:center;}
.color-row input[type="color"]{width:44px;height:42px;padding:2px 3px;border-radius:10px;cursor:pointer;flex-shrink:0;border:1.5px solid var(--border);background:var(--bg3);}
hr{border:none;border-top:1px solid var(--border);margin:28px 0;}

.btn-submit{
  width:100%;background:var(--amber);color:#0c0d10;
  border:none;padding:15px;border-radius:10px;
  font-size:15px;font-weight:700;font-family:'Manrope',sans-serif;
  cursor:pointer;letter-spacing:.3px;
  box-shadow:0 4px 0 var(--amber-dark),0 8px 20px var(--amber-glow);
  transition:all .15s;
}
.btn-submit:hover{transform:translateY(-2px);box-shadow:0 6px 0 var(--amber-dark),0 14px 28px var(--amber-glow);}
.btn-submit:active{transform:translateY(1px);box-shadow:0 2px 0 var(--amber-dark);}
.btn-submit:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none;}
.terms{font-size:11px;color:var(--faint);text-align:center;margin-top:12px;line-height:1.6;}
.error-msg{color:#ef4444;font-size:13px;margin-top:10px;display:none;font-weight:500;}

/* SIDE INFO */
.side-info{padding-top:4px;animation:slideUp .7s cubic-bezier(.16,1,.3,1) .1s both;}
.side-title{font-family:'Bebas Neue',sans-serif;font-size:32px;letter-spacing:2px;color:var(--text);margin-bottom:24px;line-height:.95;transition:color .3s;}

.benefit{display:flex;gap:16px;padding:18px 0;border-bottom:1px solid var(--border);transition:border-color .3s;animation:fadeIn .5s ease both;}
.benefit:last-of-type{border-bottom:none;}
.benefit-icon{
  width:42px;height:42px;
  background:var(--amber-dim);border:1px solid rgba(245,158,11,0.2);
  border-radius:10px;display:flex;align-items:center;justify-content:center;
  font-size:18px;flex-shrink:0;
  transition:transform .3s cubic-bezier(.34,1.56,.64,1);
}
.benefit:hover .benefit-icon{transform:scale(1.1) rotate(-5deg);}
.benefit h4{font-size:14px;font-weight:700;color:var(--text);margin-bottom:3px;transition:color .3s;}
.benefit p{font-size:13px;color:var(--muted);line-height:1.6;font-weight:400;}

.testimonial-card{
  background:linear-gradient(135deg,#0c0d10,#1a1b22);
  border-radius:16px;padding:28px;margin-top:24px;
  position:relative;overflow:hidden;
  animation:slideUp .7s cubic-bezier(.16,1,.3,1) .2s both;
}
.testimonial-card::before{content:'"';font-family:'Bebas Neue',sans-serif;font-size:120px;color:rgba(245,158,11,0.1);position:absolute;top:-10px;left:10px;line-height:1;}
.testimonial-card p{font-family:'Bebas Neue',sans-serif;font-size:18px;letter-spacing:1px;color:#ffffff;line-height:1.3;margin-bottom:14px;position:relative;z-index:1;}
.testimonial-card cite{font-size:11px;color:rgba(255,255,255,.35);font-style:normal;letter-spacing:.5px;}
.t-stars{font-size:14px;letter-spacing:3px;margin-bottom:12px;}

@keyframes fadeIn{from{opacity:0;}to{opacity:1;}}
</style>
</head>
<body>

<svg style="display:none" aria-hidden="true">
  <symbol id="n2q" viewBox="0 0 40 40">
    <path d="M20 5h10l9 9v18a2 2 0 01-2 2H20a2 2 0 01-2-2V7a2 2 0 012-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M30 5v9h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="23" y1="20" x2="35" y2="20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <line x1="23" y1="25" x2="31" y2="25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <rect x="3" y="11" width="10" height="14" rx="5" fill="#f59e0b"/>
    <path d="M1 21.5a7 7 0 0014 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="8" y1="28.5" x2="8" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="5" y1="33" x2="11" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  </symbol>
</svg>

<nav>
  <a href="/" class="nav-logo">
    <svg width="24" height="24" viewBox="0 0 40 40"><use href="#n2q" color="currentColor"/></svg>
    Note2Quote
  </a>
  <div class="nav-right">
    <span class="nav-note">14-day free trial · No card charged upfront</span>
    <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn">🌙</button>
  </div>
</nav>

<main>
  <div class="form-card">
    <div class="form-header">
      <h1>Start your free trial</h1>
      <p>Set up takes 2 minutes. No card charged for 14 days.</p>
      <div class="trial-pill"><span class="trial-dot"></span>14 days free, then £49/month. Cancel anytime.</div>
    </div>
    <div class="form-body">

      <div class="section-label">Your Details</div>
      <div class="form-row">
        <div class="form-group"><label>Your Name</label><input type="text" id="owner-name" placeholder="John Smith" required /></div>
        <div class="form-group"><label>Email Address</label><input type="email" id="email" placeholder="john@smithelectrical.co.uk" required /></div>
      </div>

      <div class="section-label">Your Company</div>
      <div class="form-row">
        <div class="form-group"><label>Company Name</label><input type="text" id="company-name" placeholder="Smith Electrical Ltd" required /></div>
        <div class="form-group">
          <label>Trade</label>
          <select id="trade" required>
            <option value="">Select your trade...</option>
            <option>Electrical</option><option>Plumbing</option>
            <option>Carpentry / Joinery</option><option>Plastering</option>
            <option>Bricklaying</option><option>Roofing</option>
            <option>Painting & Decorating</option><option>HVAC / Gas</option>
            <option>Groundworks</option><option>General Building</option>
            <option>Other</option>
          </select>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>VAT Number <span style="font-weight:300;opacity:.5;text-transform:none">(optional)</span></label><input type="text" id="vat" placeholder="GB123456789" /></div>
        <div class="form-group"><label>Company Address</label><input type="text" id="address" placeholder="123 High Street, London" /></div>
      </div>

      <div class="section-label">WhatsApp & Branding</div>
      <div class="form-row">
        <div class="form-group">
          <label>WhatsApp Number</label>
          <input type="tel" id="whatsapp" placeholder="+447700900000" required />
          <div class="hint">The number your team will message the bot from</div>
        </div>
        <div class="form-group">
          <label>Brand Colour</label>
          <div class="color-row">
            <input type="color" id="color-picker" value="#f59e0b" oninput="document.getElementById('color-hex').value=this.value" />
            <input type="text" id="color-hex" value="#f59e0b" oninput="document.getElementById('color-picker').value=this.value" />
          </div>
          <div class="hint">Used on all your branded documents</div>
        </div>
      </div>

      <div class="form-group" style="margin-top:4px">
        <label>Company Logo <span style="font-weight:300;opacity:.5;text-transform:none">(optional — appears on all PDFs)</span></label>
        <div id="logo-drop" onclick="document.getElementById('logo-file').click()" style="border:2px dashed var(--border);border-radius:10px;padding:20px;text-align:center;cursor:pointer;transition:all .2s;background:var(--bg3)">
          <div id="logo-preview" style="display:none;margin-bottom:8px"><img id="logo-img" style="max-height:48px;max-width:160px;border-radius:6px" /></div>
          <div id="logo-placeholder"><div style="font-size:22px;margin-bottom:4px">🖼️</div><div style="font-size:12px;color:var(--muted)">Click to upload your logo</div><div style="font-size:10px;color:var(--faint);margin-top:2px">PNG, JPG or SVG · Max 2MB</div></div>
        </div>
        <input type="file" id="logo-file" accept="image/*" style="display:none" onchange="previewLogo(this)" />
      </div>
      <hr>
      <button class="btn-submit" onclick="handleSignup()" id="submit-btn">Continue to payment →</button>
      <div class="error-msg" id="error-msg"></div>
      <p class="terms">By signing up you agree to our terms of service.<br>Your card will not be charged during the 14-day free trial.</p>
    </div>
  </div>

  <div class="side-info">
    <h2 class="side-title">What you get from day one</h2>

    <div class="benefit"><div class="benefit-icon">🎙</div><div><h4>Voice note logging</h4><p>Send a voice note and we transcribe it instantly. No typing on site.</p></div></div>
    <div class="benefit"><div class="benefit-icon">📄</div><div><h4>Branded PDFs</h4><p>Your logo and colours on every variation order, daywork sheet and purchase order.</p></div></div>
    <div class="benefit"><div class="benefit-icon">📊</div><div><h4>Boss dashboard</h4><p>Edit entries, generate and send documents in one click from any device.</p></div></div>
    <div class="benefit"><div class="benefit-icon">📍</div><div><h4>Multi-site tracking</h4><p>Every log tagged to the right client and site automatically.</p></div></div>
    <div class="benefit"><div class="benefit-icon">🔒</div><div><h4>Completely private</h4><p>Your data is yours. Each company is completely separate and secure.</p></div></div>

    <div class="testimonial-card">
      <div class="t-stars">⭐⭐⭐⭐⭐</div>
      <p>"Used to spend three hours every Sunday doing paperwork. Now ten minutes."</p>
      <cite>Beta tester · Electrical subcontractor · Essex</cite>
    </div>
  </div>
</main>

<script>
function previewLogo(input){
  var file=input.files[0];if(!file)return;
  if(file.size>2*1024*1024){alert('Logo too large. Max 2MB.');return;}
  var reader=new FileReader();
  reader.onload=function(e){
    document.getElementById('logo-img').src=e.target.result;
    document.getElementById('logo-preview').style.display='block';
    document.getElementById('logo-placeholder').style.display='none';
    document.getElementById('logo-drop').style.borderColor='var(--amber)';
  };
  reader.readAsDataURL(file);
}

function toggleTheme(){
  var h=document.documentElement,b=document.getElementById('theme-btn');
  var n=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',n);b.textContent=n==='dark'?'☀️':'🌙';
  localStorage.setItem('n2q-theme',n);
}
var saved=localStorage.getItem('n2q-theme');
if(saved){var tb=document.getElementById('theme-btn');if(tb)tb.textContent=saved==='dark'?'☀️':'🌙';}

function previewLogo(input){
  var file=input.files[0];if(!file)return;
  if(file.size>2*1024*1024){alert('Logo too large. Max 2MB.');return;}
  var reader=new FileReader();
  reader.onload=function(e){
    document.getElementById('logo-img').src=e.target.result;
    document.getElementById('logo-preview').style.display='block';
    document.getElementById('logo-placeholder').style.display='none';
    document.getElementById('logo-drop').style.borderColor='var(--amber)';
  };
  reader.readAsDataURL(file);
}

async function handleSignup(){
  var btn=document.getElementById('submit-btn');
  var errEl=document.getElementById('error-msg');
  errEl.style.display='none';
  var name=document.getElementById('owner-name').value.trim();
  var email=document.getElementById('email').value.trim();
  var company=document.getElementById('company-name').value.trim();
  var trade=document.getElementById('trade').value;
  var whatsapp=document.getElementById('whatsapp').value.trim();
  var vat=document.getElementById('vat').value.trim();
  var address=document.getElementById('address').value.trim();
  var color=document.getElementById('color-hex').value.trim();
  if(!name||!email||!company||!trade||!whatsapp){errEl.textContent='Please fill in all required fields.';errEl.style.display='block';return;}
  btn.disabled=true;btn.textContent='Setting up your account...';
  try{
    var r=await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,email,company_name:company,trade,whatsapp,vat,address,primary_color:color})});
    var d=await r.json();
    if(d.checkout_url){
      // Upload logo if provided
      var logoFile=document.getElementById('logo-file').files[0];
      if(logoFile&&d.company_whatsapp){
        try{
          var fd=new FormData();fd.append('logo',logoFile);
          await fetch('/api/signup/logo?whatsapp='+encodeURIComponent(d.company_whatsapp),{method:'POST',body:fd});
        }catch(e){console.warn('Logo upload failed',e);}
      }
      window.location.href=d.checkout_url;
    }
    else{throw new Error(d.error||'Something went wrong');}
  }catch(e){errEl.textContent=e.message;errEl.style.display='block';btn.disabled=false;btn.textContent='Continue to payment →';}
}
</script>
</body>
</html>
'''
WELCOME_HTML   = '''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script>
const _t=localStorage.getItem('n2q-theme');
if(_t){document.documentElement.setAttribute('data-theme',_t);}
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Welcome to Note2Quote!</title>

<style>
  :root { --red: #d62828; --cream: #fff3e0; --ink: #0f0a00; --muted: #8a7560; --border: #e8ddd0; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Manrope', sans-serif; background: #111111;
         min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }
  .box { background: white; border: 1px solid #222222; border-radius: 24px;
         padding: 56px 48px; text-align: center; max-width: 480px;
         box-shadow: 0 24px 48px rgba(0,0,0,0.06); }
  .emoji { font-size: 56px; margin-bottom: 20px; }
  h1 { font-family: 'Bebas Neue', sans-serif; font-size: 36px; font-weight: 900;
       color: #ffffff; letter-spacing: -1px; margin-bottom: 12px; }
  .highlight { color: #f59e0b; font-weight: 600; font-size: 17px; margin-bottom: 16px; }
  p { color: #888888; line-height: 1.7; font-weight: 300; margin-bottom: 12px; }
  a { display: inline-block; background: #f59e0b; color: white;
      padding: 14px 28px; border-radius: 10px; text-decoration: none;
      font-weight: 600; font-size: 15px; margin-top: 16px; transition: all 0.2s; }
  a:hover { background: #b01f1f; transform: translateY(-1px); }
</style>
</head>
<body>
<svg style="display:none">
  <symbol id="n2q-logo" viewBox="0 0 40 40">
    <path d="M20 5h10l9 9v18a2 2 0 01-2 2H20a2 2 0 01-2-2V7a2 2 0 012-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M30 5v9h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="23" y1="20" x2="35" y2="20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <line x1="23" y1="25" x2="31" y2="25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <rect x="3" y="11" width="10" height="14" rx="5" fill="#f59e0b"/>
    <path d="M1 21.5a7 7 0 0014 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="8" y1="28.5" x2="8" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="5" y1="33" x2="11" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  </symbol>
</svg>
<div class="box">
  <div class="emoji">🎉</div>
  <h1>You're all set!</h1>
  <p class="highlight">Check WhatsApp — your bot is ready.</p>
  <p>We've sent you a welcome message with everything you need to get started. Log your first variation in the next 60 seconds.</p>
  <a href="/dashboard">Open Dashboard →</a>
</div>
</body>
</html>
'''
DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — Note2Quote</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script>var _t=localStorage.getItem('n2q-theme');if(_t)document.documentElement.setAttribute('data-theme',_t);</script>
<style>
:root{--amber:#f59e0b;--amber-d:#d97706;--amber-l:#fbbf24;--amber-dim:rgba(245,158,11,0.12);--amber-g:rgba(245,158,11,0.2);}
[data-theme="light"]{--bg:#f5f3ef;--bg2:#fff;--bg3:#eceae5;--text:#0c0d10;--text2:#374151;--muted:#6b7280;--faint:#a0a5b0;--border:#dddad4;--card:#fff;--nav-bg:#0c0d10;--nav-text:rgba(255,255,255,0.7);--nav-active:#fff;--shadow:rgba(0,0,0,0.06);--shadow2:rgba(0,0,0,0.14);--green:#16a34a;--red:#dc2626;--blue:#2563eb;--purple:#7c3aed;}
[data-theme="dark"]{--bg:#0c0d10;--bg2:#131419;--bg3:#1a1b22;--text:#f0ede8;--text2:#c8c4bc;--muted:#8b8fa8;--faint:#4a4d5e;--border:rgba(255,255,255,0.07);--card:#131419;--nav-bg:#080a0d;--nav-text:rgba(255,255,255,0.55);--nav-active:#fff;--shadow:rgba(0,0,0,0.4);--shadow2:rgba(0,0,0,0.6);--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--purple:#a78bfa;}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;}
body{font-family:'Manrope',sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;transition:background .3s,color .3s;}

/* ── LOGIN ── */
#login-screen{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);}
.login-card{background:var(--card);border:1px solid var(--border);border-radius:20px;overflow:hidden;width:100%;max-width:400px;box-shadow:0 32px 64px var(--shadow2);animation:fadeUp .6s cubic-bezier(.16,1,.3,1) both;}
@keyframes fadeUp{from{opacity:0;transform:translateY(24px);}to{opacity:1;transform:translateY(0);}}
.login-top{background:linear-gradient(135deg,#0c0d10,#1a1b22);padding:36px 36px 28px;position:relative;overflow:hidden;}
.login-top::after{content:'';position:absolute;top:-40%;right:-20%;width:220px;height:220px;background:radial-gradient(circle,rgba(245,158,11,0.14),transparent 65%);pointer-events:none;}
.login-logo{font-family:'Bebas Neue',sans-serif;font-size:26px;letter-spacing:3px;color:#fff;display:flex;align-items:center;gap:10px;margin-bottom:4px;position:relative;z-index:1;}
.login-top p{font-size:13px;color:rgba(255,255,255,.4);position:relative;z-index:1;}
.login-body{padding:28px 36px;}
.login-body input{width:100%;padding:11px 14px;border:1.5px solid var(--border);border-radius:10px;font-size:14px;font-family:'Manrope',sans-serif;outline:none;margin-bottom:10px;background:var(--bg3);color:var(--text);transition:border-color .15s,box-shadow .15s;}
.login-body input:focus{border-color:var(--amber);box-shadow:0 0 0 3px var(--amber-dim);}
.btn-login{width:100%;background:var(--amber);color:#0c0d10;border:none;padding:13px;border-radius:10px;font-size:14px;font-weight:700;font-family:'Manrope',sans-serif;cursor:pointer;box-shadow:0 4px 0 var(--amber-d);transition:all .15s;}
.btn-login:hover{transform:translateY(-1px);box-shadow:0 5px 0 var(--amber-d),0 10px 20px var(--amber-g);}
.err{color:var(--red);font-size:12px;margin-top:6px;font-weight:500;}

/* ── APP SHELL ── */
#app{display:none;height:100vh;overflow:hidden;}
.app-shell{display:flex;height:100%;}

/* ── SIDEBAR ── */
.sidebar{width:220px;flex-shrink:0;background:var(--nav-bg);display:flex;flex-direction:column;overflow-y:auto;transition:width .3s;position:relative;z-index:50;}
.sidebar-logo{padding:20px 20px 16px;border-bottom:1px solid rgba(255,255,255,0.06);}
.sidebar-logo a{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:3px;color:#fff;text-decoration:none;display:flex;align-items:center;gap:8px;}
.sidebar-company{font-size:11px;color:rgba(255,255,255,0.3);margin-top:2px;padding-left:32px;font-weight:400;}
.sidebar-nav{flex:1;padding:12px 10px;}
.nav-section{margin-bottom:20px;}
.nav-section-label{font-size:9px;font-weight:700;color:rgba(255,255,255,0.25);text-transform:uppercase;letter-spacing:2px;padding:0 10px;margin-bottom:6px;}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:9px;cursor:pointer;color:var(--nav-text);font-size:13px;font-weight:500;transition:all .15s;border:none;background:none;width:100%;text-align:left;font-family:'Manrope',sans-serif;}
.nav-item:hover{background:rgba(255,255,255,0.06);color:var(--nav-active);}
.nav-item.active{background:var(--amber);color:#0c0d10;font-weight:700;}
.nav-item.active .nav-icon{opacity:1;}
.nav-icon{font-size:16px;flex-shrink:0;opacity:.7;}
.nav-item.active .nav-icon{opacity:1;}
.sidebar-bottom{padding:12px 10px;border-top:1px solid rgba(255,255,255,0.06);}
.sidebar-user{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:9px;background:rgba(255,255,255,0.04);}
.sidebar-avatar{width:30px;height:30px;background:var(--amber);border-radius:50%;display:flex;align-items:center;justify-content:center;font-family:'Bebas Neue',sans-serif;font-size:13px;color:#0c0d10;flex-shrink:0;}
.sidebar-user-info{flex:1;min-width:0;}
.sidebar-user-name{font-size:12px;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.sidebar-user-role{font-size:10px;color:rgba(255,255,255,.3);}
.sidebar-actions{display:flex;gap:6px;padding:8px 10px 4px;}
.sidebar-btn{flex:1;padding:7px;border-radius:7px;border:1px solid rgba(255,255,255,0.1);background:none;color:rgba(255,255,255,.5);font-size:11px;font-family:'Manrope',sans-serif;font-weight:600;cursor:pointer;transition:all .15s;}
.sidebar-btn:hover{background:rgba(255,255,255,0.08);color:#fff;}

/* ── MAIN ── */
.main{flex:1;overflow-y:auto;display:flex;flex-direction:column;}
.topbar{padding:16px 28px;background:var(--card);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;flex-shrink:0;transition:background .3s,border-color .3s;}
.topbar-title{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:2px;color:var(--text);transition:color .3s;}
.topbar-right{display:flex;align-items:center;gap:8px;}
.theme-btn{width:32px;height:32px;border-radius:7px;border:1px solid var(--border);background:var(--card);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;transition:all .2s;}
.theme-btn:hover{border-color:var(--amber);}
.page-content{padding:24px 28px;flex:1;}
@media(max-width:900px){.page-content{padding:16px;}}

/* ── FILTERS BAR ── */
.filters-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:20px;}
.filter-btn{padding:6px 14px;border-radius:7px;border:1.5px solid var(--border);background:var(--card);color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;font-family:'Manrope',sans-serif;transition:all .15s;}
.filter-btn:hover,.filter-btn.active{border-color:var(--amber);color:var(--amber);background:var(--amber-dim);}
.filter-select{padding:6px 12px;border-radius:7px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:12px;font-weight:600;cursor:pointer;font-family:'Manrope',sans-serif;outline:none;}
.filter-select:focus{border-color:var(--amber);}
.date-input{padding:6px 10px;border-radius:7px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:12px;font-family:'Manrope',sans-serif;outline:none;}
.date-input:focus{border-color:var(--amber);}
.filter-sep{width:1px;height:20px;background:var(--border);flex-shrink:0;}

/* ── KPI CARDS ── */
.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px;}
@media(max-width:900px){.kpi-row{grid-template-columns:repeat(2,1fr);}}
@media(max-width:500px){.kpi-row{grid-template-columns:1fr 1fr;}}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px 20px;transition:all .2s;}
.kpi:hover{border-color:var(--amber);box-shadow:0 4px 16px var(--amber-g);}
.kpi-label{font-size:10px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;}
.kpi-value{font-family:'Bebas Neue',sans-serif;font-size:36px;letter-spacing:1px;color:var(--text);line-height:1;transition:color .3s;}
.kpi-value.amber{color:var(--amber);}
.kpi-value.green{color:var(--green);}
.kpi-value.red{color:var(--red);}
.kpi-sub{font-size:11px;color:var(--faint);margin-top:3px;}

/* ── CHARTS ── */
.charts-row{display:grid;grid-template-columns:1fr 1fr 300px;gap:16px;margin-bottom:24px;}
@media(max-width:1100px){.charts-row{grid-template-columns:1fr 1fr;}}
@media(max-width:700px){.charts-row{grid-template-columns:1fr;}}
.chart-card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;transition:background .3s,border-color .3s;}
.chart-title{font-family:'Bebas Neue',sans-serif;font-size:16px;letter-spacing:1.5px;color:var(--text);margin-bottom:14px;}
.chart-wrap{position:relative;height:200px;}

/* ── SECTION CARD ── */
.section-card{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:16px;transition:background .3s,border-color .3s;}
.section-header{padding:14px 20px;background:var(--bg3);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;transition:background .3s,border-color .3s;}
.section-title{font-family:'Bebas Neue',sans-serif;font-size:17px;letter-spacing:1.5px;color:var(--text);display:flex;align-items:center;gap:8px;}
.count-pill{background:var(--amber);color:#0c0d10;font-size:10px;font-weight:800;padding:2px 8px;border-radius:100px;}
.section-actions{display:flex;gap:6px;}

/* ── TABLE ── */
.tbl-wrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{padding:8px 14px;text-align:left;font-size:9px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1.5px;background:var(--bg3);border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none;transition:background .3s,border-color .3s,color .3s;}
th:hover{color:var(--amber);}
td{border-bottom:1px solid var(--border);transition:border-color .3s;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:var(--bg3);}
.cell{padding:9px 14px;outline:none;cursor:text;color:var(--text2);transition:background .1s;}
.cell:focus{background:var(--amber-dim);box-shadow:inset 0 0 0 1.5px var(--amber);}
.cell-pad{padding:9px 14px;color:var(--text2);}

/* STATUS BADGES */
.status-sel{border:none;background:transparent;font-size:11px;font-weight:700;cursor:pointer;font-family:'Manrope',sans-serif;padding:3px 8px;border-radius:6px;outline:none;}
.s-pending{background:var(--amber-dim);color:var(--amber-d);}
.s-approved{background:rgba(34,197,94,0.12);color:var(--green);}
.s-cancelled{background:rgba(220,38,38,0.1);color:var(--red);}
.s-chasing{background:rgba(99,102,241,0.1);color:var(--purple);}

/* TYPE PILLS */
.type-pill{display:inline-block;font-size:9px;font-weight:700;padding:2px 7px;border-radius:5px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;}
.t-VARIATION{background:var(--amber-dim);color:var(--amber-d);}
.t-DAYWORK{background:rgba(34,197,94,0.1);color:var(--green);}
.t-MATERIAL_ORDER{background:rgba(99,102,241,0.1);color:var(--purple);}
.t-TIMESHEET{background:rgba(236,72,153,0.1);color:#ec4899;}
.t-UNKNOWN{background:var(--bg3);color:var(--faint);}

/* ── BUTTONS ── */
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:8px;font-size:12px;font-weight:700;border:none;cursor:pointer;font-family:'Manrope',sans-serif;transition:all .15s;letter-spacing:.3px;}
.btn-amber{background:var(--amber);color:#0c0d10;box-shadow:0 2px 0 var(--amber-d);}
.btn-amber:hover{transform:translateY(-1px);box-shadow:0 3px 0 var(--amber-d),0 6px 14px var(--amber-g);}
.btn-outline{background:var(--card);color:var(--text);border:1.5px solid var(--border);}
.btn-outline:hover{border-color:var(--amber);color:var(--amber);}
.btn-ghost-red{background:rgba(220,38,38,0.08);color:var(--red);font-size:11px;padding:4px 9px;border-radius:6px;border:none;cursor:pointer;font-family:'Manrope',sans-serif;font-weight:600;}
.btn-ghost-red:hover{background:var(--red);color:#fff;}
.btn-sm{padding:5px 10px;font-size:11px;}

/* ── MODALS ── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:200;padding:16px;backdrop-filter:blur(4px);}
.modal{background:var(--card);border-radius:18px;overflow:hidden;width:100%;max-width:480px;box-shadow:0 32px 64px var(--shadow2);animation:modalIn .25s cubic-bezier(.34,1.56,.64,1);}
@keyframes modalIn{from{opacity:0;transform:scale(.94);}to{opacity:1;transform:scale(1);}}
.modal-top{background:linear-gradient(135deg,#0c0d10,#1a1b22);padding:24px 28px;}
.modal-top h3{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:2px;color:#fff;margin-bottom:3px;}
.modal-top p{font-size:12px;color:rgba(255,255,255,.4);}
.modal-body{padding:24px 28px;}
.form-group{margin-bottom:12px;}
.form-group label{display:block;font-size:10px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1px;margin-bottom:5px;}
.form-group input,.form-group select,.form-group textarea{width:100%;padding:10px 13px;border:1.5px solid var(--border);border-radius:9px;font-size:13px;font-family:'Manrope',sans-serif;outline:none;background:var(--bg3);color:var(--text);transition:border-color .15s;}
.form-group input:focus,.form-group select:focus,.form-group textarea:focus{border-color:var(--amber);}
.form-row-2{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:20px;}

/* ── SITE FOLDERS ── */
.site-folder{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:12px;transition:all .2s;}
.site-folder-header{padding:14px 20px;background:var(--bg3);display:flex;align-items:center;justify-content:space-between;cursor:pointer;gap:8px;flex-wrap:wrap;transition:background .3s;}
.site-folder-header:hover{background:var(--bg);}
.site-folder-name{font-family:'Bebas Neue',sans-serif;font-size:18px;letter-spacing:1.5px;color:var(--text);display:flex;align-items:center;gap:8px;}
.site-folder-meta{font-size:11px;color:var(--muted);}
.folder-chevron{font-size:12px;color:var(--muted);transition:transform .2s;}
.folder-open .folder-chevron{transform:rotate(90deg);}
.site-folder-body{display:none;padding:0;}
.folder-open .site-folder-body{display:block;}

/* ── PO CARDS ── */
.supplier-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 20px;margin-bottom:10px;transition:all .2s;}
.supplier-card:hover{border-color:var(--amber);box-shadow:0 4px 12px var(--amber-g);}
.supplier-name{font-family:'Bebas Neue',sans-serif;font-size:18px;letter-spacing:1px;color:var(--text);}
.supplier-meta{font-size:12px;color:var(--muted);margin-top:2px;}
.supplier-total{font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:1px;color:var(--amber);}

/* ── STATES ── */
.empty-state{text-align:center;padding:48px 20px;}
.empty-icon{font-size:40px;margin-bottom:12px;}
.empty-title{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:1.5px;color:var(--text);margin-bottom:6px;}
.empty-sub{font-size:13px;color:var(--muted);}
.loading{text-align:center;padding:48px;color:var(--faint);}
.spinner{display:inline-block;width:26px;height:26px;border:2.5px solid var(--border);border-top-color:var(--amber);border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
.toast{position:fixed;bottom:24px;right:24px;background:var(--card);border:1px solid var(--border);color:var(--text);padding:11px 18px;border-radius:10px;font-size:13px;font-weight:700;z-index:300;box-shadow:0 8px 24px var(--shadow2);animation:toastIn .2s ease;}
.toast.success{border-color:var(--green);color:var(--green);}
.toast.error{border-color:var(--red);color:var(--red);}
@keyframes toastIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}

/* ── MOBILE ── */
@media(max-width:768px){
  .sidebar{display:none;}
  .mobile-nav{display:flex;position:fixed;bottom:0;left:0;right:0;background:var(--nav-bg);border-top:1px solid rgba(255,255,255,0.06);z-index:100;padding:8px 0 calc(8px + env(safe-area-inset-bottom));}
  .mobile-nav-item{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 4px;cursor:pointer;color:rgba(255,255,255,.45);font-size:9px;font-weight:600;font-family:'Manrope',sans-serif;text-transform:uppercase;letter-spacing:.5px;transition:color .15s;border:none;background:none;}
  .mobile-nav-item.active{color:var(--amber);}
  .mobile-nav-icon{font-size:18px;}
  .main{padding-bottom:64px;}
  .page-content{padding:12px;}
  .kpi-row{grid-template-columns:1fr 1fr;gap:8px;}
  .kpi{padding:14px 16px;}
  .kpi-value{font-size:30px;}
  .charts-row{grid-template-columns:1fr;}
}
@media(min-width:769px){.mobile-nav{display:none;}}
</style>
</head>
<body>

<svg style="display:none" aria-hidden="true">
  <symbol id="n2q" viewBox="0 0 40 40">
    <path d="M20 5h10l9 9v18a2 2 0 01-2 2H20a2 2 0 01-2-2V7a2 2 0 012-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M30 5v9h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="23" y1="20" x2="35" y2="20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <line x1="23" y1="25" x2="31" y2="25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <rect x="3" y="11" width="10" height="14" rx="5" fill="#f59e0b"/>
    <path d="M1 21.5a7 7 0 0014 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="8" y1="28.5" x2="8" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="5" y1="33" x2="11" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  </symbol>
</svg>

<!-- LOGIN -->
<div id="login-screen">
  <div class="login-card">
    <div class="login-top">
      <div class="login-logo"><svg width="22" height="22" viewBox="0 0 40 40"><use href="#n2q" color="white"/></svg>Note2Quote</div>
      <p>Sign in to your dashboard</p>
    </div>
    <div class="login-body">
      <input type="text" id="ln" placeholder="Username or WhatsApp number (+447...)" />
      <input type="password" id="lp" placeholder="Password" />
      <button class="btn-login" onclick="doLogin()">Sign in →</button>
      <p style="text-align:center;margin-top:10px;font-size:12px;color:var(--faint)">Or <a href="#" onclick="requestMagicLink()" style="color:var(--amber);text-decoration:none;font-weight:600">get a magic link via WhatsApp</a></p>
      <p class="err" id="lerr"></p>
    </div>
  </div>
</div>

<!-- APP -->
<div id="app">
  <div class="app-shell">

    <!-- SIDEBAR -->
    <div class="sidebar">
      <div class="sidebar-logo">
        <a href="/"><svg width="18" height="18" viewBox="0 0 40 40"><use href="#n2q" color="white"/></svg>Note2Quote</a>
        <div class="sidebar-company" id="sb-company"></div>
      </div>
      <nav class="sidebar-nav">
        <div class="nav-section">
          <div class="nav-section-label">Main</div>
          <button class="nav-item active" onclick="goto('overview')" id="nav-overview"><span class="nav-icon">📊</span>Overview</button>
          <button class="nav-item" onclick="goto('logs')" id="nav-logs"><span class="nav-icon">📋</span>All Logs</button>
          <button class="nav-item" onclick="goto('sites')" id="nav-sites"><span class="nav-icon">📍</span>My Sites</button>
        </div>
        <div class="nav-section">
          <div class="nav-section-label">Finance</div>
          <button class="nav-item" onclick="goto('purchase-orders')" id="nav-purchase-orders"><span class="nav-icon">📦</span>Purchase Orders</button>
          <button class="nav-item" onclick="goto('documents')" id="nav-documents"><span class="nav-icon">📄</span>Documents Sent</button>
          <button class="nav-item" onclick="goto('clients')" id="nav-clients"><span class="nav-icon">👥</span>Clients</button>
        </div>
      </nav>
      <div class="sidebar-actions">
        <a href="/account" class="sidebar-btn" style="text-align:center;text-decoration:none;display:flex;align-items:center;justify-content:center;gap:4px">⚙️ Account</a>
        <button class="sidebar-btn" onclick="toggleTheme()" id="theme-btn">🌙 Theme</button>
        <button class="sidebar-btn" onclick="logout()">Sign out</button>
      </div>
      <div class="sidebar-bottom">
        <div class="sidebar-user">
          <div class="sidebar-avatar" id="sb-avatar">?</div>
          <div class="sidebar-user-info">
            <div class="sidebar-user-name" id="sb-name">Loading...</div>
            <div class="sidebar-user-role">Boss</div>
          </div>
        </div>
      </div>
    </div>

    <!-- MAIN -->
    <div class="main" id="main">
      <div class="topbar">
        <div class="topbar-title" id="page-title">Overview</div>
        <div class="topbar-right">
          <button class="btn btn-amber btn-sm" onclick="openAddLog()">+ Add entry</button>
        </div>
      </div>
      <div class="page-content" id="page-content">
        <div class="loading"><div class="spinner"></div></div>
      </div>
    </div>

  </div>

  <!-- MOBILE NAV -->
  <nav class="mobile-nav">
    <button class="mobile-nav-item active" onclick="goto('overview')" id="mnav-overview"><span class="mobile-nav-icon">📊</span>Overview</button>
    <button class="mobile-nav-item" onclick="goto('logs')" id="mnav-logs"><span class="mobile-nav-icon">📋</span>Logs</button>
    <button class="mobile-nav-item" onclick="goto('sites')" id="mnav-sites"><span class="mobile-nav-icon">📍</span>Sites</button>
    <button class="mobile-nav-item" onclick="goto('purchase-orders')" id="mnav-purchase-orders"><span class="mobile-nav-icon">📦</span>Orders</button>
    <button class="mobile-nav-item" onclick="goto('documents')" id="mnav-documents"><span class="mobile-nav-icon">📄</span>Docs</button>
    <button class="mobile-nav-item" onclick="goto('clients')" id="mnav-clients"><span class="mobile-nav-icon">👥</span>Clients</button>
  </nav>
</div>

<!-- ADD LOG MODAL -->
<div class="overlay" id="add-log-modal" style="display:none">
  <div class="modal">
    <div class="modal-top"><h3>Add Log Entry</h3><p>Manually add a site log — text, voice or phone order</p></div>
    <div class="modal-body">
      <div class="form-row-2">
        <div class="form-group"><label>Type</label>
          <select id="al-type">
            <option value="VARIATION">Variation Order</option>
            <option value="DAYWORK">Daywork</option>
            <option value="MATERIAL_ORDER">Material Order</option>
            <option value="TIMESHEET">Timesheet</option>
          </select>
        </div>
        <div class="form-group"><label>Site</label><select id="al-site"></select></div>
      </div>
      <div class="form-group"><label>Description</label><input type="text" id="al-desc" placeholder="e.g. Extra sockets in room 4" /></div>
      <div class="form-row-2">
        <div class="form-group"><label>Hours</label><input type="number" id="al-hours" placeholder="2.5" step="0.5" /></div>
        <div class="form-group"><label>Est. Cost (£)</label><input type="number" id="al-cost" placeholder="80" /></div>
      </div>
      <div class="form-row-2">
        <div class="form-group"><label>Location</label><input type="text" id="al-location" placeholder="Room 4" /></div>
        <div class="form-group"><label>Supplier (POs only)</label><input type="text" id="al-supplier" placeholder="Screwfix" /></div>
      </div>
      <div class="modal-actions">
        <button class="btn btn-outline" onclick="closeModal('add-log-modal')">Cancel</button>
        <button class="btn btn-amber" onclick="submitAddLog()">Save entry</button>
      </div>
    </div>
  </div>
</div>

<!-- GENERATE PDF MODAL -->
<div class="overlay" id="gen-modal" style="display:none">
  <div class="modal">
    <div class="modal-top"><h3>Generate Document</h3><p id="gen-modal-site"></p></div>
    <div class="modal-body">
      <div class="form-group"><label>Document Type</label>
        <select id="gen-type">
          <option value="VARIATION">Variation Order (VO)</option>
          <option value="DAYWORK">Daywork Sheet (DS)</option>
          <option value="MATERIAL_ORDER">Purchase Order (PO)</option>
        </select>
      </div>
      <div class="modal-actions">
        <button class="btn btn-outline" onclick="closeModal('gen-modal')">Cancel</button>
        <button class="btn btn-amber" onclick="confirmGenerate()">Generate PDF →</button>
      </div>
    </div>
  </div>
</div>

<!-- PDF READY MODAL -->
<div class="overlay" id="pdf-modal" style="display:none">
  <div class="modal">
    <div class="modal-top"><h3 id="pdf-ref">PDF Ready</h3><p id="pdf-desc"></p></div>
    <div class="modal-body">
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px;" id="pdf-btns"></div>
      <div style="padding-top:16px;border-top:1px solid var(--border);">
        <div class="form-group"><label>Email to client (optional)</label><input type="email" id="pdf-email" placeholder="client@example.com" /></div>
        <div class="modal-actions">
          <button class="btn btn-outline" onclick="closeModal('pdf-modal')">Close</button>
          <button class="btn btn-amber" onclick="sendEmail()">Send email →</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
// ── STATE ──────────────────────────────────────────────────────────────────
var S={pw:'',num:'',data:null,page:'overview',currentSite:null,currentPdf:null,charts:{},
  filters:{period:'month',site:'all',type:'all',status:'all',dateFrom:'',dateTo:''},
  sortCol:'',sortDir:1};

// ── THEME ──────────────────────────────────────────────────────────────────
function toggleTheme(){
  var h=document.documentElement,btn=document.getElementById('theme-btn');
  var n=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',n);
  btn.textContent=(n==='dark'?'☀️':'🌙')+' Theme';
  localStorage.setItem('n2q-theme',n);
  // Redraw charts for new theme
  if(S.data) setTimeout(function(){renderCharts(S.data);},100);
}
var saved=localStorage.getItem('n2q-theme');
if(saved){var tb=document.getElementById('theme-btn');if(tb)tb.textContent=(saved==='dark'?'☀️':'🌙')+' Theme';}

// ── AUTH ───────────────────────────────────────────────────────────────────
async function doLogin(){
  var login=document.getElementById('ln').value.trim();
  var pw=document.getElementById('lp').value.trim();
  if(!login||!pw){document.getElementById('lerr').textContent='Fill in both fields.';return;}
  // Try username login first, then fall back to master password
  var r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({login:login,password:pw})});
  var d=await r.json();
  if(d.ok){
    S.pw=d.session_token||pw;
    S.num=d.whatsapp||login;
    localStorage.setItem('ss_password',S.pw);
    localStorage.setItem('ss_number',S.num);
    showApp();
  } else {
    document.getElementById('lerr').textContent=d.error||'Wrong username or password.';
  }
}

async function requestMagicLink(){
  var login=document.getElementById('ln').value.trim();
  if(!login){document.getElementById('lerr').textContent='Enter your WhatsApp number first.';return;}
  var r=await fetch('/api/auth/magic-request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({login:login})});
  var d=await r.json();
  if(d.ok){document.getElementById('lerr').style.color='var(--green)';document.getElementById('lerr').textContent='Magic link sent to your WhatsApp!';}
  else{document.getElementById('lerr').textContent=d.error||'Could not send magic link.';}
}
function logout(){localStorage.removeItem('ss_password');localStorage.removeItem('ss_number');localStorage.removeItem('ss_magic');S={pw:'',num:'',data:null,page:'overview',currentSite:null,currentPdf:null,charts:{},filters:{period:'month',site:'all',type:'all',status:'all',dateFrom:'',dateTo:''},sortCol:'',sortDir:1};document.getElementById('login-screen').style.display='flex';document.getElementById('app').style.display='none';}

function api(url,opts){
  opts=opts||{};
  return fetch(url,Object.assign({},opts,{headers:Object.assign({'Content-Type':'application/json','X-Dashboard-Password':S.pw,'X-Dashboard-Number':S.num},opts.headers||{})}));
}

// ── INIT ───────────────────────────────────────────────────────────────────
async function showApp(){
  document.getElementById('login-screen').style.display='none';
  document.getElementById('app').style.display='block';
  await loadData();
}

async function loadData(){
  var r=await api('/api/summary?number='+encodeURIComponent(S.num));
  var d=await r.json();
  S.data=d;
  var c=d.company||{};
  var name=c.company_name||'My Company';
  var initials=name.charAt(0).toUpperCase();
  document.getElementById('sb-company').textContent=name;
  document.getElementById('sb-name').textContent=name;
  document.getElementById('sb-avatar').textContent=initials;
  // Populate site dropdown in add log modal
  var siteEl=document.getElementById('al-site');
  siteEl.innerHTML='<option value="">-- Select site --</option>';
  (d.projects||[]).forEach(function(p){siteEl.innerHTML+='<option value="'+p.site_name+'">'+p.site_name+'</option>';});
  renderPage();
}

// ── NAVIGATION ─────────────────────────────────────────────────────────────
function goto(page){
  S.page=page;
  // Sidebar
  document.querySelectorAll('.nav-item').forEach(function(el){el.classList.remove('active');});
  var ni=document.getElementById('nav-'+page);if(ni)ni.classList.add('active');
  // Mobile nav
  document.querySelectorAll('.mobile-nav-item').forEach(function(el){el.classList.remove('active');});
  var mni=document.getElementById('mnav-'+page);if(mni)mni.classList.add('active');
  // Title
  var titles={overview:'Overview',logs:'All Logs',sites:'My Sites','purchase-orders':'Purchase Orders',documents:'Documents Sent',clients:'Clients'};
  document.getElementById('page-title').textContent=titles[page]||page;
  renderPage();
}

function renderPage(){
  destroyCharts();
  if(S.page==='overview') renderOverview();
  else if(S.page==='logs') renderLogs();
  else if(S.page==='sites') renderSites();
  else if(S.page==='purchase-orders') renderPOs();
  else if(S.page==='documents') renderDocuments();
  else if(S.page==='clients') renderClients();
}

// ── FILTER BAR ─────────────────────────────────────────────────────────────
function filtersBar(showSite,showType,showStatus){
  var d=S.data||{};
  var projects=d.projects||[];
  var siteOpts='<option value="all">All sites</option>';
  projects.forEach(function(p){siteOpts+='<option value="'+p.site_name+'"'+(S.filters.site===p.site_name?' selected':'')+'>'+p.site_name+'</option>';});
  var typeOpts='<option value="all">All types</option><option value="VARIATION">Variations</option><option value="DAYWORK">Dayworks</option><option value="MATERIAL_ORDER">Material Orders</option><option value="TIMESHEET">Timesheets</option>';
  var statusOpts='<option value="all">All status</option><option value="pending">Pending</option><option value="approved">Approved</option><option value="chasing">Chasing</option><option value="cancelled">Cancelled</option><option value="sent">Sent</option>';
  var periods=['week','month','range'];
  var pLabels={week:'This week',month:'This month',range:'Date range'};
  var html='<div class="filters-bar">';
  periods.forEach(function(p){html+='<button class="filter-btn'+(S.filters.period===p?' active':'')+'" onclick="setFilter(\\'period\\',\\''+p+'\\')">'+(pLabels[p])+'</button>';});
  if(S.filters.period==='range'){html+='<span class="filter-sep"></span><input type="date" class="date-input" value="'+S.filters.dateFrom+'" onchange="setFilter(\\'dateFrom\\',this.value)"><span style="color:var(--faint);font-size:12px">to</span><input type="date" class="date-input" value="'+S.filters.dateTo+'" onchange="setFilter(\\'dateTo\\',this.value)">';}
  if(showSite)html+='<span class="filter-sep"></span><select class="filter-select" onchange="setFilter(\\'site\\',this.value)">'+siteOpts+'</select>';
  if(showType)html+='<select class="filter-select" onchange="setFilter(\\'type\\',this.value)">'+typeOpts+'</select>';
  if(showStatus)html+='<select class="filter-select" onchange="setFilter(\\'status\\',this.value)">'+statusOpts+'</select>';
  html+='</div>';
  return html;
}

function setFilter(key,val){S.filters[key]=val;renderPage();}

// ── GET FILTERED LOGS ──────────────────────────────────────────────────────
function getFilteredLogs(allLogs){
  var now=new Date();
  return (allLogs||[]).filter(function(l){
    // Period filter
    var created=new Date(l.created_at);
    if(S.filters.period==='week'){var wk=new Date(now);wk.setDate(now.getDate()-7);if(created<wk)return false;}
    else if(S.filters.period==='month'){var mo=new Date(now);mo.setDate(1);mo.setHours(0,0,0,0);if(created<mo)return false;}
    else if(S.filters.period==='range'){
      if(S.filters.dateFrom&&created<new Date(S.filters.dateFrom))return false;
      if(S.filters.dateTo){var dt=new Date(S.filters.dateTo);dt.setHours(23,59,59);if(created>dt)return false;}
    }
    // Site
    if(S.filters.site!=='all'&&l.site_name!==S.filters.site)return false;
    // Type
    if(S.filters.type!=='all'&&l.type!==S.filters.type)return false;
    // Status
    if(S.filters.status!=='all'&&l.status!==S.filters.status)return false;
    return true;
  });
}

function getAllLogs(){
  var d=S.data||{};
  var pending=[];var sent=[];
  Object.values(d.by_site||{}).forEach(function(s){pending=pending.concat(s.logs||[]);});
  (d.recent_sent||[]).forEach(function(l){sent.push(l);});
  return pending.concat(sent);
}

// ── OVERVIEW ───────────────────────────────────────────────────────────────
function renderOverview(){
  var d=S.data||{};
  var allLogs=getAllLogs();
  var filtered=getFilteredLogs(allLogs);
  var totalVal=filtered.reduce(function(a,l){return a+parseFloat(l.cost_estimate||0);},0);
  var totalHrs=filtered.reduce(function(a,l){return a+parseFloat(l.hours||0);},0);
  var pending=filtered.filter(function(l){return l.status==='pending';}).length;
  var approved=filtered.filter(function(l){return l.status==='approved';}).length;

  var html=filtersBar(true,true,false);
  html+='<div class="kpi-row">';
  html+='<div class="kpi"><div class="kpi-label">Total Items</div><div class="kpi-value">'+filtered.length+'</div><div class="kpi-sub">In selected period</div></div>';
  html+='<div class="kpi"><div class="kpi-label">Est. Value</div><div class="kpi-value amber">£'+totalVal.toFixed(0)+'</div><div class="kpi-sub">Logged this period</div></div>';
  html+='<div class="kpi"><div class="kpi-label">Total Hours</div><div class="kpi-value">'+totalHrs.toFixed(1)+'</div><div class="kpi-sub">Extra work logged</div></div>';
  html+='<div class="kpi"><div class="kpi-label">Approved</div><div class="kpi-value green">'+approved+'</div><div class="kpi-sub">'+pending+' still pending</div></div>';
  html+='</div>';
  html+='<div class="charts-row"><div class="chart-card"><div class="chart-title">Value by Site</div><div class="chart-wrap"><canvas id="chart-bar"></canvas></div></div>';
  html+='<div class="chart-card"><div class="chart-title">Activity Over Time</div><div class="chart-wrap"><canvas id="chart-line"></canvas></div></div>';
  html+='<div class="chart-card"><div class="chart-title">By Type</div><div class="chart-wrap"><canvas id="chart-donut"></canvas></div></div></div>';

  // Recent pending
  var pend=filtered.filter(function(l){return l.status==='pending';}).slice(0,10);
  html+='<div class="section-card"><div class="section-header"><div class="section-title">Pending <span class="count-pill">'+pend.length+'</span></div><div class="section-actions"><button class="btn btn-amber btn-sm" onclick="goto(\\'logs\\')">View all</button></div></div>';
  if(pend.length){
    html+='<div class="tbl-wrap"><table><thead><tr><th>Type</th><th>Description</th><th>Site</th><th>Cost</th><th>Status</th></tr></thead><tbody>';
    pend.forEach(function(l){
      html+='<tr><td class="cell-pad"><span class="type-pill t-'+l.type+'">'+(l.type||'').replace('_',' ')+'</span></td><td class="cell-pad">'+(l.description||'—')+'</td><td class="cell-pad">'+(l.site_name||'—')+'</td><td class="cell-pad">'+(l.cost_estimate?'£'+parseFloat(l.cost_estimate).toFixed(2):'—')+'</td><td class="cell-pad">'+statusSelect(l.id,l.status)+'</td></tr>';
    });
    html+='</tbody></table></div>';
  }else{html+='<div class="empty-state"><div class="empty-icon">✅</div><div class="empty-title">All caught up!</div><div class="empty-sub">No pending items in this period</div></div>';}
  html+='</div>';

  document.getElementById('page-content').innerHTML=html;
  renderCharts(d,filtered);
}

function renderCharts(d,filtered){
  var isDark=document.documentElement.getAttribute('data-theme')==='dark';
  var textColor=isDark?'rgba(255,255,255,0.5)':'rgba(0,0,0,0.4)';
  var gridColor=isDark?'rgba(255,255,255,0.06)':'rgba(0,0,0,0.06)';
  Chart.defaults.color=textColor;
  Chart.defaults.font.family='Manrope';

  var allLogs=filtered||getFilteredLogs(getAllLogs());

  // Bar chart — value per site
  var barCanvas=document.getElementById('chart-bar');
  if(barCanvas){
    var siteMap={};
    allLogs.forEach(function(l){var s=l.site_name||'Unassigned';siteMap[s]=(siteMap[s]||0)+parseFloat(l.cost_estimate||0);});
    var barLabels=Object.keys(siteMap);var barData=barLabels.map(function(k){return siteMap[k];});
    destroyChart('bar');
    S.charts.bar=new Chart(barCanvas,{type:'bar',data:{labels:barLabels,datasets:[{data:barData,backgroundColor:'rgba(245,158,11,0.7)',borderColor:'rgba(245,158,11,1)',borderWidth:1.5,borderRadius:6}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{grid:{color:gridColor},ticks:{callback:function(v){return'£'+v;}}},x:{grid:{color:gridColor}}}}});
  }

  // Line chart — logs per day
  var lineCanvas=document.getElementById('chart-line');
  if(lineCanvas){
    var dayMap={};
    allLogs.forEach(function(l){var day=l.created_at?l.created_at.substr(0,10):'';if(day){dayMap[day]=(dayMap[day]||0)+1;}});
    var days=Object.keys(dayMap).sort();var dayData=days.map(function(d){return dayMap[d];});
    var shortDays=days.map(function(d){var dt=new Date(d);return dt.toLocaleDateString('en-GB',{day:'numeric',month:'short'});});
    destroyChart('line');
    S.charts.line=new Chart(lineCanvas,{type:'line',data:{labels:shortDays,datasets:[{data:dayData,borderColor:'rgba(245,158,11,1)',backgroundColor:'rgba(245,158,11,0.08)',borderWidth:2,pointRadius:3,pointBackgroundColor:'rgba(245,158,11,1)',fill:true,tension:.4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{grid:{color:gridColor},ticks:{stepSize:1}},x:{grid:{color:gridColor}}}}});
  }

  // Donut — by type
  var donutCanvas=document.getElementById('chart-donut');
  if(donutCanvas){
    var typeMap={VARIATION:0,DAYWORK:0,MATERIAL_ORDER:0,TIMESHEET:0};
    allLogs.forEach(function(l){if(l.type&&typeMap[l.type]!==undefined)typeMap[l.type]++;});
    var donutLabels=['Variation','Daywork','Material Order','Timesheet'];
    var donutData=[typeMap.VARIATION,typeMap.DAYWORK,typeMap.MATERIAL_ORDER,typeMap.TIMESHEET];
    var donutColors=['rgba(245,158,11,0.85)','rgba(34,197,94,0.85)','rgba(124,58,237,0.85)','rgba(236,72,153,0.85)'];
    destroyChart('donut');
    S.charts.donut=new Chart(donutCanvas,{type:'doughnut',data:{labels:donutLabels,datasets:[{data:donutData,backgroundColor:donutColors,borderWidth:0,hoverOffset:8}]},options:{responsive:true,maintainAspectRatio:false,cutout:'68%',plugins:{legend:{position:'bottom',labels:{padding:10,font:{size:11},boxWidth:10}}}}});
  }
}

function destroyChart(key){if(S.charts[key]){S.charts[key].destroy();delete S.charts[key];}}
function destroyCharts(){Object.keys(S.charts).forEach(function(k){destroyChart(k);});}

// ── LOGS (MASTER SPREADSHEET) ───────────────────────────────────────────────
function renderLogs(){
  var allLogs=getAllLogs();
  var filtered=getFilteredLogs(allLogs);
  // Sort
  if(S.sortCol){filtered.sort(function(a,b){var av=a[S.sortCol]||'',bv=b[S.sortCol]||'';return av<bv?-S.sortDir:av>bv?S.sortDir:0;});}
  var html=filtersBar(true,true,true);
  html+='<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">';
  html+='<div style="font-size:13px;color:var(--muted)">Showing <strong style="color:var(--text)">'+filtered.length+'</strong> entries</div>';
  html+='<button class="btn btn-outline btn-sm" onclick="openAddLog()">+ Add manually</button></div>';

  if(!filtered.length){
    html+='<div class="empty-state"><div class="empty-icon">📋</div><div class="empty-title">No entries found</div><div class="empty-sub">Try adjusting your filters or add a new entry</div></div>';
  }else{
    var types=['VARIATION','DAYWORK','MATERIAL_ORDER','TIMESHEET','UNKNOWN'];
    html+='<div class="section-card"><div class="tbl-wrap"><table><thead><tr>';
    var cols=[{k:'type',l:'Type'},{k:'description',l:'Description'},{k:'site_name',l:'Site'},{k:'location',l:'Location'},{k:'hours',l:'Hrs'},{k:'cost_estimate',l:'Cost'},{k:'status',l:'Status'},{k:'created_at',l:'Date'}];
    cols.forEach(function(c){var dir=S.sortCol===c.k?(S.sortDir===1?' ↑':' ↓'):'';html+='<th onclick="sortBy(\\''+c.k+'\\')">'+c.l+dir+'</th>';});
    html+='<th></th></tr></thead><tbody>';
    filtered.forEach(function(l){
      var typeOpts=types.map(function(t){return '<option value="'+t+'"'+(l.type===t?' selected':'')+'>'+t.replace('_',' ')+'</option>';}).join('');
      var date=l.created_at?new Date(l.created_at).toLocaleDateString('en-GB'):'—';
      html+='<tr id="row-'+l.id+'">';
      html+='<td style="padding:6px 14px"><select style="border:none;background:transparent;font-size:11px;font-weight:700;cursor:pointer;font-family:Manrope,sans-serif;color:inherit;outline:none" onchange="saveField('+l.id+',\\'type\\',this.value)">'+typeOpts+'</select></td>';
      html+='<td><div class="cell" contenteditable="true" onblur="saveField('+l.id+',\\'description\\',this.innerText.trim())">'+(l.description||'')+'</div></td>';
      html+='<td><div class="cell" style="min-width:100px" contenteditable="true" onblur="saveField('+l.id+',\\'site_name\\',this.innerText.trim())">'+(l.site_name||'')+'</div></td>';
      html+='<td><div class="cell" contenteditable="true" onblur="saveField('+l.id+',\\'location\\',this.innerText.trim())">'+(l.location||'')+'</div></td>';
      html+='<td><div class="cell" style="max-width:60px" contenteditable="true" onblur="saveField('+l.id+',\\'hours\\',this.innerText.trim())">'+(l.hours||'')+'</div></td>';
      html+='<td><div class="cell" style="max-width:80px" contenteditable="true" onblur="saveField('+l.id+',\\'cost_estimate\\',this.innerText.trim())">'+(l.cost_estimate||'')+'</div></td>';
      html+='<td style="padding:6px 14px">'+statusSelect(l.id,l.status)+'</td>';
      html+='<td class="cell-pad" style="white-space:nowrap;color:var(--faint);font-size:12px">'+date+'</td>';
      html+='<td class="cell-pad"><button class="btn-ghost-red" onclick="deleteLog('+l.id+')">✕</button></td>';
      html+='</tr>';
    });
    html+='</tbody></table></div></div>';
  }
  document.getElementById('page-content').innerHTML=html;
}

function sortBy(col){if(S.sortCol===col){S.sortDir*=-1;}else{S.sortCol=col;S.sortDir=1;}renderLogs();}

// ── SITES ──────────────────────────────────────────────────────────────────
function renderSites(){
  var d=S.data||{};
  var bySite=d.by_site||{};
  var allLogs=getAllLogs();
  var projects=d.projects||[];
  var html='';

  if(!projects.length){
    html='<div class="empty-state"><div class="empty-icon">📍</div><div class="empty-title">No sites yet</div><div class="empty-sub">Add your first site via WhatsApp or from My Account</div></div>';
    document.getElementById('page-content').innerHTML=html;return;
  }

  projects.forEach(function(p){
    var siteLogs=allLogs.filter(function(l){return l.site_name===p.site_name;});
    var filteredSiteLogs=getFilteredLogs(siteLogs);
    var totalVal=filteredSiteLogs.reduce(function(a,l){return a+parseFloat(l.cost_estimate||0);},0);
    var pendCount=filteredSiteLogs.filter(function(l){return l.status==='pending';}).length;
    html+='<div class="site-folder" id="folder-'+p.site_name.replace(/\\s/g,'-')+'">';
    html+='<div class="site-folder-header" onclick="toggleFolder(this)">';
    html+='<div><div class="site-folder-name">📍 '+p.site_name+'</div><div class="site-folder-meta">'+filteredSiteLogs.length+' logs · £'+totalVal.toFixed(2)+' · '+pendCount+' pending</div></div>';
    html+='<div style="display:flex;align-items:center;gap:8px;"><button class="btn btn-amber btn-sm" onclick="event.stopPropagation();openGenModal(\\''+p.site_name+'\\')">Generate PDF</button><span class="folder-chevron">▶</span></div></div>';
    html+='<div class="site-folder-body">';
    if(filteredSiteLogs.length){
      html+='<div class="tbl-wrap"><table><thead><tr><th>Type</th><th>Description</th><th>Cost</th><th>Hours</th><th>Status</th><th>Date</th></tr></thead><tbody>';
      filteredSiteLogs.forEach(function(l){
        var date=l.created_at?new Date(l.created_at).toLocaleDateString('en-GB'):'—';
        html+='<tr><td class="cell-pad"><span class="type-pill t-'+l.type+'">'+(l.type||'').replace('_',' ')+'</span></td>';
        html+='<td class="cell-pad">'+(l.description||'—')+'</td>';
        html+='<td class="cell-pad">'+(l.cost_estimate?'£'+parseFloat(l.cost_estimate).toFixed(2):'—')+'</td>';
        html+='<td class="cell-pad">'+(l.hours?l.hours+' hrs':'—')+'</td>';
        html+='<td class="cell-pad">'+statusSelect(l.id,l.status)+'</td>';
        html+='<td class="cell-pad" style="color:var(--faint);font-size:12px">'+date+'</td></tr>';
      });
      html+='</tbody></table></div>';
    }else{html+='<div style="padding:20px;text-align:center;color:var(--faint);font-size:13px">No logs in this period for this site</div>';}
    html+='</div></div>';
  });

  document.getElementById('page-content').innerHTML=filtersBar(false,true,true)+html;
}

function toggleFolder(header){header.parentElement.classList.toggle('folder-open');}

// ── PURCHASE ORDERS ────────────────────────────────────────────────────────
function renderPOs(){
  var allLogs=getAllLogs();
  var filtered=getFilteredLogs(allLogs).filter(function(l){return l.type==='MATERIAL_ORDER';});
  var bySupplier={};
  filtered.forEach(function(l){
    var sup=l.supplier||'Unknown Supplier';
    if(!bySupplier[sup])bySupplier[sup]={total:0,count:0,logs:[]};
    bySupplier[sup].total+=parseFloat(l.cost_estimate||0);
    bySupplier[sup].count++;
    bySupplier[sup].logs.push(l);
  });

  var grandTotal=filtered.reduce(function(a,l){return a+parseFloat(l.cost_estimate||0);},0);
  var html=filtersBar(true,false,false);
  html+='<div class="kpi-row" style="grid-template-columns:repeat(3,1fr)">';
  html+='<div class="kpi"><div class="kpi-label">Total Orders</div><div class="kpi-value">'+filtered.length+'</div></div>';
  html+='<div class="kpi"><div class="kpi-label">Total Spend</div><div class="kpi-value amber">£'+grandTotal.toFixed(2)+'</div></div>';
  html+='<div class="kpi"><div class="kpi-label">Suppliers</div><div class="kpi-value">'+Object.keys(bySupplier).length+'</div></div>';
  html+='</div>';
  html+='<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">';
  html+='<div style="font-family:\\'Bebas Neue\\',sans-serif;font-size:20px;letter-spacing:2px;color:var(--text)">By Supplier</div>';
  html+='<button class="btn btn-amber btn-sm" onclick="openAddLog();document.getElementById(\\'al-type\\').value=\\'MATERIAL_ORDER\\'">+ Add order</button></div>';

  if(!Object.keys(bySupplier).length){
    html+='<div class="empty-state"><div class="empty-icon">📦</div><div class="empty-title">No purchase orders</div><div class="empty-sub">Material orders logged via WhatsApp appear here, or add manually above</div></div>';
  }else{
    var suppliers=Object.entries(bySupplier).sort(function(a,b){return b[1].total-a[1].total;});
    suppliers.forEach(function(entry){
      var sup=entry[0];var info=entry[1];
      html+='<div class="supplier-card"><div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px;">';
      html+='<div><div class="supplier-name">'+sup+'</div><div class="supplier-meta">'+info.count+' order'+(info.count!==1?'s':'')+'</div></div>';
      html+='<div class="supplier-total">£'+info.total.toFixed(2)+'</div></div>';
      html+='<div class="tbl-wrap"><table style="font-size:12px"><thead><tr><th>Description</th><th>Site</th><th>Cost</th><th>Date</th></tr></thead><tbody>';
      info.logs.forEach(function(l){
        var date=l.created_at?new Date(l.created_at).toLocaleDateString('en-GB'):'—';
        html+='<tr><td class="cell-pad">'+(l.description||l.materials||'—')+'</td><td class="cell-pad">'+(l.site_name||'—')+'</td><td class="cell-pad">'+(l.cost_estimate?'£'+parseFloat(l.cost_estimate).toFixed(2):'—')+'</td><td class="cell-pad" style="color:var(--faint)">'+date+'</td></tr>';
      });
      html+='</tbody></table></div></div>';
    });
  }
  document.getElementById('page-content').innerHTML=html;
}

// ── DOCUMENTS SENT ─────────────────────────────────────────────────────────
function renderDocuments(){
  var d=S.data||{};
  var sent=(d.recent_sent||[]);
  var filtered=getFilteredLogs(sent.map(function(l){return Object.assign({},l,{status:'sent'});}));
  var html=filtersBar(true,true,false);

  if(!filtered.length){
    html+='<div class="empty-state"><div class="empty-icon">📄</div><div class="empty-title">No documents yet</div><div class="empty-sub">Generate a PDF from Overview or Sites to see it here</div></div>';
    document.getElementById('page-content').innerHTML=html;return;
  }

  html+='<div class="section-card"><div class="tbl-wrap"><table><thead><tr><th>Type</th><th>Description</th><th>Site</th><th>Cost</th><th>Date</th><th></th></tr></thead><tbody>';
  filtered.forEach(function(l){
    var date=l.created_at?new Date(l.created_at).toLocaleDateString('en-GB'):'—';
    html+='<tr><td class="cell-pad"><span class="type-pill t-'+l.type+'">'+(l.type||'').replace('_',' ')+'</span></td>';
    html+='<td class="cell-pad">'+(l.description||'—')+'</td>';
    html+='<td class="cell-pad">'+(l.site_name||'—')+'</td>';
    html+='<td class="cell-pad">'+(l.cost_estimate?'£'+parseFloat(l.cost_estimate).toFixed(2):'—')+'</td>';
    html+='<td class="cell-pad" style="color:var(--faint);font-size:12px">'+date+'</td>';
    html+='<td class="cell-pad"><button class="btn btn-outline btn-sm" onclick="unsendLog('+l.id+')">Move to pending</button></td></tr>';
  });
  html+='</tbody></table></div></div>';
  document.getElementById('page-content').innerHTML=html;
}

// ── STATUS SELECT ──────────────────────────────────────────────────────────
function statusSelect(id,status){
  var opts=['pending','approved','chasing','cancelled','sent'];
  var s=status||'pending';
  var cls='s-'+s;
  var html='<select class="status-sel '+cls+'" onchange="updateStatus('+id+',this)">';
  opts.forEach(function(o){html+='<option value="'+o+'"'+(s===o?' selected':'')+'>'+o.charAt(0).toUpperCase()+o.slice(1)+'</option>';});
  html+='</select>';
  return html;
}

async function updateStatus(id,el){
  var val=el.value;
  el.className='status-sel s-'+val;
  await api('/api/log/'+id,{method:'PATCH',body:JSON.stringify({status:val})});
  showToast('Status updated','success');
  await loadData();
}

// ── CRUD ───────────────────────────────────────────────────────────────────
async function saveField(id,field,value){
  if(!value&&value!==0)return;
  var payload={};
  if(field==='hours'||field==='cost_estimate'){var n=parseFloat(value.replace(/[^0-9.]/g,''));if(isNaN(n))return;payload[field]=n;}
  else{payload[field]=value;}
  await api('/api/log/'+id,{method:'PATCH',body:JSON.stringify(payload)});
  showToast('Saved','success');
}

async function deleteLog(id){
  if(!confirm('Delete this entry?'))return;
  await api('/api/log/'+id,{method:'DELETE'});
  var row=document.getElementById('row-'+id);if(row)row.remove();
  showToast('Deleted','success');
  await loadData();renderPage();
}

async function unsendLog(id){
  await api('/api/log/'+id+'/unsend',{method:'POST'});
  showToast('Moved back to pending','success');
  await loadData();renderPage();
}

// ── ADD LOG MODAL ──────────────────────────────────────────────────────────
function openAddLog(){document.getElementById('add-log-modal').style.display='flex';}
async function submitAddLog(){
  var type=document.getElementById('al-type').value;
  var site=document.getElementById('al-site').value;
  var desc=document.getElementById('al-desc').value.trim();
  var hours=document.getElementById('al-hours').value;
  var cost=document.getElementById('al-cost').value;
  var location=document.getElementById('al-location').value.trim();
  var supplier=document.getElementById('al-supplier').value.trim();
  if(!desc){showToast('Please enter a description','error');return;}
  var payload={from_number:S.num.startsWith('whatsapp:')?S.num:'whatsapp:'+S.num,type:type,description:desc,status:'pending',raw_message:desc};
  if(site)payload.site_name=site;
  if(hours)payload.hours=parseFloat(hours);
  if(cost)payload.cost_estimate=parseFloat(cost);
  if(location)payload.location=location;
  if(supplier)payload.supplier=supplier;
  await api('/api/log/manual',{method:'POST',body:JSON.stringify(payload)});
  closeModal('add-log-modal');
  showToast('Entry added','success');
  await loadData();renderPage();
}

// ── GENERATE PDF ───────────────────────────────────────────────────────────
function openGenModal(site){
  S.currentSite=site;
  document.getElementById('gen-modal-site').textContent='For: '+site;
  document.getElementById('gen-modal').style.display='flex';
}
async function confirmGenerate(){
  var docType=document.getElementById('gen-type').value;
  closeModal('gen-modal');showToast('⏳ Generating PDF...','');
  var r=await api('/api/generate',{method:'POST',body:JSON.stringify({site_name:S.currentSite,doc_type:docType,from_number:S.num})});
  var d=await r.json();
  if(d.ok){S.currentPdf=d;showToast('✅ PDF ready!','success');
    document.getElementById('pdf-ref').textContent=d.doc_ref+' — Ready';
    document.getElementById('pdf-desc').textContent=d.items+' item(s) · '+(S.currentSite||'All Sites');
    document.getElementById('pdf-email').value='';
    document.getElementById('pdf-btns').innerHTML='<a href="'+d.pdf_url+'" download="'+d.filename+'" style="display:inline-flex;align-items:center;gap:6px;padding:9px 16px;border-radius:8px;background:#0c0d10;color:#fff;text-decoration:none;font-size:13px;font-weight:700;font-family:Manrope,sans-serif">⬇ Download</a><a href="'+d.pdf_url+'" target="_blank" style="display:inline-flex;align-items:center;gap:6px;padding:9px 16px;border-radius:8px;background:var(--bg3);color:var(--text);text-decoration:none;font-size:13px;font-weight:700;font-family:Manrope,sans-serif;border:1px solid var(--border)">👁 View</a>';
    document.getElementById('pdf-modal').style.display='flex';
    await loadData();renderPage();
  }else{showToast('❌ '+(d.error||'Failed'),'error');}
}

async function sendEmail(){
  var to=document.getElementById('pdf-email').value.trim();
  if(!to){showToast('Enter an email address','error');return;}
  if(!S.currentPdf){showToast('No PDF ready','error');return;}
  closeModal('pdf-modal');showToast('📧 Sending...','');
  var r=await api('/api/send-email',{method:'POST',body:JSON.stringify({to_email:to,pdf_b64:S.currentPdf.pdf_b64,pdf_url:S.currentPdf.pdf_url,doc_ref:S.currentPdf.doc_ref,site_name:S.currentSite,company_name:(S.data&&S.data.company&&S.data.company.company_name)||'Company',filename:S.currentPdf.filename})});
  var d=await r.json();
  showToast(d.ok?'✅ Email sent!':'❌ '+(d.error||'Failed'),d.ok?'success':'error');
}

function closeModal(id){document.getElementById(id).style.display='none';}

function showToast(msg,type){
  var t=document.createElement('div');t.className='toast '+(type||'');t.textContent=msg;
  document.body.appendChild(t);setTimeout(function(){t.remove();},3000);
}


// ── CLIENTS ───────────────────────────────────────────────────────────────
function renderClients(){
  var d=S.data||{};
  var projects=d.projects||[];
  var html='<div style="margin-bottom:16px;font-size:13px;color:var(--muted)">Add client contact details per site — bot recognises names, PDFs auto-addressed.</div>';
  if(!projects.length){
    html+='<div class="empty-state"><div class="empty-icon">👥</div><div class="empty-title">No sites yet</div><div class="empty-sub">Add sites from My Account first</div></div>';
    document.getElementById('page-content').innerHTML=html;return;
  }
  html+='<div class="section-card"><div class="tbl-wrap"><table><thead><tr><th>Site</th><th style="min-width:160px">Client Name</th><th style="min-width:200px">Email Address</th><th style="min-width:130px">Phone</th></tr></thead><tbody>';
  projects.forEach(function(p){
    html+='<tr>';
    html+='<td class="cell-pad"><strong>'+(p.site_name||'')+'</strong></td>';
    html+='<td><div class="cell" contenteditable="true" onblur="saveClientField('+p.id+',\\'client_name\\',this.innerText.trim())">'+(p.client_name||'')+'</div></td>';
    html+='<td><div class="cell" contenteditable="true" onblur="saveClientField('+p.id+',\\'client_email\\',this.innerText.trim())">'+(p.client_email||'')+'</div></td>';
    html+='<td><div class="cell" contenteditable="true" onblur="saveClientField('+p.id+',\\'client_phone\\',this.innerText.trim())">'+(p.client_phone||'')+'</div></td>';
    html+='</tr>';
  });
  html+='</tbody></table></div></div>';
  html+='<div style="margin-top:12px;padding:14px 18px;background:var(--amber-dim);border:1px solid rgba(245,158,11,0.2);border-radius:10px;font-size:13px;color:var(--text2)">💡 <strong>Tip:</strong> Once you add a client name here, the bot recognises it in WhatsApp messages automatically.</div>';
  document.getElementById('page-content').innerHTML=html;
}
async function saveClientField(projectId,field,value){
  await api('/api/client/'+projectId,{method:'PATCH',body:JSON.stringify({[field]:value})});
  showToast('Saved','success');
}

// ── BOOT ───────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded',function(){
  var params=new URLSearchParams(window.location.search);
  var autologin=params.get('autologin');
  if(autologin){S.num=autologin.startsWith('+')?autologin:'+'+autologin;S.pw='__magic__';localStorage.setItem('ss_number',S.num);localStorage.setItem('ss_magic','1');localStorage.removeItem('ss_password');window.history.replaceState({},'','/dashboard');showApp();return;}
  if(localStorage.getItem('ss_magic')==='1'){var n=localStorage.getItem('ss_number');if(n){S.num=n;S.pw='__magic__';showApp();return;}}
  var pw=localStorage.getItem('ss_password'),num=localStorage.getItem('ss_number');
  if(pw&&num){S.pw=pw;S.num=num;showApp();}
});
document.addEventListener('keydown',function(e){if(e.key==='Enter'&&document.getElementById('login-screen').style.display!=='none')doLogin();});
</script>
</body>
</html>
'''
ACCOUNT_HTML   = '''<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Account — Note2Quote</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script>var _t=localStorage.getItem('n2q-theme');if(_t)document.documentElement.setAttribute('data-theme',_t);</script>
<style>
:root{--amber:#f59e0b;--amber-d:#d97706;--amber-l:#fbbf24;--amber-dim:rgba(245,158,11,0.12);--amber-g:rgba(245,158,11,0.2);}
[data-theme="light"]{--bg:#f5f3ef;--bg2:#fff;--bg3:#eceae5;--text:#0c0d10;--text2:#374151;--muted:#6b7280;--faint:#a0a5b0;--border:#dddad4;--border2:#c8c4bc;--card:#fff;--nav-bg:#0c0d10;--shadow:rgba(0,0,0,0.06);--shadow2:rgba(0,0,0,0.14);--green:#16a34a;--red:#dc2626;}
[data-theme="dark"]{--bg:#0c0d10;--bg2:#131419;--bg3:#1a1b22;--text:#f0ede8;--text2:#c8c4bc;--muted:#8b8fa8;--faint:#4a4d5e;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);--card:#131419;--nav-bg:#080a0d;--shadow:rgba(0,0,0,0.4);--shadow2:rgba(0,0,0,0.6);--green:#22c55e;--red:#ef4444;}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;}
body{font-family:'Manrope',sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;transition:background .3s,color .3s;}

/* LOGIN */
#login-screen{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);}
.login-card{background:var(--card);border:1px solid var(--border);border-radius:20px;overflow:hidden;width:100%;max-width:400px;box-shadow:0 32px 64px var(--shadow2);animation:fadeUp .6s cubic-bezier(.16,1,.3,1) both;}
@keyframes fadeUp{from{opacity:0;transform:translateY(24px);}to{opacity:1;transform:translateY(0);}}
.login-top{background:linear-gradient(135deg,#0c0d10,#1a1b22);padding:36px 36px 28px;position:relative;overflow:hidden;}
.login-top::after{content:'';position:absolute;top:-40%;right:-20%;width:220px;height:220px;background:radial-gradient(circle,rgba(245,158,11,0.14),transparent 65%);pointer-events:none;}
.login-logo{font-family:'Bebas Neue',sans-serif;font-size:26px;letter-spacing:3px;color:#fff;display:flex;align-items:center;gap:10px;margin-bottom:4px;position:relative;z-index:1;}
.login-top p{font-size:13px;color:rgba(255,255,255,.4);position:relative;z-index:1;}
.login-body{padding:28px 36px;}
.login-body input{width:100%;padding:11px 14px;border:1.5px solid var(--border);border-radius:10px;font-size:14px;font-family:'Manrope',sans-serif;outline:none;margin-bottom:10px;background:var(--bg3);color:var(--text);transition:border-color .15s;}
.login-body input:focus{border-color:var(--amber);}
.btn-login{width:100%;background:var(--amber);color:#0c0d10;border:none;padding:13px;border-radius:10px;font-size:14px;font-weight:700;font-family:'Manrope',sans-serif;cursor:pointer;box-shadow:0 4px 0 var(--amber-d);transition:all .15s;}
.btn-login:hover{transform:translateY(-1px);box-shadow:0 5px 0 var(--amber-d),0 10px 20px var(--amber-g);}
.err{color:var(--red);font-size:12px;margin-top:6px;font-weight:500;}

/* APP SHELL */
#app{display:none;height:100vh;overflow:hidden;}
.app-shell{display:flex;height:100%;}

/* SIDEBAR */
.sidebar{width:220px;flex-shrink:0;background:var(--nav-bg);display:flex;flex-direction:column;overflow-y:auto;}
.sidebar-logo{padding:20px 20px 16px;border-bottom:1px solid rgba(255,255,255,0.06);}
.sidebar-logo a{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:3px;color:#fff;text-decoration:none;display:flex;align-items:center;gap:8px;}
.sidebar-company{font-size:11px;color:rgba(255,255,255,0.3);margin-top:2px;padding-left:32px;}
.sidebar-nav{flex:1;padding:12px 10px;}
.nav-section{margin-bottom:20px;}
.nav-section-label{font-size:9px;font-weight:700;color:rgba(255,255,255,0.25);text-transform:uppercase;letter-spacing:2px;padding:0 10px;margin-bottom:6px;}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:9px;cursor:pointer;color:rgba(255,255,255,.55);font-size:13px;font-weight:500;transition:all .15s;border:none;background:none;width:100%;text-align:left;font-family:'Manrope',sans-serif;text-decoration:none;}
.nav-item:hover{background:rgba(255,255,255,0.06);color:#fff;}
.nav-item.active{background:var(--amber);color:#0c0d10;font-weight:700;}
.nav-icon{font-size:16px;flex-shrink:0;opacity:.7;}
.nav-item.active .nav-icon{opacity:1;}
.sidebar-bottom{padding:12px 10px;border-top:1px solid rgba(255,255,255,0.06);}
.sidebar-actions{display:flex;gap:6px;padding:0 0 8px;}
.sidebar-btn{flex:1;padding:7px;border-radius:7px;border:1px solid rgba(255,255,255,0.1);background:none;color:rgba(255,255,255,.5);font-size:11px;font-family:'Manrope',sans-serif;font-weight:600;cursor:pointer;transition:all .15s;}
.sidebar-btn:hover{background:rgba(255,255,255,0.08);color:#fff;}
.sidebar-user{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:9px;background:rgba(255,255,255,0.04);}
.sidebar-avatar{width:30px;height:30px;background:var(--amber);border-radius:50%;display:flex;align-items:center;justify-content:center;font-family:'Bebas Neue',sans-serif;font-size:13px;color:#0c0d10;flex-shrink:0;}
.sidebar-user-name{font-size:12px;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.sidebar-user-role{font-size:10px;color:rgba(255,255,255,.3);}

/* MAIN */
.main{flex:1;overflow-y:auto;display:flex;flex-direction:column;}
.topbar{padding:16px 28px;background:var(--card);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;transition:background .3s,border-color .3s;}
.topbar-title{font-family:'Bebas Neue',sans-serif;font-size:22px;letter-spacing:2px;color:var(--text);}
.topbar-right{display:flex;align-items:center;gap:8px;}
.theme-btn{width:32px;height:32px;border-radius:7px;border:1px solid var(--border);background:var(--card);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;transition:all .2s;}
.theme-btn:hover{border-color:var(--amber);}
.page-content{padding:24px 28px;}
@media(max-width:768px){.sidebar{display:none;}.page-content{padding:16px;}}

/* SECTIONS */
.section{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:16px;transition:background .3s,border-color .3s;animation:fadeUp .5s cubic-bezier(.16,1,.3,1) both;}
.section-header{padding:14px 20px;background:var(--bg3);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;transition:background .3s,border-color .3s;}
.section-title{font-family:'Bebas Neue',sans-serif;font-size:17px;letter-spacing:1.5px;color:var(--text);transition:color .3s;}
.section-body{padding:24px 20px;}

/* SUBSCRIPTION */
.sub-card{display:flex;align-items:center;justify-content:space-between;background:var(--bg3);border:1px solid var(--border);border-radius:12px;padding:18px 20px;flex-wrap:wrap;gap:12px;transition:background .3s,border-color .3s;}
.sub-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;}
.dot-active{background:var(--green);box-shadow:0 0 6px rgba(34,197,94,.4);}
.dot-trialing{background:var(--amber);box-shadow:0 0 6px var(--amber-g);}
.dot-canceled{background:var(--red);}
.sub-name{font-weight:700;font-size:15px;color:var(--text);}
.sub-detail{font-size:12px;color:var(--muted);margin-top:2px;}
.sub-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;}

/* FORM */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
@media(max-width:600px){.form-grid{grid-template-columns:1fr;}}
.form-group{margin-bottom:0;}
.form-group label{display:block;font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;}
.form-group input,.form-group select{width:100%;padding:10px 13px;border:1.5px solid var(--border);border-radius:9px;font-size:14px;font-family:'Manrope',sans-serif;outline:none;background:var(--bg3);color:var(--text);transition:border-color .15s,box-shadow .15s;}
.form-group input:focus,.form-group select:focus{border-color:var(--amber);box-shadow:0 0 0 3px var(--amber-dim);}
.hint{font-size:11px;color:var(--faint);margin-top:4px;}
.color-row{display:flex;gap:10px;align-items:center;}
.color-row input[type="color"]{width:42px;height:40px;padding:2px 3px;border-radius:9px;cursor:pointer;flex-shrink:0;border:1.5px solid var(--border);background:var(--bg3);}
.form-actions{display:flex;justify-content:flex-end;margin-top:18px;}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:700;border:none;cursor:pointer;font-family:'Manrope',sans-serif;transition:all .15s;letter-spacing:.3px;}
.btn-amber{background:var(--amber);color:#0c0d10;box-shadow:0 3px 0 var(--amber-d);}
.btn-amber:hover{transform:translateY(-1px);box-shadow:0 4px 0 var(--amber-d),0 8px 16px var(--amber-g);}
.btn-outline{background:var(--card);color:var(--text);border:1.5px solid var(--border);}
.btn-outline:hover{border-color:var(--amber);color:var(--amber);}
.btn-outline-amber{background:var(--card);color:var(--amber);border:1.5px solid var(--amber);}
.btn-outline-amber:hover{background:var(--amber);color:#0c0d10;}
.btn-outline-red{background:var(--card);color:var(--red);border:1.5px solid var(--red);}
.btn-outline-red:hover{background:var(--red);color:#fff;}
.btn-sm{padding:6px 12px;font-size:12px;}

/* SITES */
.site-list{display:flex;flex-direction:column;gap:8px;margin-bottom:14px;}
.site-item{display:flex;align-items:center;justify-content:space-between;background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:11px 16px;transition:background .3s,border-color .3s;}
.site-item h4{font-size:14px;font-weight:600;color:var(--text);transition:color .3s;}
.site-item p{font-size:12px;color:var(--muted);margin-top:2px;}
.add-site-form{display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:end;background:var(--bg3);border:1.5px dashed var(--border);border-radius:10px;padding:14px;}
@media(max-width:600px){.add-site-form{grid-template-columns:1fr;}}

/* LOGO */
.logo-upload-box{border:2px dashed var(--border);border-radius:10px;padding:20px;text-align:center;cursor:pointer;transition:all .2s;background:var(--bg3);}
.logo-upload-box:hover{border-color:var(--amber);background:var(--amber-dim);}

/* MISC */
.spinner{display:inline-block;width:26px;height:26px;border:2.5px solid var(--border);border-top-color:var(--amber);border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
.loading{text-align:center;padding:48px;color:var(--faint);}
.toast{position:fixed;bottom:24px;right:24px;background:var(--card);border:1px solid var(--border);color:var(--text);padding:11px 18px;border-radius:10px;font-size:13px;font-weight:700;z-index:300;box-shadow:0 8px 24px var(--shadow2);animation:toastIn .2s ease;}
.toast.success{border-color:var(--green);color:var(--green);}
.toast.error{border-color:var(--red);color:var(--red);}
@keyframes toastIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
</style>
</head>
<body>

<svg style="display:none" aria-hidden="true">
  <symbol id="n2q" viewBox="0 0 40 40">
    <path d="M20 5h10l9 9v18a2 2 0 01-2 2H20a2 2 0 01-2-2V7a2 2 0 012-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M30 5v9h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="23" y1="20" x2="35" y2="20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <line x1="23" y1="25" x2="31" y2="25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <rect x="3" y="11" width="10" height="14" rx="5" fill="#f59e0b"/>
    <path d="M1 21.5a7 7 0 0014 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="8" y1="28.5" x2="8" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="5" y1="33" x2="11" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  </symbol>
</svg>

<!-- LOGIN -->
<div id="login-screen">
  <div class="login-card">
    <div class="login-top">
      <div class="login-logo"><svg width="22" height="22" viewBox="0 0 40 40"><use href="#n2q" color="white"/></svg>Note2Quote</div>
      <p>Sign in to manage your account</p>
    </div>
    <div class="login-body">
      <input type="text" id="ln" placeholder="Username or WhatsApp number" />
      <input type="password" id="lp" placeholder="Password" />
      <button class="btn-login" onclick="doLogin()">Sign in →</button>
      <p class="err" id="lerr"></p>
    </div>
  </div>
</div>

<!-- APP -->
<div id="app">
  <div class="app-shell">
    <div class="sidebar">
      <div class="sidebar-logo">
        <a href="/"><svg width="18" height="18" viewBox="0 0 40 40"><use href="#n2q" color="white"/></svg>Note2Quote</a>
        <div class="sidebar-company" id="sb-company"></div>
      </div>
      <nav class="sidebar-nav">
        <div class="nav-section">
          <div class="nav-section-label">Main</div>
          <a href="/dashboard" class="nav-item"><span class="nav-icon">📊</span>Dashboard</a>
        </div>
        <div class="nav-section">
          <div class="nav-section-label">Settings</div>
          <button class="nav-item active"><span class="nav-icon">⚙️</span>My Account</button>
        </div>
      </nav>
      <div class="sidebar-bottom">
        <div class="sidebar-actions">
          <button class="sidebar-btn" onclick="toggleTheme()" id="theme-btn">🌙 Theme</button>
          <button class="sidebar-btn" onclick="logout()">Sign out</button>
        </div>
        <div class="sidebar-user">
          <div class="sidebar-avatar" id="sb-avatar">?</div>
          <div>
            <div class="sidebar-user-name" id="sb-name">Loading...</div>
            <div class="sidebar-user-role">Boss</div>
          </div>
        </div>
      </div>
    </div>

    <div class="main">
      <div class="topbar">
        <div class="topbar-title">My Account</div>
        <div class="topbar-right">
          <button class="theme-btn" onclick="toggleTheme()" id="theme-btn2">🌙</button>
        </div>
      </div>
      <div class="page-content" id="page-content">
        <div class="loading"><div class="spinner"></div></div>
      </div>
    </div>
  </div>
</div>

<script>
var STATE={pw:'',num:'',data:null};

function toggleTheme(){
  var h=document.documentElement;
  var n=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',n);
  var icon=n==='dark'?'☀️':'🌙';
  var b1=document.getElementById('theme-btn');var b2=document.getElementById('theme-btn2');
  if(b1)b1.textContent=icon+' Theme';if(b2)b2.textContent=icon;
  localStorage.setItem('n2q-theme',n);
}
var saved=localStorage.getItem('n2q-theme');
if(saved){var tb=document.getElementById('theme-btn');var tb2=document.getElementById('theme-btn2');var icon=saved==='dark'?'☀️':'🌙';if(tb)tb.textContent=icon+' Theme';if(tb2)tb2.textContent=icon;}

async function doLogin(){
  var login=document.getElementById('ln').value.trim();
  var pw=document.getElementById('lp').value.trim();
  if(!login||!pw){document.getElementById('lerr').textContent='Fill in both fields.';return;}
  var r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({login:login,password:pw})});
  var d=await r.json();
  if(d.ok){STATE.pw=d.session_token||pw;STATE.num=d.whatsapp||login;localStorage.setItem('ss_password',STATE.pw);localStorage.setItem('ss_number',STATE.num);showApp();}
  else{document.getElementById('lerr').textContent=d.error||'Wrong credentials.';}
}

function logout(){localStorage.removeItem('ss_password');localStorage.removeItem('ss_number');localStorage.removeItem('ss_magic');STATE={pw:'',num:'',data:null};document.getElementById('login-screen').style.display='flex';document.getElementById('app').style.display='none';}

function apiFetch(url,opts){
  opts=opts||{};
  return fetch(url,Object.assign({},opts,{headers:Object.assign({'Content-Type':'application/json','X-Dashboard-Password':STATE.pw},opts.headers||{})}));
}

async function showApp(){
  document.getElementById('login-screen').style.display='none';
  document.getElementById('app').style.display='block';
  await loadAccount();
}

async function loadAccount(){
  var r=await apiFetch('/api/account?number='+encodeURIComponent(STATE.num));
  var d=await r.json();
  STATE.data=d;
  var c=d.company||{};
  var name=c.company_name||'My Company';
  document.getElementById('sb-company').textContent=name;
  document.getElementById('sb-name').textContent=name;
  document.getElementById('sb-avatar').textContent=name.charAt(0).toUpperCase();
  renderAccount(d);
}

function renderAccount(d){
  var c=d.company||{};var ps=d.projects||[];var s=d.stripe||{};
  var subStatus=s.status||'unknown';
  var dotClass=subStatus==='active'?'dot-active':subStatus==='trialing'?'dot-trialing':'dot-canceled';
  var subLabel=subStatus==='active'?'Active subscription':subStatus==='trialing'?'Free trial':subStatus;
  var nextDate=s.next_billing?new Date(s.next_billing*1000).toLocaleDateString('en-GB'):'—';
  var trialDate=s.trial_end?new Date(s.trial_end*1000).toLocaleDateString('en-GB'):null;

  var sitesHtml=ps.length?ps.map(function(p){return '<div class="site-item"><div><h4>📍 '+p.site_name+'</h4><p>'+(p.client_name||'No client name set')+'</p></div><button class="btn btn-outline btn-sm" onclick="removeSite('+p.id+',\\''+p.site_name+'\\')">Remove</button></div>';}).join(''):'<p style="color:var(--muted);font-size:14px;margin-bottom:12px">No sites yet.</p>';

  document.getElementById('page-content').innerHTML = `
    <div class="section">
      <div class="section-header"><div class="section-title">Subscription</div></div>
      <div class="section-body">
        <div class="sub-card">
          <div style="display:flex;align-items:center;gap:12px;">
            <div class="sub-dot ${dotClass}"></div>
            <div>
              <div class="sub-name">Note2Quote Monthly — £49/month</div>
              <div class="sub-detail">${subLabel}${trialDate?' · Trial ends '+trialDate:''}${subStatus==='active'?' · Next billing '+nextDate:''}</div>
            </div>
          </div>
        </div>
        <div class="sub-actions">
          ${s.billing_url?`<a href="${s.billing_url}" target="_blank" class="btn btn-outline">Manage billing →</a>`:''}
          <button class="btn btn-outline-amber" onclick="pauseSubscription()">⏸ Pause</button>
          <button class="btn btn-outline-red" onclick="cancelSubscription()">✕ Cancel</button>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-header"><div class="section-title">Company Logo</div></div>
      <div class="section-body">
        <p style="font-size:13px;color:var(--muted);margin-bottom:14px;font-weight:300">Appears on all branded PDF documents sent to clients.</p>
        <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap;">
          ${c.logo_url?`<img src="${c.logo_url}" style="max-height:56px;max-width:180px;border:1px solid var(--border);border-radius:8px;padding:8px;background:white" alt="Logo" />`:'<div style="width:100px;height:56px;border:2px dashed var(--border);border-radius:8px;display:flex;align-items:center;justify-content:center;color:var(--faint);font-size:12px">No logo</div>'}
          <div>
            <input type="file" id="logo-file" accept="image/*" style="display:none" onchange="uploadLogo(this)" />
            <button class="btn btn-outline" onclick="document.getElementById('logo-file').click()">${c.logo_url?'Change logo':'Upload logo'}</button>
            <p style="font-size:11px;color:var(--faint);margin-top:6px">PNG, JPG or SVG · Max 2MB</p>
          </div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-header"><div class="section-title">Company Details</div></div>
      <div class="section-body">
        <div class="form-grid">
          <div class="form-group"><label>Company Name</label><input type="text" id="f-name" value="${c.company_name||''}" /></div>
          <div class="form-group"><label>Email Address</label><input type="email" id="f-email" value="${c.email||''}" /></div>
          <div class="form-group"><label>Phone</label><input type="tel" id="f-phone" value="${c.phone||''}" /></div>
          <div class="form-group"><label>VAT Number</label><input type="text" id="f-vat" value="${c.vat_number||''}" /></div>
          <div class="form-group" style="grid-column:1/-1"><label>Company Address</label><input type="text" id="f-address" value="${c.address||''}" /></div>
          <div class="form-group"><label>Brand Colour</label><div class="color-row"><input type="color" id="f-color-picker" value="${c.primary_color||'#f59e0b'}" oninput="document.getElementById('f-color-hex').value=this.value" /><input type="text" id="f-color-hex" value="${c.primary_color||'#f59e0b'}" oninput="document.getElementById('f-color-picker').value=this.value" /></div></div>
        </div>
        <div class="form-actions"><button class="btn btn-amber" onclick="saveCompany()">Save details</button></div>
      </div>
    </div>

    <div class="section">
      <div class="section-header"><div class="section-title">Dashboard Login</div></div>
      <div class="section-body">
        <p style="font-size:13px;color:var(--muted);margin-bottom:16px;font-weight:300">Set a username and personal password so you don't need to type your phone number each time.</p>
        <div class="form-grid">
          <div class="form-group"><label>Username</label><input type="text" id="f-username" value="${c.username||''}" placeholder="e.g. aaron.green" /><div class="hint">Lowercase, no spaces</div></div>
          <div class="form-group"><label>New Password <span style="font-weight:300;opacity:.5;text-transform:none">(leave blank to keep current)</span></label><input type="password" id="f-new-pw" placeholder="Set a new password" /></div>
        </div>
        <div class="form-actions"><button class="btn btn-amber" onclick="saveLoginSettings()">Save login settings</button></div>
      </div>
    </div>

    <div class="section">
      <div class="section-header"><div class="section-title">My Sites</div></div>
      <div class="section-body">
        <div class="site-list">${sitesHtml}</div>
        <div class="add-site-form">
          <div class="form-group"><label>Site Name</label><input type="text" id="new-site" placeholder="Brookfield Site" /></div>
          <div class="form-group"><label>Client Name</label><input type="text" id="new-client" placeholder="Brookfield Developments" /></div>
          <button class="btn btn-amber" onclick="addSite()">Add</button>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-header"><div class="section-title">WhatsApp Bot</div></div>
      <div class="section-body">
        <p style="font-size:13px;color:var(--muted);margin-bottom:14px;font-weight:300">Your bot is connected to this WhatsApp number. Share the bot number with your team so they can log site activity.</p>
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:14px 18px;transition:background .3s,border-color .3s;">
          <div style="font-size:10px;color:var(--faint);font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">Your WhatsApp number</div>
          <div style="font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:1px;color:var(--text)">${(c.whatsapp_number||'—').replace('whatsapp:','')}</div>
        </div>
      </div>
    </div>`;
}

async function saveCompany(){
  var payload={company_name:document.getElementById('f-name').value.trim(),email:document.getElementById('f-email').value.trim(),phone:document.getElementById('f-phone').value.trim(),vat_number:document.getElementById('f-vat').value.trim(),address:document.getElementById('f-address').value.trim(),primary_color:document.getElementById('f-color-hex').value.trim()};
  var r=await apiFetch('/api/account/company?number='+encodeURIComponent(STATE.num),{method:'PATCH',body:JSON.stringify(payload)});
  var d=await r.json();
  if(d.ok)showToast('✓ Details saved!','success');else showToast('❌ '+(d.error||'Failed'),'error');
}

async function saveLoginSettings(){
  var username=document.getElementById('f-username').value.trim();
  var newPw=document.getElementById('f-new-pw').value.trim();
  if(!username){showToast('Please enter a username','error');return;}
  var payload={username};if(newPw)payload.dashboard_password=newPw;
  var r=await apiFetch('/api/account/login?number='+encodeURIComponent(STATE.num),{method:'PATCH',body:JSON.stringify(payload)});
  var d=await r.json();
  if(d.ok)showToast('✓ Login settings saved!','success');else showToast('❌ '+(d.error||'Failed'),'error');
}

async function uploadLogo(input){
  var file=input.files[0];if(!file)return;
  if(file.size>2*1024*1024){showToast('File too large. Max 2MB.','error');return;}
  showToast('Uploading...','');
  var r=await apiFetch('/api/account/logo?number='+encodeURIComponent(STATE.num),{method:'POST',headers:{'X-Dashboard-Password':STATE.pw,'Content-Type':file.type},body:file});
  var d=await r.json();
  if(d.ok){showToast('✓ Logo uploaded!','success');await loadAccount();}
  else showToast('❌ '+(d.error||'Failed'),'error');
}

async function addSite(){
  var site=document.getElementById('new-site').value.trim();
  var client=document.getElementById('new-client').value.trim();
  if(!site){showToast('Enter a site name','error');return;}
  var r=await apiFetch('/api/account/sites?number='+encodeURIComponent(STATE.num),{method:'POST',body:JSON.stringify({site_name:site,client_name:client})});
  var d=await r.json();
  if(d.ok){showToast('✓ Site added!','success');document.getElementById('new-site').value='';document.getElementById('new-client').value='';await loadAccount();}
  else showToast('❌ '+(d.error||'Failed'),'error');
}

async function removeSite(id,name){
  if(!confirm('Remove "'+name+'" from active sites?'))return;
  var r=await apiFetch('/api/account/sites/'+id,{method:'DELETE'});
  var d=await r.json();
  if(d.ok){showToast('Site removed','success');await loadAccount();}
  else showToast('❌ '+(d.error||'Failed'),'error');
}

function pauseSubscription(){
  var s=STATE.data&&STATE.data.stripe;
  if(s&&s.billing_url){window.open(s.billing_url,'_blank');}
  else{showToast('Contact support to pause your subscription','');}
}

function cancelSubscription(){
  if(!confirm('Are you sure you want to cancel? You keep access until the end of your billing period.'))return;
  var s=STATE.data&&STATE.data.stripe;
  if(s&&s.billing_url){window.open(s.billing_url,'_blank');}
  else{showToast('Contact support to cancel your subscription','');}
}

function showToast(msg,type){
  var t=document.createElement('div');t.className='toast '+(type||'');t.textContent=msg;
  document.body.appendChild(t);setTimeout(function(){t.remove();},3000);
}

window.addEventListener('DOMContentLoaded',function(){
  var pw=localStorage.getItem('ss_password');var num=localStorage.getItem('ss_number');
  if(pw&&num){STATE.pw=pw;STATE.num=num;showApp();}
});
document.addEventListener('keydown',function(e){if(e.key==='Enter'&&document.getElementById('login-screen').style.display!=='none')doLogin();});
</script>
</body>
</html>
'''
ADMIN_HTML     = '''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Note2Quote — Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Manrope:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--amber:#f59e0b;--amber-dark:#d97706;--amber-dim:rgba(245,158,11,0.1);--amber-glow:rgba(245,158,11,0.15);}
[data-theme="dark"]{--bg:#0d0f14;--bg-2:#161820;--bg-3:#1e2028;--text:#f1f0ed;--text-2:#d1cdc7;--muted:#9ca3af;--faint:#6b7280;--border:rgba(255,255,255,0.08);--card:#161820;--green:#22c55e;--red:#ef4444;}
[data-theme="light"]{--bg:#f8f7f4;--bg-2:#ffffff;--bg-3:#f0ede8;--text:#0d0f14;--text-2:#374151;--muted:#6b7280;--faint:#9ca3af;--border:#e5e2dd;--card:#ffffff;--green:#16a34a;--red:#dc2626;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Manrope',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased;transition:background .3s,color .3s;}

/* Login */
#login-screen{min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);}
.login-box{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:48px 40px;width:100%;max-width:380px;}
.login-logo{font-family:'Bebas Neue',sans-serif;font-size:26px;letter-spacing:2px;color:var(--text);margin-bottom:4px;display:flex;align-items:center;gap:10px;}
.login-box p{font-size:13px;color:var(--muted);margin-bottom:24px;font-weight:300;}
.login-box input{width:100%;padding:11px 14px;border:1px solid var(--border);border-radius:8px;font-size:14px;font-family:'Manrope',sans-serif;outline:none;margin-bottom:12px;background:var(--bg-3);color:var(--text);transition:border .15s;}
.login-box input:focus{border-color:var(--amber);}
.btn{display:inline-flex;align-items:center;gap:6px;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:700;border:none;cursor:pointer;font-family:'Manrope',sans-serif;transition:all .15s;letter-spacing:0.3px;}
.btn-primary{background:var(--amber);color:#0d0f14;width:100%;justify-content:center;padding:13px;font-size:14px;}
.btn-primary:hover{background:#fbbf24;}
.btn-sm{padding:6px 12px;font-size:12px;}
.btn-outline{background:transparent;color:var(--muted);border:1px solid var(--border);}
.btn-outline:hover{border-color:var(--amber);color:var(--amber);}
.btn-red{background:rgba(239,68,68,0.1);color:var(--red);border:1px solid rgba(239,68,68,0.2);font-size:11px;padding:5px 10px;}
.btn-red:hover{background:var(--red);color:white;}
.btn-ghost-amber{background:rgba(245,158,11,0.1);color:#d97706;border:none;font-size:11px;padding:5px 10px;border-radius:6px;cursor:pointer;font-family:Manrope,sans-serif;font-weight:700;}
.btn-ghost-amber:hover{background:#f59e0b;color:#0d0f14;}
.error-msg{color:var(--red);font-size:12px;margin-top:8px;}

/* App */
#app{display:none;}
header{background:var(--bg-2);border-bottom:1px solid var(--border);padding:0 28px;height:60px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;transition:background .3s,border-color .3s;}
.header-logo{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:2px;color:var(--text);display:flex;align-items:center;gap:8px;}
.header-badge{background:var(--amber-dim);border:1px solid rgba(245,158,11,0.25);color:var(--amber);font-size:10px;font-weight:700;padding:3px 10px;border-radius:100px;letter-spacing:1px;text-transform:uppercase;margin-left:8px;}
.header-right{display:flex;align-items:center;gap:8px;}
.theme-toggle{width:32px;height:32px;border-radius:7px;border:1px solid var(--border);background:var(--card);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;transition:all .15s;}
.theme-toggle:hover{border-color:var(--amber);}
.logout-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:6px 14px;border-radius:7px;cursor:pointer;font-size:12px;font-family:'Manrope',sans-serif;font-weight:600;transition:all .15s;}
.logout-btn:hover{border-color:var(--red);color:var(--red);}

main{max-width:1300px;margin:0 auto;padding:28px 20px;}

/* KPI cards */
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:24px;transition:background .3s,border-color .3s;}
@media(max-width:700px){.kpi-grid{grid-template-columns:repeat(2,1fr);}}
.kpi{background:var(--card);padding:22px 24px;transition:background .3s;}
.kpi-label{font-size:10px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;}
.kpi-value{font-family:'Bebas Neue',sans-serif;font-size:38px;letter-spacing:1px;color:var(--text);line-height:1;transition:color .3s;}
.kpi-value.amber{color:var(--amber);}
.kpi-value.green{color:var(--green);}
.kpi-value.red{color:var(--red);}
.kpi-sub{font-size:11px;color:var(--faint);margin-top:4px;font-weight:400;}

/* Section */
.section-row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;}
@media(max-width:900px){.section-row{grid-template-columns:1fr;}}
.panel{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden;transition:background .3s,border-color .3s;}
.panel-full{grid-column:1/-1;}
.panel-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;transition:border-color .3s;}
.panel-title{font-family:'Bebas Neue',sans-serif;font-size:18px;letter-spacing:1px;color:var(--text);transition:color .3s;}
.panel-body{padding:20px;}

/* System health */
.health-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;}
.health-item{display:flex;align-items:center;justify-content:space-between;background:var(--bg-3);border:1px solid var(--border);border-radius:10px;padding:12px 16px;transition:background .3s,border-color .3s;}
.health-name{font-size:13px;font-weight:600;color:var(--text-2);transition:color .3s;}
.health-status{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.dot-green{background:var(--green);box-shadow:0 0 6px rgba(34,197,94,0.4);}
.dot-amber{background:var(--amber);box-shadow:0 0 6px var(--amber-glow);}
.dot-red{background:var(--red);box-shadow:0 0 6px rgba(239,68,68,0.4);}

/* Cost breakdown */
.cost-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);transition:border-color .3s;}
.cost-row:last-child{border-bottom:none;}
.cost-service{font-size:13px;font-weight:500;color:var(--text-2);transition:color .3s;}
.cost-amount{font-family:'Bebas Neue',sans-serif;font-size:18px;letter-spacing:1px;color:var(--amber);}
.cost-note{font-size:11px;color:var(--faint);}

/* Churn alerts */
.alert-item{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border);transition:border-color .3s;}
.alert-item:last-child{border-bottom:none;}
.alert-company{font-size:13px;font-weight:600;color:var(--text);transition:color .3s;}
.alert-detail{font-size:11px;color:var(--faint);margin-top:2px;}
.alert-days{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:1px;color:var(--red);}
.alert-badge{font-size:10px;font-weight:700;padding:3px 8px;border-radius:5px;}
.alert-badge-red{background:rgba(239,68,68,0.1);color:var(--red);}
.alert-badge-amber{background:var(--amber-dim);color:var(--amber-dark);}

/* Customer table */
table{width:100%;border-collapse:collapse;font-size:13px;}
th{padding:9px 14px;text-align:left;font-size:10px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1.5px;background:var(--bg-3);border-bottom:1px solid var(--border);transition:background .3s,border-color .3s,color .3s;}
td{padding:11px 14px;border-bottom:1px solid var(--border);color:var(--text-2);transition:border-color .3s,color .3s;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:var(--bg-3);cursor:pointer;}
.status-badge{display:inline-block;font-size:10px;font-weight:700;padding:3px 8px;border-radius:5px;text-transform:uppercase;letter-spacing:0.5px;}
.status-active{background:rgba(34,197,94,0.1);color:var(--green);}
.status-trialing{background:var(--amber-dim);color:var(--amber-dark);}
.status-canceled{background:rgba(239,68,68,0.1);color:var(--red);}
.status-unknown{background:var(--bg-3);color:var(--faint);}
.usage-bar{width:80px;height:6px;background:var(--border);border-radius:3px;overflow:hidden;}
.usage-fill{height:100%;background:var(--amber);border-radius:3px;transition:width .5s;}

/* Modal */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:flex-start;justify-content:center;z-index:200;padding:40px 20px;overflow-y:auto;}
.modal{background:var(--card);border-radius:16px;padding:32px;width:100%;max-width:680px;border:1px solid var(--border);transition:background .3s,border-color .3s;}
.modal h3{font-family:'Bebas Neue',sans-serif;font-size:24px;letter-spacing:1.5px;color:var(--text);margin-bottom:4px;}
.modal p{font-size:13px;color:var(--muted);margin-bottom:20px;font-weight:300;}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px;}
.info-item label{display:block;font-size:10px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;}
.info-item span{font-size:14px;font-weight:600;color:var(--text);transition:color .3s;}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:24px;}
.mini-table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px;}
.mini-table th{background:var(--bg-3);padding:7px 10px;text-align:left;font-size:9px;color:var(--faint);text-transform:uppercase;letter-spacing:1px;transition:background .3s;}
.mini-table td{padding:7px 10px;border-bottom:1px solid var(--border);color:var(--text-2);transition:border-color .3s,color .3s;}

/* Misc */
.loading{text-align:center;padding:60px;color:var(--faint);}
.spinner{display:inline-block;width:26px;height:26px;border:2.5px solid var(--border);border-top-color:var(--amber);border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
.toast{position:fixed;bottom:24px;right:24px;background:var(--card);border:1px solid var(--border);color:var(--text);padding:12px 20px;border-radius:10px;font-size:13px;z-index:300;box-shadow:0 8px 24px rgba(0,0,0,.2);font-weight:600;}
.toast.success{border-color:var(--green);color:var(--green);}
.toast.error{border-color:var(--red);color:var(--red);}
.empty{text-align:center;padding:32px;color:var(--faint);font-size:13px;}
.mrr-target{font-size:11px;color:var(--faint);margin-top:4px;}
.progress-bar{height:4px;background:var(--border);border-radius:2px;margin-top:8px;overflow:hidden;}
.progress-fill{height:100%;background:var(--amber);border-radius:2px;transition:width .8s;}
</style>
</head>
<body>

<svg style="display:none">
  <symbol id="n2q-logo" viewBox="0 0 40 40">
    <path d="M20 5h10l9 9v18a2 2 0 01-2 2H20a2 2 0 01-2-2V7a2 2 0 012-2z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M30 5v9h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="23" y1="20" x2="35" y2="20" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <line x1="23" y1="25" x2="31" y2="25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    <rect x="3" y="11" width="10" height="14" rx="5" fill="#f59e0b"/>
    <path d="M1 21.5a7 7 0 0014 0" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="8" y1="28.5" x2="8" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="5" y1="33" x2="11" y2="33" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
  </symbol>
</svg>

<!-- LOGIN -->
<div id="login-screen">
  <div class="login-box">
    <div class="login-logo">
      <svg width="24" height="24" viewBox="0 0 40 40"><use href="#n2q-logo" color="currentColor"/></svg>
      Note2Quote
      <span class="header-badge">Admin</span>
    </div>
    <p>Founder access only</p>
    <input type="password" id="admin-password" placeholder="Admin password" />
    <button class="btn btn-primary" onclick="doLogin()">Sign in →</button>
    <p class="error-msg" id="login-error"></p>
  </div>
</div>

<!-- APP -->
<div id="app">
  <header>
    <div style="display:flex;align-items:center;">
      <div class="header-logo">
        <svg width="20" height="20" viewBox="0 0 40 40"><use href="#n2q-logo" color="currentColor"/></svg>
        Note2Quote
      </div>
      <span class="header-badge">Admin</span>
    </div>
    <div class="header-right">
      <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn">☀️</button>
      <button class="logout-btn" onclick="logout()">Sign out</button>
    </div>
  </header>

  <main>
    <div id="content"><div class="loading"><div class="spinner"></div></div></div>
  </main>
</div>

<!-- CUSTOMER MODAL -->
<div class="modal-overlay" id="customer-modal" style="display:none">
  <div class="modal">
    <h3 id="modal-name">Company</h3>
    <p id="modal-sub"></p>
    <div class="info-grid" id="modal-info"></div>
    <div style="margin-bottom:14px">
      <div style="font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Active Sites</div>
      <div id="modal-sites"></div>
    </div>
    <div>
      <div style="font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Recent Logs</div>
      <div id="modal-logs"></div>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost-amber" onclick="sendWelcome()">📱 Send Welcome</button>
      <button class="btn btn-red btn-sm" id="cancel-sub-btn" onclick="cancelSubscription()" style="display:none">Cancel Subscription</button>
      <button class="btn btn-outline btn-sm" onclick="closeModal()">Close</button>
    </div>
  </div>
</div>

<script>
let STATE = { password: "", data: null, currentCustomer: null };

async function doLogin(){
  const pw = document.getElementById("admin-password").value.trim();
  const r  = await fetch("/api/admin/auth",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:pw})});
  const d  = await r.json();
  if(d.ok){ STATE.password=pw; localStorage.setItem("n2q_admin_pw",pw); showApp(); }
  else { document.getElementById("login-error").textContent="Wrong password."; }
}

function logout(){ localStorage.removeItem("n2q_admin_pw"); location.reload(); }

function apiFetch(url,opts={}){
  return fetch(url,{...opts,headers:{"Content-Type":"application/json","X-Admin-Password":STATE.password,...(opts.headers||{})}});
}

async function showApp(){
  document.getElementById("login-screen").style.display="none";
  document.getElementById("app").style.display="block";
  await loadOverview();
}

async function loadOverview(){
  document.getElementById("content").innerHTML=`<div class="loading"><div class="spinner"></div></div>`;
  const r = await apiFetch("/api/admin/overview");
  const d = await r.json();
  STATE.data = d;
  renderOverview(d);
}

function renderOverview(d){
  const customers  = d.customers || [];
  const mrr        = d.mrr || 0;
  const mrrTarget  = 5000;
  const mrrPct     = Math.min(100, Math.round((mrr/mrrTarget)*100));

  // Churn risk: no logs in 7+ days
  const now = Date.now();
  const churnRisk = customers.filter(c => {
    if(!c.last_log_date) return c.total_logs === 0;
    const days = Math.floor((now - new Date(c.last_log_date).getTime()) / 86400000);
    return days >= 7;
  }).sort((a,b) => (b.days_inactive||99) - (a.days_inactive||99));

  // Sort customers by usage
  const byUsage = [...customers].sort((a,b) => b.total_logs - a.total_logs);

  // Estimated costs (rough)
  const aiCostPerLog    = 0.001; // ~£0.001 per Claude call
  const voiceCostPerLog = 0.0006; // ~£0.0006 per Groq transcription
  const totalLogs       = d.total_logs || 0;
  const estimatedAI     = (totalLogs * aiCostPerLog).toFixed(2);
  const estimatedVoice  = (totalLogs * voiceCostPerLog * 0.3).toFixed(2); // ~30% voice
  const estimatedTotal  = (parseFloat(estimatedAI) + parseFloat(estimatedVoice) + 5).toFixed(2); // +£5 infra

  const statusBadge = s => {
    const map = {active:'status-active',trialing:'status-trialing',canceled:'status-canceled'};
    return `<span class="status-badge ${map[s]||'status-unknown'}">${s||'unknown'}</span>`;
  };

  let customerRows = byUsage.map(c => {
    const maxLogs = Math.max(...customers.map(x=>x.total_logs),1);
    const pct     = Math.round((c.total_logs/maxLogs)*100);
    const date    = c.created_at ? new Date(c.created_at).toLocaleDateString("en-GB") : "—";
    return `<tr onclick="openCustomer(${c.id})">
      <td><strong>${c.company_name}</strong></td>
      <td>${c.email||"—"}</td>
      <td>${statusBadge(c.stripe_status)}</td>
      <td>${c.sites}</td>
      <td>
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="font-family:'Bebas Neue',sans-serif;font-size:16px;letter-spacing:1px;color:var(--amber)">${c.total_logs}</span>
          <div class="usage-bar"><div class="usage-fill" style="width:${pct}%"></div></div>
        </div>
      </td>
      <td>${c.sent_docs}</td>
      <td>${date}</td>
    </tr>`;
  }).join("");

  let churnRows = churnRisk.length ? churnRisk.slice(0,5).map(c => `
    <div class="alert-item">
      <div>
        <div class="alert-company">${c.company_name}</div>
        <div class="alert-detail">${c.email||""} · ${c.total_logs} total logs</div>
      </div>
      <div style="text-align:right">
        <div class="alert-days">${c.days_inactive||"7+"}D</div>
        <span class="alert-badge ${(c.days_inactive||7)>14?'alert-badge-red':'alert-badge-amber'}">${(c.days_inactive||7)>14?'HIGH RISK':'AT RISK'}</span>
      </div>
    </div>`).join("") : `<div class="empty">✅ No churn risk detected</div>`;

  document.getElementById("content").innerHTML = `
    <!-- KPIs -->
    <div class="kpi-grid">
      <div class="kpi">
        <div class="kpi-label">Monthly Revenue</div>
        <div class="kpi-value amber">£${mrr.toFixed(0)}</div>
        <div class="kpi-sub">MRR · ${d.active_count||0} active</div>
        <div class="mrr-target">Target: £${mrrTarget.toLocaleString()}</div>
        <div class="progress-bar"><div class="progress-fill" style="width:${mrrPct}%"></div></div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Total Customers</div>
        <div class="kpi-value">${d.total_customers||0}</div>
        <div class="kpi-sub">${d.trial_count||0} trialing · ${d.active_count||0} active</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Total Logs</div>
        <div class="kpi-value">${d.total_logs||0}</div>
        <div class="kpi-sub">${d.total_sent||0} converted to docs</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Churn Risk</div>
        <div class="kpi-value ${churnRisk.length>0?'red':'green'}">${churnRisk.length}</div>
        <div class="kpi-sub">Inactive 7+ days</div>
      </div>
    </div>

    <div class="section-row">
      <!-- System Health -->
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">System Health</div>
          <button class="btn btn-outline btn-sm" onclick="checkHealth()">Refresh</button>
        </div>
        <div class="panel-body">
          <div class="health-grid" id="health-grid">
            <div class="health-item"><span class="health-name">Railway (App)</span><span class="health-status"><span class="dot dot-green"></span>Online</span></div>
            <div class="health-item"><span class="health-name">Supabase (DB)</span><span class="health-status"><span class="dot dot-green"></span>Online</span></div>
            <div class="health-item"><span class="health-name">Anthropic (AI)</span><span class="health-status" id="h-anthropic"><span class="dot dot-amber"></span>Checking...</span></div>
            <div class="health-item"><span class="health-name">Groq (Voice)</span><span class="health-status" id="h-groq"><span class="dot dot-amber"></span>Checking...</span></div>
            <div class="health-item"><span class="health-name">Twilio (WhatsApp)</span><span class="health-status" id="h-twilio"><span class="dot dot-amber"></span>Checking...</span></div>
            <div class="health-item"><span class="health-name">Resend (Email)</span><span class="health-status" id="h-resend"><span class="dot dot-amber"></span>Checking...</span></div>
          </div>
        </div>
      </div>

      <!-- Cost Tracking -->
      <div class="panel">
        <div class="panel-header"><div class="panel-title">Est. Monthly Costs</div></div>
        <div class="panel-body">
          <div class="cost-row">
            <div><div class="cost-service">Railway Hosting</div><div class="cost-note">Hobby plan</div></div>
            <div class="cost-amount">£5</div>
          </div>
          <div class="cost-row">
            <div><div class="cost-service">Anthropic (AI)</div><div class="cost-note">~${totalLogs} API calls</div></div>
            <div class="cost-amount">£${estimatedAI}</div>
          </div>
          <div class="cost-row">
            <div><div class="cost-service">Groq (Voice)</div><div class="cost-note">~${Math.round(totalLogs*0.3)} transcriptions</div></div>
            <div class="cost-amount">£${estimatedVoice}</div>
          </div>
          <div class="cost-row">
            <div><div class="cost-service">Twilio (WhatsApp)</div><div class="cost-note">Per message</div></div>
            <div class="cost-amount">~£2</div>
          </div>
          <div class="cost-row" style="border-top:2px solid var(--amber);margin-top:4px;padding-top:12px;">
            <div><div class="cost-service" style="font-weight:700;color:var(--text)">Total Est. Cost</div></div>
            <div class="cost-amount">£${estimatedTotal}</div>
          </div>
          <div style="margin-top:12px;padding:10px 14px;background:var(--amber-dim);border-radius:8px;font-size:12px;color:var(--amber-dark);">
            💰 Est. margin: <strong>£${(mrr - parseFloat(estimatedTotal)).toFixed(0)}/mo</strong> (${mrr > 0 ? Math.round(((mrr-parseFloat(estimatedTotal))/mrr)*100) : 0}%)
          </div>
        </div>
      </div>
    </div>

    <div class="section-row">
      <!-- Churn Alerts -->
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">⚠️ Churn Alerts</div>
          <span style="font-size:11px;color:var(--faint)">Inactive 7+ days</span>
        </div>
        <div class="panel-body">
          ${churnRows}
        </div>
      </div>

      <!-- Revenue breakdown -->
      <div class="panel">
        <div class="panel-header"><div class="panel-title">Revenue Breakdown</div></div>
        <div class="panel-body">
          <div class="cost-row">
            <div><div class="cost-service">Active subscriptions</div><div class="cost-note">${d.active_count||0} × £49/mo</div></div>
            <div class="cost-amount">£${((d.active_count||0)*49).toFixed(0)}</div>
          </div>
          <div class="cost-row">
            <div><div class="cost-service">Trialing</div><div class="cost-note">${d.trial_count||0} converting soon</div></div>
            <div class="cost-amount" style="color:var(--amber-dark)">+£${((d.trial_count||0)*49).toFixed(0)}</div>
          </div>
          <div class="cost-row" style="border-top:2px solid var(--amber);margin-top:4px;padding-top:12px;">
            <div><div class="cost-service" style="font-weight:700;color:var(--text)">Potential MRR</div></div>
            <div class="cost-amount">£${(((d.active_count||0)+(d.trial_count||0))*49).toFixed(0)}</div>
          </div>
          <div style="margin-top:16px;">
            <div style="font-size:10px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">To reach £5,000 MRR</div>
            <div style="font-size:13px;color:var(--text-2)">Need <strong style="color:var(--amber)">${Math.max(0,Math.ceil((5000-mrr)/49))}</strong> more paying customers</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Customer Table -->
    <div class="panel panel-full">
      <div class="panel-header">
        <div class="panel-title">All Customers</div>
        <span style="font-size:11px;color:var(--faint)">Click to view details</span>
      </div>
      <div style="overflow-x:auto;">
        ${customers.length ? `<table>
          <thead><tr>
            <th>Company</th><th>Email</th><th>Status</th>
            <th>Sites</th><th>Total Logs</th><th>Docs Sent</th><th>Joined</th>
          </tr></thead>
          <tbody>${customerRows}</tbody>
        </table>` : `<div class="empty">No customers yet. Share your landing page to get started!</div>`}
      </div>
    </div>`;

  // Check health async
  checkHealth();
}

async function checkHealth(){
  const r = await apiFetch("/api/admin/health");
  const d = await r.json();
  const map = {
    "h-anthropic": d.anthropic,
    "h-groq":      d.groq,
    "h-twilio":    d.twilio,
    "h-resend":    d.resend,
  };
  for(const [id, status] of Object.entries(map)){
    const el = document.getElementById(id);
    if(el){
      const cls  = status ? "dot-green" : "dot-red";
      const text = status ? "Online" : "Issue";
      el.innerHTML = `<span class="dot ${cls}"></span>${text}`;
    }
  }
}

async function openCustomer(id){
  const r = await apiFetch(`/api/admin/customer/${id}`);
  const d = await r.json();
  STATE.currentCustomer = d;
  const c = d.company;

  document.getElementById("modal-name").textContent = c.company_name;
  document.getElementById("modal-sub").textContent  = `${c.email||""} · ${c.phone||""} · Joined ${new Date(c.created_at).toLocaleDateString("en-GB")}`;

  document.getElementById("modal-info").innerHTML = `
    <div class="info-item"><label>Trade</label><span>${c.trade||"—"}</span></div>
    <div class="info-item"><label>WhatsApp</label><span>${c.whatsapp_number||"—"}</span></div>
    <div class="info-item"><label>VAT</label><span>${c.vat_number||"—"}</span></div>
    <div class="info-item"><label>Address</label><span>${c.address||"—"}</span></div>`;

  const sites = d.projects||[];
  document.getElementById("modal-sites").innerHTML = sites.length
    ? sites.map(s=>`<span style="display:inline-block;background:var(--amber-dim);border:1px solid rgba(245,158,11,0.2);color:var(--amber-dark);font-size:11px;font-weight:700;padding:3px 9px;border-radius:5px;margin:3px 3px 0 0">📍 ${s.site_name}</span>`).join("")
    : `<span style="color:var(--faint);font-size:13px">No sites added yet</span>`;

  const logs = (d.logs||[]).slice(0,8);
  document.getElementById("modal-logs").innerHTML = logs.length ? `
    <table class="mini-table">
      <thead><tr><th>Type</th><th>Description</th><th>Site</th><th>Status</th><th>Date</th></tr></thead>
      <tbody>${logs.map(l=>`<tr>
        <td>${l.type||"—"}</td>
        <td>${l.description||"—"}</td>
        <td>${l.site_name||"—"}</td>
        <td>${l.status}</td>
        <td>${new Date(l.created_at).toLocaleDateString("en-GB")}</td>
      </tr>`).join("")}</tbody>
    </table>` : `<span style="color:var(--faint);font-size:13px">No logs yet</span>`;

  const cancelBtn = document.getElementById("cancel-sub-btn");
  cancelBtn.style.display = STATE.currentCustomer?.stripe_sub_id ? "inline-flex" : "none";

  document.getElementById("customer-modal").style.display="flex";
}

async function cancelSubscription(){
  if(!confirm("Cancel this subscription in Stripe?")) return;
  const subId = STATE.currentCustomer?.stripe_sub_id;
  if(!subId){showToast("No subscription found","error");return;}
  const r = await apiFetch("/api/admin/cancel-subscription",{method:"POST",body:JSON.stringify({subscription_id:subId})});
  const d = await r.json();
  if(d.ok){showToast("Subscription cancelled","success");closeModal();await loadOverview();}
  else{showToast("Error: "+(d.error||"Failed"),"error");}
}

function closeModal(){document.getElementById("customer-modal").style.display="none";}

async function sendWelcome(){
  var c = STATE.currentCustomer && STATE.currentCustomer.company;
  if(!c){showToast('No customer selected','error');return;}
  if(!confirm('Send welcome WhatsApp to ' + c.company_name + '?')) return;
  var r = await apiFetch('/api/admin/send-welcome',{method:'POST',body:JSON.stringify({whatsapp:c.whatsapp_number,company_name:c.company_name})});
  var d = await r.json();
  if(d.ok){showToast('Welcome sent to ' + c.company_name,'success');}
  else{showToast('Failed: '+(d.error||'Unknown'),'error');}
}
function showToast(msg,type=""){
  const t=document.createElement("div");t.className=`toast ${type}`;t.textContent=msg;
  document.body.appendChild(t);setTimeout(()=>t.remove(),3500);
}

function toggleTheme(){
  const html=document.documentElement;
  const btn=document.getElementById("theme-btn");
  const next = html.getAttribute("data-theme")==="dark"?"light":"dark";
  html.setAttribute("data-theme",next);
  btn.textContent = next==="dark"?"☀️":"🌙";
  localStorage.setItem("n2q-theme",next);
}

window.addEventListener("DOMContentLoaded",()=>{
  const saved=localStorage.getItem("n2q-theme");
  if(saved){document.documentElement.setAttribute("data-theme",saved);document.getElementById("theme-btn").textContent=saved==="dark"?"☀️":"🌙";}
  const pw=localStorage.getItem("n2q_admin_pw");
  if(pw){STATE.password=pw;showApp();}
});
document.addEventListener("keydown",e=>{if(e.key==="Enter"&&document.getElementById("login-screen").style.display!=="none")doLogin();});
</script>
</body>
</html>
'''


@app.route('/')
def landing():
    return Response(LANDING_HTML, mimetype='text/html')

@app.route('/signup')
def signup():
    return Response(SIGNUP_HTML, mimetype='text/html')

@app.route('/welcome')
def welcome():
    return Response(WELCOME_HTML, mimetype='text/html')

@app.route('/dashboard')
def dashboard():
    return Response(DASHBOARD_HTML, mimetype='text/html')

@app.route('/account')
def account():
    return Response(ACCOUNT_HTML, mimetype='text/html')

@app.route('/admin')
def admin_page():
    return Response(ADMIN_HTML, mimetype='text/html')
