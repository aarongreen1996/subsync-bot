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
def _html(fname):
    import os as _os
    path = _os.path.join(app.root_path, fname)
    with open(path, 'r', encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')

@app.route('/')
def landing():      return _html('landing.html')

@app.route('/signup')
def signup():       return _html('signup.html')

@app.route('/welcome')
def welcome():      return _html('welcome.html')

@app.route('/dashboard')
def dashboard():    return _html('dashboard.html')

@app.route('/account')
def account():      return _html('account.html')

@app.route('/admin')
def admin_page():   return _html('admin.html')
