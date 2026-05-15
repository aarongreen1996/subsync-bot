import os
import json
import re
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse

def _reply(text):
    resp = MessagingResponse()
    resp.message(text)
    return str(resp)

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
    if r.status_code not in (200, 201, 204):
        raise Exception(f"DB Error {r.status_code}: {r.text}")

def db_patch(path, payload):
    http_requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                        headers={**sb_headers(), "Prefer": "return=minimal"})

def normalise_wa_number(n):
    n = n.strip()
    if n.startswith("whatsapp:"):
        n = n[9:]
    n = n.replace(" ", "").replace("-", "")
    if n.startswith("07") and len(n) == 11:
        n = "+44" + n[1:]
    elif n.startswith("447") and not n.startswith("+"):
        n = "+" + n
    elif n.startswith("44") and not n.startswith("+") and not n.startswith("447"):
        n = "+" + n
    if not n.startswith("+"):
        n = "+" + n
    return "whatsapp:" + n

def encode_number(n):
    wa = normalise_wa_number(n) if not n.startswith("%2B") else n
    return wa.replace("+", "%2B")

def encode_text(text):
    from urllib.parse import quote
    return quote(str(text), safe="")

# ── Project helpers ───────────────────────────────────────────────────────────
def get_projects(from_number):
    wa      = normalise_wa_number(from_number)
    encoded = wa.replace("+", "%2B")
    result  = db_get("projects?whatsapp_number=eq." + encoded + "&status=eq.active&order=site_name.asc")
    return result if isinstance(result, list) else []

def _similarity(a, b):
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b: return 0.0
    if a == b: return 1.0
    if len(a) < 5 or len(b) < 5: return 0.0
    la, lb = len(a), len(b)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev = dp[0]; dp[0] = i
        for j in range(1, lb + 1):
            temp = dp[j]
            dp[j] = prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
            prev = temp
    return 1.0 - dp[lb] / max(la, lb)

def match_site(msg, projects):
    if not msg or not projects: return None
    msg_lower = msg.lower().strip()
    for p in projects:
        if p["site_name"].lower() in msg_lower: return p["site_name"]
    # Reverse check: msg is a substring of a known site name (e.g. "Brookfield" → "Brookfield Site")
    for p in projects:
        if len(msg_lower) >= 5 and msg_lower in p["site_name"].lower(): return p["site_name"]
    for p in projects:
        cn = (p.get("client_name") or "").strip()
        if cn and len(cn) > 3 and cn.lower() in msg_lower: return p["site_name"]
    for p in projects:
        site_words = [w for w in p["site_name"].lower().split() if len(w) > 3]
        if site_words and all(w in msg_lower for w in site_words): return p["site_name"]
    for p in projects:
        site_words = [w for w in p["site_name"].lower().split() if len(w) > 4]
        msg_words  = set(msg_lower.split())
        if site_words and any(w in msg_words for w in site_words): return p["site_name"]
    msg_words = [w for w in msg_lower.split() if len(w) >= 4]
    for p in projects:
        site_words = [w for w in p["site_name"].lower().split() if len(w) >= 4]
        for sw in site_words:
            for mw in msg_words:
                if _similarity(sw, mw) >= 0.72: return p["site_name"]
    return None

def fuzzy_site_suggestions(msg, projects):
    if not msg or not projects: return None, 0.0
    msg_words = [w for w in msg.lower().split() if len(w) >= 4]
    best_match, best_score = None, 0.0
    for p in projects:
        site_words = [w for w in p["site_name"].lower().split() if len(w) >= 4]
        for sw in site_words:
            for mw in msg_words:
                score = _similarity(sw, mw)
                if score > best_score:
                    best_score = score; best_match = p["site_name"]
    return best_match, best_score

def create_site(from_number, site_name):
    whatsapp_raw = from_number if from_number.startswith("whatsapp:") else "whatsapp:" + from_number
    try:
        db_post("projects", {"whatsapp_number": whatsapp_raw, "site_name": site_name,
                             "client_name": "", "status": "active"})
    except Exception as e:
        print(f"Site create error: {e}")

pending_selections = {}
signup_sessions    = {}   # tracks WhatsApp signup conversations

# ── Voice transcription ───────────────────────────────────────────────────────
CONSTRUCTION_VOCAB = (
    "Construction site UK subcontractor. Terms: variation order, daywork, VO, PO, "
    "snagging, first fix, second fix, making good, day rate, labour only, "
    "CIS, retention, practical completion, site manager, surveyor, main contractor, "
    "Screwfix, Toolstation, Travis Perkins, BSS, CEF, Wolseley, Jewson, Wickes, "
    "batten, joist, noggin, stud, RSJ, lintel, DPC, tanking, pointing, rendering, "
    "coving, architrave, skirting, fascia, soffit, rafter, purlin, ridge, valley, "
    "MDPE, UPVC, OSB, MDF, CLS, trunking, conduit, SWA, twin and earth, CPC, "
    "RCD, MCB, consumer unit, distribution board, socket, switched fused spur, "
    "rad, TRV, MVHR, UFH, combi, unvented, Megaflo, primatic, "
    "skim, dot and dab, bonding, multifinish, thistle, sand and cement, "
    "block, brick, mortar, rebar, shutter, pour, cure, "
    "Manor House, Brookfield, site one, block one, block two, plot, phase"
)

def transcribe_voice(media_url):
    try:
        twilio_sid  = os.environ.get("TWILIO_ACCOUNT_SID", "")
        twilio_auth = os.environ.get("TWILIO_AUTH_TOKEN", "")
        audio_r = http_requests.get(media_url, auth=(twilio_sid, twilio_auth), timeout=30)
        if audio_r.status_code != 200: return None
        files = {"file": ("audio.ogg", audio_r.content, "audio/ogg")}
        data  = {"model": "whisper-large-v3", "language": "en", "prompt": CONSTRUCTION_VOCAB}
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        groq_r  = http_requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                                     headers=headers, files=files, data=data, timeout=30)
        if groq_r.status_code == 200:
            text = groq_r.json().get("text", "").strip()
            result = preprocess_transcription(text)
            if result:
                result = result + " __TRANSCRIBED__"
            return result
        return None
    except Exception as e:
        print(f"Transcription error: {e}")
        return None

SUPPLIER_CORRECTIONS = {
    "screwfix": "Screwfix", "screw fix": "Screwfix", "screw fix it": "Screwfix",
    "toolstation": "Toolstation", "tool station": "Toolstation",
    "travis perkins": "Travis Perkins", "travis": "Travis Perkins",
    "bss": "BSS", "cef": "CEF", "city electrical": "CEF",
    "wolseley": "Wolseley", "jewson": "Jewson", "wickes": "Wickes",
    "buildbase": "Buildbase", "selco": "Selco", "b and q": "B&Q", "b&q": "B&Q",
    "plumbase": "Plumbase", "city plumbing": "City Plumbing",
}

LINGO_CORRECTIONS = {
    "very asian order": "variation order", "variation odour": "variation order",
    "very asian": "variation", "vary asian": "variation", "variegation": "variation",
    "day work": "daywork", "day works": "dayworks",
    "making goods": "making good", "purchase odour": "purchase order",
    "purchase all that": "purchase order",
    "pounds": "£", "quid": "£",
    "a grand": "£1000", "two grand": "£2000", "half a grand": "£500",
}

def preprocess_transcription(text):
    if not text: return text
    result = text.lower()
    for wrong, right in LINGO_CORRECTIONS.items():
        result = result.replace(wrong.lower(), right)
    for wrong, right in SUPPLIER_CORRECTIONS.items():
        result = result.replace(wrong.lower(), right)
    if result:
        result = result[0].upper() + result[1:]
    return result

def preprocess_text(text):
    if not text: return text
    result = text
    for wrong, right in SUPPLIER_CORRECTIONS.items():
        result = result.replace(wrong, right).replace(wrong.title(), right).replace(wrong.upper(), right)
    return result

# ── AI Prompt ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are an expert admin assistant for UK construction subcontractors and tradespeople.
Workers send you informal voice-note transcriptions or WhatsApp messages from busy building sites.
Your job is to extract structured data and classify each message accurately.

=== CRITICAL RULE — needs_clarification ===
needs_clarification must be FALSE in almost all cases. Only set it TRUE when:
- The message is COMPLETELY AMBIGUOUS — could be VARIATION or DAYWORK with no clues at all
- AND the user did NOT mention any type word (variation, daywork, material, order, timesheet)

If the user says ANY of these words → needs_clarification = FALSE, no exceptions:
- "variation", "VO", "extra", "vary" → type = VARIATION
- "daywork", "day work", "extra work I did" → type = DAYWORK
- "order", "material", "from Screwfix" etc → type = MATERIAL_ORDER
- "timesheet", "hours today", "day rate" → type = TIMESHEET

When in doubt, default to DAYWORK with needs_clarification=false. DO NOT ask.

=== SITE NAME RULES ===
- Extract any location/project name mentioned: "Danes Park", "Brookfield Site", "the flat job", "Manor House"
- Accept ANY site name the user mentions, even if informal
- NEVER use a person's name as site (Tim, Dave etc)
- NEVER use a company name as site
- If genuinely no location mentioned at all → return null (we will ask once)

=== CLASSIFICATION TYPES ===

MATERIAL_ORDER (CHECK FIRST)
Any message about buying, ordering, or needing materials/tools/equipment.
Triggers: any supplier name, "need", "order", "get some", "pick up", "grab", "delivery"
RULE: NEVER needs_clarification=true for MATERIAL_ORDER.

TIMESHEET
Worker logging their standard day/shift.
Triggers: "day rate", "standard day", "hours today", "on site", times like "8am to 4pm"
RULE: NEVER needs_clarification=true for TIMESHEET.

VARIATION
Extra work a CLIENT, SITE MANAGER, SURVEYOR or MAIN CONTRACTOR explicitly requested.
Key signals: "asked me to", "they want", "site manager said", "client wants", person's name + request
RULE: If a named person is mentioned asking for something → VARIATION, needs_clarification=false.

DAYWORK
Extra work the WORKER did and is logging for payment — no named requester.
RULE: If unclear between VARIATION/DAYWORK and NO type word used → needs_clarification=true.
      Otherwise → DAYWORK with needs_clarification=false.

=== MONEY/COST RULES ===
"quid" = £. "a ton" = £100. "a grand" = £1000.

=== MULTIPLE ITEMS ===
If message has 2+ separate tasks, return a JSON ARRAY of objects.

=== OUTPUT FORMAT ===
Respond ONLY with valid JSON. No explanation, no markdown.

{
  "type": "VARIATION|DAYWORK|MATERIAL_ORDER|TIMESHEET|UNKNOWN",
  "description": "Clear concise description",
  "hours": 2.5,
  "cost_estimate": 400.00,
  "location": "specific room/area or null",
  "site_name": "project site name or null",
  "requested_by": "person who requested it or null",
  "worker_name": "worker name if mentioned or null",
  "materials": ["list", "of", "materials"],
  "supplier": "supplier name or null",
  "needs_clarification": false,
  "confirmation_message": "Friendly ✅ confirmation, max 2 lines. Say what was logged and where."
}
"""

# ── Keywords ──────────────────────────────────────────────────────────────────
GENERATE_KEYWORDS  = [
    # generate + doc type
    "generate variation", "generate daywork", "generate day work", "generate purchase",
    "generate pos", "generate report", "generate orders", "generate order",
    "generate materials", "generate material", "generate pdf", "generate document",
    "generate doc", "generate sheet",
    # create / make
    "create invoice", "make invoice", "send invoice", "create pdf", "make pdf",
    "create variation", "make variation", "create daywork", "make daywork",
    "create purchase", "make purchase",
    # get / show / send me
    "get pdf", "get variations", "get dayworks", "get variation", "get daywork",
    "show pdf", "show me pdf", "show me the pdf", "show me a pdf", "show me my pdf",
    "show variation", "show daywork",
    "send pdf", "send me pdf", "send variation", "send daywork",
    "send me variation", "send me daywork",
    # raise / do / produce
    "raise a variation", "raise variation", "raise daywork", "raise invoice",
    "do a variation", "do the dayworks", "do a daywork",
    "produce pdf", "produce variation", "produce daywork",
    # natural phrasing
    "variation report", "daywork report", "material report", "produce report",
    "make a variation", "create daywork sheet",
    "purchase orders for", "purchase order for", "pos for",
    # short
    "pdf for", "vo for", "ds for", "po for",
]
HELP_KEYWORDS      = ["help", "guide", "how do i", "what can you do", "commands",
                      "what do i say", "how does this work", "confused", "stuck"]
DASHBOARD_KEYWORDS = ["my dashboard", "get dashboard", "dashboard link", "my password",
                      "get login", "login link", "dashboard", "login", "log in",
                      "sign in", "my login", "get link", "open dashboard", "view dashboard",
                      "access dashboard", "get access"]
SUMMARY_KEYWORDS   = ["summary", "overview", "my summary", "show summary", "stats",
                      "how am i doing", "my stats", "what have i logged", "show me everything",
                      "whats outstanding", "what's outstanding", "how much", "total", "outstanding"]
PENDING_KEYWORDS   = ["pending", "what's pending", "whats pending", "show pending",
                      "not approved", "not been approved", "what needs doing", "whats left"]
APPROVE_KEYWORDS   = ["approve", "approved", "mark approved", "all approved", "client approved",
                      "they've approved", "theyve approved", "got approval", "its approved"]
CHASE_KEYWORDS     = ["chasing", "chase", "mark chasing", "follow up", "still waiting",
                      "not heard back", "no response", "chase them", "need to chase"]
CANCEL_KEYWORDS    = ["cancel", "cancelled", "mark cancelled", "mark canceled",
                      "not happening", "drop it", "remove it"]
CORRECTION_KEYWORDS = ["no not", "not £", "its £", "it's £", "no it's", "no its",
                       "wrong", "that's wrong", "thats wrong", "correction", "incorrect",
                       "not right", "change that", "update that", "fix that",
                       "i meant", "i mean", "should be", "actually",
                       "not that", "no the cost", "no the hours", "no the description",
                       "it's variation", "its variation", "it's a variation", "its a variation",
                       "not daywork", "not day work", "not a daywork", "change to variation",
                       "change to daywork", "change to material", "should be variation",
                       "should be daywork", "it was variation", "it was a variation",
                       "variation not", "daywork not", "not variation"]
SITE_QUERY_KEYWORDS = ["outstanding on", "update on", "status of",
                       "what's on", "whats on", "how is", "how's",
                       "any updates", "updates on", "progress on", "what have we got on",
                       "what's happening on", "whats happening on",
                       "run me through", "talk me through"]
DATE_QUERY_KEYWORDS = ["show me yesterday", "show me last week", "show me this week",
                       "show me last month", "show me this month", "show me today",
                       "what did i log yesterday", "what did i log last week",
                       "what was logged yesterday", "logs from yesterday", "logs from last week",
                       "from yesterday", "from last week"]

def is_generate_command(msg):
    msg_lower = msg.lower().strip()
    # Direct keyword match
    if any(kw in msg_lower for kw in GENERATE_KEYWORDS):
        return True
    # Pattern: "generate [anything] for [site]" — catch all generate + for combos
    import re as _re
    if _re.search(r"^generate\s+\w", msg_lower):
        return True
    # Pattern: "send/show/get me [a] pdf/document/variation/daywork"
    if _re.search(r"(send|show|get)\s+(me\s+)?(a\s+)?(pdf|document|variation|daywork|day\s*work|vo|ds|po)", msg_lower):
        return True
    return False
def is_help_command(msg):
    m = msg.strip().lower()
    exact = ["help", "guide", "commands", "what can you do", "how does this work",
             "what do i say", "confused", "stuck", "how do i use this", "how do i start"]
    return m in exact or any(m == kw for kw in HELP_KEYWORDS)
def is_dashboard_command(msg): return any(kw in msg.lower() for kw in DASHBOARD_KEYWORDS)
def is_summary_command(msg):   return any(kw in msg.lower() for kw in SUMMARY_KEYWORDS)
def is_pending_command(msg):   return any(kw in msg.lower() for kw in PENDING_KEYWORDS)
def is_status_command(msg):    return any(kw in msg.lower() for kw in APPROVE_KEYWORDS + CHASE_KEYWORDS + CANCEL_KEYWORDS)
def is_site_query(msg):        return any(kw in msg.lower() for kw in SITE_QUERY_KEYWORDS)
def is_date_query(msg):        return any(kw in msg.lower() for kw in DATE_QUERY_KEYWORDS)
def is_correction(msg):        return any(kw in msg.lower() for kw in CORRECTION_KEYWORDS)

def detect_doc_type(msg):
    msg_lower = msg.lower()
    if "daywork" in msg_lower or "day work" in msg_lower:
        return "DAYWORK", "Daywork Sheet", "DS"
    if "purchase order" in msg_lower or "material order" in msg_lower or \
       ("po " in msg_lower and "purchase" in msg_lower):
        return "MATERIAL_ORDER", "Purchase Order", "PO"
    if "variation" in msg_lower or "vo " in msg_lower or " vo" in msg_lower:
        return "VARIATION", "Variation Order", "VO"
    if "material" in msg_lower or "order" in msg_lower:
        return "MATERIAL_ORDER", "Purchase Order", "PO"
    return "VARIATION", "Variation Order", "VO"

def slugify(text):
    if not text: return "doc"
    text = str(text).strip().replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_\-]", "", text)[:25] or "doc"

def make_doc_ref_and_filename(company, logs, prefix, site_name):
    try:
        wa = company.get("whatsapp_number") or ""
        sent = db_get(f"site_logs?from_number=eq.{encode_number(wa)}&status=eq.sent&select=id")
        doc_number = str((len(sent) if isinstance(sent, list) else 0) + 1).zfill(3)
    except Exception:
        doc_number = "001"
    # Safe company slug — guard every possible None/empty case
    try:
        co_raw   = company.get("company_name")
        co_name  = str(co_raw).strip() if co_raw else "Co"
        co_parts = co_name.split()
        co_first = co_parts[0] if co_parts else "Co"
    except Exception:
        co_first = "Co"
    site_slug    = slugify(site_name) if site_name else "AllSites"
    company_slug = slugify(co_first)
    date_str     = datetime.now().strftime("%d%b%Y_%H%M%S")
    ref_str      = f"{prefix}-{doc_number}"
    return ref_str, f"{ref_str}_{company_slug}_{site_slug}_{date_str}.pdf"

def upload_pdf(pdf_bytes, filename):
    url = f"{SUPABASE_URL}/storage/v1/object/documents/{filename}"
    r = http_requests.post(url, data=pdf_bytes, headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/pdf", "x-upsert": "true"})
    if r.status_code not in (200, 201):
        raise Exception(f"Storage upload failed: {r.text}")
    return f"{SUPABASE_URL}/storage/v1/object/public/documents/{filename}"

HELP_TEXT = "\n".join([
    "👷 Note2Quote Bot — Quick Guide", "",
    "LOGGING ITEMS",
    "Text or voice note, single or a list:",
    "  Variation for Danes Park — ceramic tiling to bathroom, 1 hour £50",
    "  Boarded loft flat 5, 3 hours, 80 quid materials",
    "  Numbered list to log multiple items at once", "",
    "MENTIONING THE SITE",
    "  Just say the site name in your message — I'll log it there.",
    "  New site? I'll add it automatically.", "",
    "GENERATING DOCUMENTS",
    "  Generate variations for Brookfield Site",
    "  Generate dayworks for Flat 3 Refurb", "",
    "STATUS UPDATES",
    "  approve [site] — mark all pending items as approved",
    "  chasing [site] — flag you are waiting on client", "",
    "SITE QUERIES",
    "  update on [site] — full status breakdown",
    "  show me last week — logs from past 7 days", "",
    "OTHER",
    "  summary — full dashboard overview",
    "  pending — see all outstanding items",
    "  dashboard — get your login link",
    "  help — see this guide",
])

def read_html(filename):
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, filename), "r", encoding="utf-8") as f:
        return f.read()

def build_insert(from_number, raw_message, item):
    d = {
        "from_number": from_number, "raw_message": raw_message,
        "type":        item.get("type", "UNKNOWN"),
        "description": item.get("description", ""), "status": "pending",
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
# ── WhatsApp Signup Flow ──────────────────────────────────────────────────────
# ── Demo-first signup flow ────────────────────────────────────────────────────

DEMO_HOOK = "\n".join([
    "👋 *Alright! Welcome to Note2Quote.*",
    "",
    "Let's get your evenings back. 🔨",
    "",
    "Before anything else — *let me show you exactly how this works.*",
    "",
    "Send me a quick voice note right now, pretending you're on site.",
    "Something like:",
    "🎤 *'Site manager asked me to move the boiler flue, 2 hours, £120'*",
    "🎤 *'Extra double sockets in kitchen, 1 hour, £60'*",
    "🎤 *'Need to order 10m of copper pipe from Screwfix'*",
    "",
    "Just hold the mic and say it naturally. I'll show you what happens ⬇️"
])

def handle_signup_flow(from_number, msg):
    """Demo-first signup: show value, then collect details."""
    msg_clean = msg.strip()
    msg_lower = msg_clean.lower()
    session   = signup_sessions.get(from_number, {})
    step      = session.get("step", "welcome")

    # ── CANCEL anytime ────────────────────────────────────────────────────
    if msg_lower in ["cancel", "stop", "quit"]:
        signup_sessions.pop(from_number, None)
        return _reply("No problem! Come back anytime.\nnote2quote.co.uk")

    # ── STEP 1: WELCOME → prompt for demo voice note ──────────────────────
    if step == "welcome":
        signup_sessions[from_number] = {"step": "demo_wait"}
        return _reply(DEMO_HOOK)

    # ── STEP 2: WAITING FOR DEMO (voice note or text) ─────────────────────
    if step == "demo_wait":
        # They sent something — use it to generate a demo PDF
        is_transcript = "__TRANSCRIBED__" in msg_clean
        demo_text = msg_clean.replace("__TRANSCRIBED__", "").strip()

        if len(demo_text) < 8:
            return _reply("\n".join([
                "Just send me a quick voice note or text — pretend you're on site.",
                "",
                "🎤 *'Site manager wants extra sockets in the kitchen, £80'*",
                "",
                "Hold the mic button and give it a go 👇"
            ]))

        # Run it through Claude to extract job details
        try:
            demo_parsed = _parse_demo_log(demo_text)
            desc     = demo_parsed.get("description", demo_text[:80])
            hours    = demo_parsed.get("hours", 0)
            cost     = demo_parsed.get("cost_estimate", 0)
            log_type = demo_parsed.get("type", "VARIATION")
            site     = demo_parsed.get("site_name", "Sample Site")

            # Generate demo PDF
            from pdf_generator import generate_pdf
            demo_company = {
                "company_name":  "Your Company",
                "primary_color": "#f59e0b",
                "address":       "",
                "phone":         "",
                "email":         "",
                "vat_number":    "",
                "logo_url":      None,
                "project_info":  {},
            }
            demo_log = [{
                "description":    desc,
                "type":           log_type,
                "hours":          hours,
                "cost_estimate":  cost,
                "location":       "",
                "status":         "pending",
            }]
            doc_title = "Daywork Sheet" if log_type == "DAYWORK" else "Variation Order"
            pdf_bytes = generate_pdf(demo_company, demo_log, doc_title, "DEMO-001", site)

            # Upload to Supabase storage as demo file
            import secrets as _sec
            demo_filename = f"demo_{_sec.token_hex(8)}.pdf"
            SURL = os.environ.get("SUPABASE_URL","").rstrip("/")
            SKEY = os.environ.get("SUPABASE_KEY","")
            APP_URL = os.environ.get("APP_URL","https://www.note2quote.co.uk")
            upload_r = http_requests.post(
                f"{SURL}/storage/v1/object/documents/{demo_filename}",
                data=pdf_bytes,
                headers={"apikey": SKEY, "Authorization": f"Bearer {SKEY}",
                         "Content-Type": "application/pdf", "x-upsert": "true"}
            )
            pdf_url = f"{SURL}/storage/v1/object/public/documents/{demo_filename}"

            # Save session before returning
            session["demo_desc"] = desc
            session["step"]      = "demo_react"
            signup_sessions[from_number] = session

            type_label = "variation order" if log_type == "VARIATION" else "daywork sheet"

            # Send PDF using TwiML media (same as real generate) — works in webhook context
            from flask import Response as _Resp
            from twilio.twiml.messaging_response import MessagingResponse as _MR
            resp = _MR()
            body_lines = [
                f"✅ *{desc[:60]}*",
                f"📍 {site}" + (f"  ·  ⏱ {hours}h · £{cost:.0f}" if hours or cost else ""),
                "",
                f"👆 *That's a real {type_label}* — generated in seconds from what you just said.",
                "In your live account it'd have your logo and company details on it.",
                "",
                "That's Note2Quote. Every variation, daywork and material order — logged on site in seconds.",
                "",
                "Want your evenings back? 🔨",
                "Reply *YES* to start your free 14-day trial",
                "(No credit card. Takes 2 minutes.)"
            ]
            m = resp.message("\n".join(body_lines))
            m.media(pdf_url)
            return _Resp(str(resp), mimetype="application/xml")

        except Exception as e:
            print(f"Demo generation error: {e}")
            session["step"] = "demo_react"
            signup_sessions[from_number] = session
            return _reply("\n".join([
                f"✅ *Got it — {msg_clean[:60]}*",
                "",
                "In your live account that would generate a branded PDF variation order",
                "and send it straight back to you — ready to forward to your client.",
                "",
                "Ready to see it for real?",
                "Reply *YES* to start your free 14-day trial 👇"
            ]))

    # ── STEP 3: REACTION TO DEMO ─────────────────────────────────────────
    if step == "demo_react":
        yes_words = ["yes","yeah","yep","sure","ok","okay","go","start","sign",
                     "signup","trial","free","let's go","lets go","great","love it",
                     "looks good","amazing","brilliant","nice","want it","in"]
        if any(w in msg_lower for w in yes_words):
            session["step"] = "name"
            signup_sessions[from_number] = session
            return _reply("\n".join([
                "Let's get you set up! 🚀",
                "",
                "Takes 2 minutes. First — what's your *name*?",
                "(e.g. Aaron Green)"
            ]))
        # Not ready — soft nudge
        return _reply("\n".join([
            "No worries — whenever you're ready just reply *YES*.",
            "",
            "Or visit note2quote.co.uk to find out more.",
            "14-day free trial, no card needed 👍"
        ]))

    # ── STEP 4: NAME ─────────────────────────────────────────────────────
    if step == "name":
        if len(msg_clean) < 2:
            return _reply("Just your name — e.g. *Aaron Green*")
        session["name"] = msg_clean.title()
        session["step"] = "company"
        signup_sessions[from_number] = session
        return _reply(f"Nice to meet you, *{session['name']}*! 👋\n\nWhat's your *company name*?\n(or just your own name if you're a sole trader)")

    # ── STEP 5: COMPANY ──────────────────────────────────────────────────
    if step == "company":
        if len(msg_clean) < 2:
            return _reply("What's your company name? (or your name if you're a sole trader)")
        session["company"] = msg_clean
        session["step"]    = "trade"
        signup_sessions[from_number] = session
        return _reply("\n".join([
            "What *trade* are you in?",
            "",
            "e.g. Plumber, Electrician, Builder, Roofer,",
            "Plasterer, Joiner, Tiler, Ground Worker..."
        ]))

    # ── STEP 6: TRADE ────────────────────────────────────────────────────
    if step == "trade":
        if len(msg_clean) < 2:
            return _reply("Just your trade — e.g. *Plumber* or *Electrician*")
        session["trade"] = msg_clean.title()
        session["step"]  = "email"
        signup_sessions[from_number] = session
        return _reply("\n".join([
            "Almost there! Last thing — what's your *email address*?",
            "",
            "We'll send your dashboard login link there.",
            "(e.g. aaron@greenplumbing.co.uk)"
        ]))

    # ── STEP 7: EMAIL ────────────────────────────────────────────────────
    if step == "email":
        import re as _re
        email_clean = msg_clean.lower().strip()
        if not _re.match(r"[^@]+@[^@]+\.[^@]+", email_clean):
            return _reply("That doesn't look right — can you send your *email address*?\n(e.g. aaron@greenplumbing.co.uk)")
        session["email"] = email_clean
        session["step"]  = "confirm"
        signup_sessions[from_number] = session
        return _reply("\n".join([
            "✅ *Here's what I've got:*",
            "",
            f"👤 Name: {session['name']}",
            f"🏢 Company: {session['company']}",
            f"🔨 Trade: {session['trade']}",
            f"📧 Email: {session['email']}",
            "",
            "Is that correct? Reply *YES* to activate your free trial",
            "or *NO* to start again."
        ]))

    # ── STEP 8: CONFIRM ──────────────────────────────────────────────────
    if step == "confirm":
        if msg_lower in ["no", "nope", "wrong", "incorrect", "start again", "redo"]:
            signup_sessions.pop(from_number, None)
            return _reply("No problem — let's start again. Just reply *YES* when ready.")
        if msg_lower in ["yes","yeah","yep","correct","looks good","ok","okay","go","confirm"]:
            return _provision_account(from_number, session)
        return _reply("Reply *YES* to confirm and activate your trial, or *NO* to start again.")

    # Fallback
    signup_sessions.pop(from_number, None)
    signup_sessions[from_number] = {"step": "demo_wait"}
    return _reply(DEMO_HOOK)


def _parse_demo_log(text):
    """Quick AI parse of demo voice note to extract job details."""
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="""Extract job details from this message. Return ONLY valid JSON with these keys:
description (string, max 80 chars), type (VARIATION/DAYWORK/MATERIAL_ORDER),
hours (float or 0), cost_estimate (float or 0), site_name (string or "Sample Site").
No explanation, just JSON.""",
            messages=[{"role": "user", "content": text}]
        )
        import json as _json
        raw = resp.content[0].text.strip()
        if raw.startswith("```"): raw = raw.split("```")[1].replace("json","").strip()
        return _json.loads(raw)
    except Exception:
        return {"description": text[:80], "type": "VARIATION",
                "hours": 0, "cost_estimate": 0, "site_name": "Sample Site"}




def _provision_account(from_number, session):
    """Create the company record and send welcome with magic link."""
    import secrets
    from datetime import datetime, timezone, timedelta

    wa_raw   = normalise_wa_number(from_number)
    wa_enc   = wa_raw.replace("+", "%2B")
    APP_URL  = os.environ.get("APP_URL", "https://www.note2quote.co.uk")
    SURL     = os.environ.get("SUPABASE_URL", "").rstrip("/")
    SKEY     = os.environ.get("SUPABASE_KEY", "")

    name     = session.get("name", "")
    company  = session.get("company", "")
    trade    = session.get("trade", "")
    email    = session.get("email", "")
    phone    = wa_raw.replace("whatsapp:", "")

    # Generate username from name
    username = name.lower().replace(" ", ".").strip(".")[:30]

    # Generate a unique readable password — 3 words + 2 digits
    import random
    _words = ["site","roof","pipe","wall","tile","beam","bolt","nail","wire","drill",
              "build","fix","seal","weld","pour","sand","coat","fit","lay","cut"]
    _pw_word1 = random.choice(_words).capitalize()
    _pw_word2 = random.choice(_words).capitalize()
    _pw_num   = str(random.randint(10, 99))
    unique_password = _pw_word1 + _pw_word2 + _pw_num

    try:
        # Check if already exists (in case they triggered this twice)
        existing = db_get(f"companies?whatsapp_number=eq.{wa_enc}&limit=1")
        if isinstance(existing, list) and existing:
            # Already exists — just update company_name in case it was null
            db_patch(f"companies?whatsapp_number=eq.{wa_enc}",
                     {"company_name": company, "email": email})
        else:
            # Create company record
            db_post("companies", {
                "whatsapp_number":    wa_raw,
                "company_name":       company,
                "email":              email,
                "phone":              phone,
                "primary_color":      "#f59e0b",
                "username":           username,
                "dashboard_password": unique_password,
            })

        # Create magic login token (24hr)
        token   = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        http_requests.post(
            SURL + "/rest/v1/auth_tokens",
            json={"token": token, "whatsapp": wa_raw,
                  "expires_at": expires, "used": False},
            headers={"apikey": SKEY, "Authorization": "Bearer " + SKEY,
                     "Content-Type": "application/json", "Prefer": "return=minimal"}
        )
        login_url = APP_URL + "/login?token=" + token

        # Clear signup session
        signup_sessions.pop(from_number, None)

        return _reply("\n".join([
            f"🎉 *Welcome to Note2Quote, {name}!*",
            "",
            "Your free 14-day trial is now active.",
            "",
            "📊 *Your dashboard (tap to open):*",
            login_url,
            "",
            "🔑 *Manual login:*",
            f"Username: {username}",
            f"Password: {unique_password}",
            "",
            "Now try it — just tell me something that happened on site today:",
            "🎤 *'Extra rad in bedroom 3, 2 hours, £80'*",
            "🎤 *'Site manager wants extra sockets in kitchen, £120'*",
            "",
            "Send *help* anytime for the full guide 👷"
        ]))

    except Exception as e:
        print(f"Signup provisioning error: {e}")
        signup_url = APP_URL + "/signup"
        return _reply("\n".join([
            "⚠️ Something went wrong setting up your account.",
            "",
            "Please sign up at:",
            signup_url,
            "",
            "Or reply again and I'll try once more."
        ]))


@app.route("/whatsapp", methods=["POST"])
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

    _wa_norm = normalise_wa_number(from_number)
    _wa_enc  = _wa_norm.replace("+", "%2B")
    companies = db_get("companies?whatsapp_number=eq." + _wa_enc + "&limit=1")
    if not isinstance(companies, list) or not companies:
        return handle_signup_flow(from_number, incoming_msg)

    # Always check commands first even if pending state — prevents accidental logging
    if is_dashboard_command(incoming_msg):
        if from_number in pending_selections: del pending_selections[from_number]
        return handle_dashboard_command(from_number)
    if is_summary_command(incoming_msg):
        if from_number in pending_selections: del pending_selections[from_number]
        return handle_summary(from_number)
    if is_help_command(incoming_msg):
        if from_number in pending_selections: del pending_selections[from_number]
        return _reply(HELP_TEXT)
    if is_generate_command(incoming_msg):
        if from_number in pending_selections: del pending_selections[from_number]
        return handle_generate(from_number, incoming_msg)
    if is_status_command(incoming_msg):
        if from_number in pending_selections: del pending_selections[from_number]
        return handle_status_update(from_number, incoming_msg)
    if is_pending_command(incoming_msg):
        if from_number in pending_selections: del pending_selections[from_number]
        return handle_pending_summary(from_number)

    if from_number in pending_selections:
        return handle_pending(from_number, incoming_msg)

    _msg_lower = incoming_msg.lower()
    _log_hints = ["hours", "hour", "mins", "materials", "quid", "£", "standard day",
                  "day rate", "variation", "daywork", "order", "fitted", "installed",
                  "boarded", "plastered", "fixed", "sorted", "done", "completed"]
    _looks_like_log = any(h in _msg_lower for h in _log_hints)

    # ── FIX: date queries BEFORE site queries ──
    if not _looks_like_log and is_date_query(incoming_msg):
        return handle_date_query(from_number, incoming_msg)
    if not _looks_like_log and is_site_query(incoming_msg):
        return handle_site_query(from_number, incoming_msg)

    if is_correction(incoming_msg):
        return handle_correction(from_number, incoming_msg)

    return handle_log(from_number, incoming_msg)


# ── Pending handler ───────────────────────────────────────────────────────────
def handle_pending(from_number, msg):
    state    = pending_selections[from_number]
    projects = state["projects"]
    log_data = state["pending_log"]
    msg_clean = msg.strip().lower()

    # Universal cancel
    if msg_clean in ["cancel", "abort", "wrong", "stop", "exit", "no", "redo", "start again", "clear"]:
        del pending_selections[from_number]
        return _reply("No problem — cancelled. Send your message again whenever you're ready.")

    # ── Type clarification ────────────────────────────────────────────────────
    if state.get("awaiting_type"):
        log_type = None
        if any(w in msg_clean for w in ["variation", "vo", "extra", "client", "site manager", "they asked"]):
            log_type = "VARIATION"
        elif any(w in msg_clean for w in ["daywork", "day work", "i did", "myself", "extra work"]):
            log_type = "DAYWORK"
        elif any(w in msg_clean for w in ["material", "order", "purchase", "po", "buy"]):
            log_type = "MATERIAL_ORDER"
        elif any(w in msg_clean for w in ["timesheet", "hours", "time"]):
            log_type = "TIMESHEET"
        elif msg.strip() == "1": log_type = "VARIATION"
        elif msg.strip() == "2": log_type = "DAYWORK"
        elif msg.strip() == "3": log_type = "MATERIAL_ORDER"

        if not log_type and len(msg.strip()) > 15:
            del pending_selections[from_number]
            return handle_log(from_number, msg)

        if not log_type:
            return _reply("Just say variation, daywork, or material order — or cancel to start again.")

        log_data["type"] = log_type
        original_msg = state.get("original_msg", "")
        site_name = match_site(original_msg, projects) if original_msg else None

        if site_name:
            log_data["site_name"] = site_name
            del pending_selections[from_number]
            db_post("site_logs", log_data)
            return _reply(f"✅ Logged as *{log_type}* for *{site_name}*!")
        elif log_data.get("site_name"):
            del pending_selections[from_number]
            db_post("site_logs", log_data)
            return _reply(f"✅ Logged as *{log_type}* for *{log_data['site_name']}*!")
        else:
            del pending_selections[from_number]
            pending_selections[from_number] = {
                "pending_log": log_data, "projects": projects,
                "awaiting_type": False, "awaiting_site": True,
                "original_msg": original_msg,
            }
            return _reply("Got it. Which site is this for? Just type the site name.")

    # ── Site selection ────────────────────────────────────────────────────────
    if state.get("awaiting_site"):
        if state.get("_fuzzy_suggest"):
            fuzzy_match = state["_fuzzy_suggest"]
            if msg_clean in ["yes", "y", "yeah", "yep", "correct", "that one", "that's it", "thats it"]:
                log_data["site_name"] = fuzzy_match
                del pending_selections[from_number]
                db_post("site_logs", log_data)
                return _reply("✅ Logged for *" + fuzzy_match + "*!")
            elif msg_clean in ["no", "nope", "wrong", "different"]:
                state.pop("_fuzzy_suggest", None)
                pending_selections[from_number] = state
                return _reply("What's the site name? Just type it.")

        new_log_keywords = ["hours", "variation", "daywork", "order", "timesheet",
                            "boarded", "fitted", "installed", "fixed", "repaired"]
        looks_like_new_log = (len(msg.strip()) > 20 and
                              any(kw in msg_clean for kw in new_log_keywords))
        if looks_like_new_log:
            del pending_selections[from_number]
            return handle_log(from_number, msg)

        site_name = None
        if msg.strip().isdigit():
            idx = int(msg.strip()) - 1
            if 0 <= idx < len(projects):
                site_name = projects[idx]["site_name"]

        if not site_name:
            site_name = match_site(msg, projects)

        if not site_name:
            site_name = msg.strip().title()
            create_site(from_number, site_name)

        del pending_selections[from_number]

        # Status update flow
        if log_data.get("_status_update"):
            from urllib.parse import quote as _q
            encoded    = encode_number(from_number)
            new_status = log_data.get("new_status", "pending")
            past_tense = log_data.get("past_tense", "updated")
            emoji      = log_data.get("emoji", "✅")
            logs = db_get("site_logs?from_number=eq." + encoded +
                          "&status=in.(pending,chasing,sent)&site_name=eq." + _q(site_name))
            if isinstance(logs, list) and logs:
                for log in logs:
                    db_patch("site_logs?id=eq." + str(log["id"]), {"status": new_status})
                total_val = sum(float(l.get("cost_estimate") or 0) for l in logs)
                return _reply(emoji + " *" + str(len(logs)) + " item(s) " + past_tense +
                              "* for *" + site_name + "*\nTotal value: £" + ("%.2f" % total_val))
            else:
                return _reply("No active items found for *" + site_name + "*.")

        # Multi-item bulk save
        if log_data.get("_multi"):
            saved = 0; total_cost = 0.0
            for item in log_data.get("_items", []):
                item["site_name"] = site_name
                try:
                    db_post("site_logs", item); saved += 1
                    total_cost += float(item.get("cost_estimate") or 0)
                except Exception:
                    pass
            cost_tag = f" · Est. £{total_cost:.0f}" if total_cost else ""
            return _reply(f"✅ Logged {saved} item(s) for *{site_name}*{cost_tag}!")

        # Single item save
        log_data["site_name"] = site_name
        try:
            db_post("site_logs", log_data)
            return _reply(f"✅ Logged for *{site_name}*!")
        except Exception as e:
            return _reply(f"⚠️ Couldn't save. ({str(e)[:80]})")

    del pending_selections[from_number]
    return _reply("Something went wrong. Please try sending that again.")


# ── Dashboard command ─────────────────────────────────────────────────────────
def handle_dashboard_command(from_number):
    import secrets
    from datetime import datetime, timezone, timedelta
    APP_URL  = os.environ.get("APP_URL", "https://www.note2quote.co.uk")
    SURL     = os.environ.get("SUPABASE_URL", "").rstrip("/")
    SKEY     = os.environ.get("SUPABASE_KEY", "")

    _wa_norm = normalise_wa_number(from_number)
    _wa_enc  = _wa_norm.replace("+", "%2B")
    companies = db_get("companies?whatsapp_number=eq." + _wa_enc + "&limit=1")
    company   = companies[0] if isinstance(companies, list) and companies else {}
    username  = company.get("username") or ""
    dashboard_pw = company.get("dashboard_password") or ""
    wa_number = from_number.replace("whatsapp:+44", "07").replace("whatsapp:", "")

    token    = secrets.token_urlsafe(32)
    expires  = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    http_requests.post(SURL + "/rest/v1/auth_tokens",
        json={"token": token, "whatsapp": from_number, "expires_at": expires, "used": False},
        headers={"apikey": SKEY, "Authorization": "Bearer " + SKEY,
                 "Content-Type": "application/json", "Prefer": "return=minimal"})
    login_url = APP_URL + "/login?token=" + token

    lines = ["Your Note2Quote Dashboard", "", "Tap to open instantly (24hr link):",
             login_url, "", "Or log in at " + APP_URL + "/dashboard:"]
    if username:    lines.append("Username: " + username)
    if dashboard_pw: lines.append("Password: " + dashboard_pw)
    lines.append("Mobile: " + wa_number)
    return _reply("\n".join(lines))


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

        type_filter = ""
        if log_type != "ALL":
            type_filter = "&type=eq." + log_type

        base_query = ("site_logs?from_number=eq." + encode_number(from_number) +
                      "&status=in.(pending,chasing)" + type_filter + "&order=created_at.asc")
        if site_name:
            base_query += "&site_name=ilike." + encode_text(site_name)

        logs = db_get(base_query)
        if not isinstance(logs, list) or not logs:
            type_label = doc_title + "s" if log_type != "ALL" else "items"
            site_label_msg = " for *" + site_name + "*" if site_name else ""
            return _reply("📋 No active " + type_label + site_label_msg + ".\n"
                          "Items must be logged (pending or chasing) to appear in a document.")

        doc_ref, filename = make_doc_ref_and_filename(company, logs, prefix, site_name)
        company["site_label"] = site_label

        project_info = {}
        if site_name:
            projs = db_get(f"projects?whatsapp_number=eq.{encode_number(from_number)}&site_name=ilike.{encode_text(site_name)}&limit=1")
            if isinstance(projs, list) and projs:
                project_info = projs[0]
        company["project_info"] = project_info

        pdf_bytes = generate_pdf(company, logs, doc_title, doc_ref, site_label)
        pdf_url   = upload_pdf(pdf_bytes, filename)

        for log in logs:
            db_patch(f"site_logs?id=eq.{log['id']}", {"status": "sent"})

        resp = MessagingResponse()
        m = resp.message(
            f"📄 *{doc_ref}* — {doc_title}\n"
            f"Site: {site_label} · {len(logs)} item(s)\n"
            f"Review and forward to your client ✅"
        )
        m.media(pdf_url)
        return Response(str(resp), mimetype="application/xml")

    except Exception as e:
        import traceback
        print(f"Generate error: {traceback.format_exc()}")
        return _reply(f"⚠️ Couldn't generate document. ({str(e)[:150]})")


# ── Log handler ───────────────────────────────────────────────────────────────
def handle_log(from_number, incoming_msg):
    try:
        if incoming_msg.endswith(" __TRANSCRIBED__"):
            processed_msg = incoming_msg[:-len(" __TRANSCRIBED__")].strip()
        else:
            processed_msg = preprocess_text(incoming_msg)

        SHORT_CHAT = ["ok", "okay", "thanks", "cheers", "got it", "nice", "great",
                      "good", "perfect", "sweet", "yes", "no", "yep", "nope", "sure",
                      "alright", "sorted", "cool", "brilliant", "brill", "ta",
                      "hello", "hi", "hey", "hiya", "morning", "afternoon", "evening",
                      "hello there", "hi there", "hey there", "how are you", "howdy",
                      "sup", "whats up", "yo", "helo", "hii"]
        msg_clean = processed_msg.strip().lower()
        if msg_clean in SHORT_CHAT or len(processed_msg.strip()) < 4:
            greetings = ["hello","hi","hey","hiya","morning","afternoon","evening","howdy","yo","sup"]
            if any(msg_clean.startswith(g) for g in greetings):
                return _reply("\n".join([
                    "👋 Hey! Welcome to Note2Quote.",
                    "",
                    "I'm your WhatsApp admin assistant for site work. Just tell me what happened on site and I'll log it instantly.",
                    "",
                    "For example:",
                    "\U0001f3a4 'Site manager asked me to move the boiler flue at Brookfield, 2 hours, £120'",
                    "\U0001f3a4 'Need to order copper fittings from Screwfix for Danes Park'",
                    "",
                    "Or send *help* to see everything I can do 👍"
                ]))
            return _reply("👍 No problem! Send me a site update when you're ready.")

        ai_response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": processed_msg}]
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
            items      = [build_insert(from_number, incoming_msg, i) for i in parsed]
            total_cost = sum(float(i.get("cost_estimate") or 0) for i in parsed)

            # Match site per item individually — don't force all items to one site
            saved      = 0
            needs_site = []
            known_site_names = [p["site_name"] for p in projects]

            for item, raw in zip(items, parsed):
                ai_site   = raw.get("site_name") or ""
                site_name = None

                if ai_site:
                    # Try to match to a known project
                    site_name = match_site(ai_site, projects)
                    if not site_name:
                        # Genuinely new site — auto-create it
                        site_name = ai_site
                        if site_name not in known_site_names:
                            create_site(from_number, site_name)
                            known_site_names.append(site_name)
                else:
                    # No site in this item — try the raw message as fallback
                    site_name = match_site(incoming_msg, projects)

                if site_name:
                    item["site_name"] = site_name
                    try:
                        db_post("site_logs", item)
                        saved += 1
                    except Exception:
                        pass
                else:
                    needs_site.append(item)

            cost_tag = " · Est. £" + ("%.0f" % total_cost) if total_cost else ""

            # Some items had no site — ask once for the remainder
            if needs_site:
                pending_selections[from_number] = {
                    "pending_log": {"_multi": True, "_items": needs_site},
                    "projects": projects, "awaiting_type": False, "awaiting_site": True,
                    "original_msg": incoming_msg,
                }
                saved_msg = ("✅ Logged " + str(saved) + " item(s) to their sites" + cost_tag + ".\n\n") if saved else ""
                return _reply(saved_msg + "Which site are the remaining " + str(len(needs_site)) +
                              " item(s) for? Just type the site name.")

            return _reply("✅ Logged " + str(saved) + " item(s) across sites" + cost_tag + "!")

        # ── Single item ───────────────────────────────────────────────────────
        if isinstance(parsed, dict):
            data                = parsed
            insert_data         = build_insert(from_number, incoming_msg, data)
            needs_clarification = data.get("needs_clarification", False)
            confirmation        = data.get("confirmation_message", "✅ Got it!")

            ai_site_name = data.get("site_name") or ""
            site_name = None

            if ai_site_name:
                known_match = match_site(ai_site_name, projects)
                if known_match:
                    site_name = known_match
                else:
                    fuzzy_match, score = fuzzy_site_suggestions(ai_site_name, projects)
                    if fuzzy_match and score >= 0.72:
                        insert_data["site_name"] = ai_site_name
                        pending_selections[from_number] = {
                            "pending_log": insert_data, "projects": projects,
                            "awaiting_type": False, "awaiting_site": True,
                            "_fuzzy_suggest": fuzzy_match, "original_msg": incoming_msg,
                        }
                        return _reply(
                            confirmation + "\n\nDid you mean *" + fuzzy_match + "*?\n"
                            "Reply yes to confirm, or type the correct site name."
                        )
                    else:
                        site_name = ai_site_name
                        create_site(from_number, site_name)
            else:
                site_name = match_site(incoming_msg, projects)

            if needs_clarification:
                insert_data["site_name"] = site_name
                pending_selections[from_number] = {
                    "pending_log": insert_data, "projects": projects,
                    "awaiting_type": True, "awaiting_site": False,
                    "original_msg": incoming_msg,
                }
                return _reply(
                    confirmation + "\n\nJust to confirm — was this a *variation order* "
                    "(client or site manager asked for it) or *daywork* (extra work you logged yourself)?"
                )

            if site_name:
                insert_data["site_name"] = site_name
                db_post("site_logs", insert_data)
                return _reply(confirmation + f"\n📍 *{site_name}*")
            else:
                pending_selections[from_number] = {
                    "pending_log": insert_data, "projects": projects,
                    "awaiting_type": False, "awaiting_site": True,
                    "original_msg": incoming_msg,
                }
                return _reply(confirmation + "\n\nWhich site is this for? Just type the site name.")

        return _reply("⚠️ I couldn't understand that. Try rephrasing.")

    except json.JSONDecodeError:
        return _reply("⚠️ I couldn't read that clearly. Try rephrasing.")
    except Exception as e:
        return _reply(f"⚠️ Something went wrong. ({str(e)[:100]})")


# ── Landing ───────────────────────────────────────────────────────────────────
import base64 as _b64
_LANDING_B64 = 'PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj48aGVhZD48bWV0YSBjaGFyc2V0PSJVVEYtOCI+PHRpdGxlPk5vdGUyUXVvdGU8L3RpdGxlPjwvaGVhZD48Ym9keT48aDE+Tm90ZTJRdW90ZTwvaDE+PC9ib2R5PjwvaHRtbD4='

@app.route('/')
def landing():
    try:
        return read_html('landing.html')
    except FileNotFoundError:
        print("WARNING: landing.html not found — using fallback")
        html = _b64.b64decode(_LANDING_B64).decode('utf-8')
        return Response(html, mimetype='text/html')
    except Exception as e:
        print(f"WARNING: landing.html error: {e} — using fallback")
        html = _b64.b64decode(_LANDING_B64).decode('utf-8')
        return Response(html, mimetype='text/html')

def _html(fname):
    path = os.path.join(app.root_path, fname)
    with open(path, 'r', encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')

@app.route('/signup')
def signup():    return _html('signup.html')
@app.route('/welcome')
def welcome():   return _html('welcome.html')
@app.route('/dashboard')
def dashboard(): return _html('dashboard.html')
@app.route('/account')
def account():   return _html('account.html')
@app.route('/admin')
def admin_page(): return _html('admin.html')


# ── Summary command ───────────────────────────────────────────────────────────
def handle_summary(from_number):
    try:
        encoded   = encode_number(from_number)
        logs      = db_get("site_logs?from_number=eq." + encoded + "&order=created_at.desc")
        companies = db_get("companies?whatsapp_number=eq." + encoded + "&limit=1")
        company_name = companies[0].get("company_name", "Your Company") if isinstance(companies, list) and companies else "Your Company"
        if not isinstance(logs, list): logs = []
        pending  = [l for l in logs if l.get("status") == "pending"]
        approved = [l for l in logs if l.get("status") == "approved"]
        chasing  = [l for l in logs if l.get("status") == "chasing"]
        sent     = [l for l in logs if l.get("status") == "sent"]
        pend_val  = sum(float(l.get("cost_estimate") or 0) for l in pending)
        appr_val  = sum(float(l.get("cost_estimate") or 0) for l in approved)
        chase_val = sum(float(l.get("cost_estimate") or 0) for l in chasing)
        site_map  = {}
        for l in pending:
            site = l.get("site_name") or "Unassigned"
            if site not in site_map: site_map[site] = {"count": 0, "value": 0.0}
            site_map[site]["count"] += 1
            site_map[site]["value"] += float(l.get("cost_estimate") or 0)
        out = ["📊 *" + company_name + " — Summary*", "",
               "📋 *Pending:* " + str(len(pending)) + " items · £" + ("%.2f" % pend_val),
               "✅ *Approved:* " + str(len(approved)) + " items · £" + ("%.2f" % appr_val),
               "⏰ *Chasing:* " + str(len(chasing)) + " items · £" + ("%.2f" % chase_val),
               "📄 *Docs sent:* " + str(len(sent)) + " total", ""]
        if site_map:
            out.append("*By Site (pending):*")
            for site, info in sorted(site_map.items(), key=lambda x: -x[1]["value"]):
                out.append("📍 " + site + " — " + str(info["count"]) + " items · £" + ("%.2f" % info["value"]))
            out.append("")
        out += ["*Quick commands:*", "Reply *pending* — see full list",
                "Reply *approve [site]* — mark as approved",
                "Reply *my dashboard* — get login link"]
        return _reply("\n".join(out))
    except Exception as e:
        return _reply("⚠️ Couldn't get summary. (" + str(e)[:80] + ")")


# ── Pending list ──────────────────────────────────────────────────────────────
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
        out += ["", "Reply *approve [site name]* to mark as approved",
                "Reply *summary* for full overview"]
        return _reply("\n".join(out))
    except Exception as e:
        return _reply("⚠️ Couldn't get pending list. (" + str(e)[:80] + ")")


# ── Status update ─────────────────────────────────────────────────────────────
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
        type_filter = None
        if any(kw in msg_lower for kw in ["variation", "vo", "variations"]):
            type_filter = "VARIATION"
        elif any(kw in msg_lower for kw in ["daywork", "dayworks", "day work"]):
            type_filter = "DAYWORK"
        elif any(kw in msg_lower for kw in ["material", "purchase order", "po ", "materials", "order"]):
            type_filter = "MATERIAL_ORDER"
        elif any(kw in msg_lower for kw in ["timesheet", "timesheets"]):
            type_filter = "TIMESHEET"

        if site_name:
            q = ("site_logs?from_number=eq." + encoded +
                 "&status=in.(pending,chasing,sent)&site_name=eq." + _quote(site_name))
            if type_filter: q += "&type=eq." + type_filter
            logs = db_get(q)
            if not isinstance(logs, list) or not logs:
                return _reply("No active items found for *" + site_name + "*.")
            for log in logs:
                db_patch("site_logs?id=eq." + str(log["id"]), {"status": new_status})
            total_val = sum(float(l.get("cost_estimate") or 0) for l in logs)
            return _reply(emoji + " *" + str(len(logs)) + " item(s) " + past_tense +
                          "* for *" + site_name + "*\nTotal value: £" + ("%.2f" % total_val))
        elif projects:
            pending_selections[from_number] = {
                "pending_log": {"_status_update": True, "new_status": new_status,
                                "past_tense": past_tense, "emoji": emoji},
                "projects": projects, "awaiting_type": False, "awaiting_site": True,
                "original_msg": msg,
            }
            return _reply("Which site do you want to " + new_status + "? Just type the site name.")
        else:
            return _reply("No sites found. Add sites via WhatsApp or your account portal.")
    except Exception as e:
        return _reply("⚠️ Couldn't update status. (" + str(e)[:80] + ")")


# ── Site query — with full detail per item ────────────────────────────────────
def handle_site_query(from_number, msg):
    try:
        encoded  = encode_number(from_number)
        projects = get_projects(from_number)
        all_logs = db_get("site_logs?from_number=eq." + encoded + "&order=created_at.desc")
        if not isinstance(all_logs, list): all_logs = []
        site_name = match_site(msg, projects)
        logs = [l for l in all_logs if l.get("site_name") == site_name] if site_name else all_logs

        pending   = [l for l in logs if l.get("status") == "pending"]
        chasing   = [l for l in logs if l.get("status") == "chasing"]
        sent      = [l for l in logs if l.get("status") == "sent"]
        approved  = [l for l in logs if l.get("status") == "approved"]
        cancelled = [l for l in logs if l.get("status") == "cancelled"]

        out = ["📍 *" + (site_name or "All Sites") + " — Current Status*", ""]

        # ── FIX: Show type, hours and cost for each item ──
        def fmt_logs(log_list, limit=5):
            rows = []
            for l in log_list[:limit]:
                desc  = (l.get("description") or "—")[:42]
                ltype = (l.get("type") or "").replace("MATERIAL_ORDER", "Mat Order").replace("_", " ").title()
                cost  = "£" + ("%.2f" % float(l.get("cost_estimate") or 0)) if l.get("cost_estimate") else ""
                hrs   = str(l.get("hours")) + "h" if l.get("hours") else ""
                parts = [x for x in [ltype, hrs, cost] if x]
                rows.append("  • " + desc + (" [" + " · ".join(parts) + "]" if parts else ""))
            if len(log_list) > limit:
                rows.append("  ... and " + str(len(log_list) - limit) + " more")
            return rows

        if pending:
            pval = sum(float(l.get("cost_estimate") or 0) for l in pending)
            out.append("📋 *Pending (" + str(len(pending)) + ") · £" + ("%.2f" % pval) + "*")
            out.extend(fmt_logs(pending)); out.append("")
        if chasing:
            cval = sum(float(l.get("cost_estimate") or 0) for l in chasing)
            out.append("⏰ *Chasing (" + str(len(chasing)) + ") · £" + ("%.2f" % cval) + "*")
            out.extend(fmt_logs(chasing)); out.append("")
        if sent:
            sval = sum(float(l.get("cost_estimate") or 0) for l in sent)
            out.append("📄 *Sent (" + str(len(sent)) + ") · £" + ("%.2f" % sval) + "*")
            out.extend(fmt_logs(sent, 3)); out.append("")
        if approved:
            aval = sum(float(l.get("cost_estimate") or 0) for l in approved)
            out.append("✅ *Approved (" + str(len(approved)) + ") · £" + ("%.2f" % aval) + "*")
        if not logs:
            out.append("Nothing logged yet" + (" for " + site_name if site_name else "") + ".")
        return _reply("\n".join(out))
    except Exception as e:
        return _reply("Could not get site update. (" + str(e)[:80] + ")")


# ── Date range query ──────────────────────────────────────────────────────────
def handle_date_query(from_number, msg):
    from datetime import datetime, timezone, timedelta
    try:
        encoded   = encode_number(from_number)
        msg_lower = msg.lower()
        now       = datetime.now(timezone.utc)

        # "yesterday" = midnight to midnight of the previous day — NOT last 24 hours
        until_str = None
        if "yesterday" in msg_lower:
            since = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            until = now.replace(hour=0, minute=0, second=0, microsecond=0)
            until_str = until.isoformat().replace("+", "%2B")
            label = "Yesterday"
        elif "last week" in msg_lower:    since = now - timedelta(days=7); label = "Last 7 days"
        elif "this week" in msg_lower:
            since = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
            label = "This week"
        elif "last month" in msg_lower:   since = now - timedelta(days=30); label = "Last 30 days"
        elif "this month" in msg_lower:   since = now.replace(day=1, hour=0, minute=0, second=0); label = "This month"
        elif "today" in msg_lower:        since = now.replace(hour=0, minute=0, second=0); label = "Today"
        else:                             since = now - timedelta(days=7); label = "Last 7 days"

        # Replace + in timezone offset with %2B so URL isn't malformed
        since_str = since.isoformat().replace("+", "%2B")
        query = "site_logs?from_number=eq." + encoded + "&created_at=gte." + since_str
        if until_str:
            query += "&created_at=lt." + until_str
        query += "&order=created_at.desc"
        all_logs = db_get(query)
        if not isinstance(all_logs, list): all_logs = []
        if not all_logs:
            return _reply("Nothing logged in the " + label.lower() + " period.")

        by_site = {}
        for l in all_logs:
            site = l.get("site_name") or "No site"
            if site not in by_site: by_site[site] = []
            by_site[site].append(l)

        total_val = sum(float(l.get("cost_estimate") or 0) for l in all_logs)
        out = ["📅 *" + label + " — " + str(len(all_logs)) + " items · £" + ("%.2f" % total_val) + "*", ""]

        for site, logs in sorted(by_site.items()):
            site_val = sum(float(l.get("cost_estimate") or 0) for l in logs)
            out.append("📍 *" + site + "* — " + str(len(logs)) + " items" +
                       (" · £" + ("%.2f" % site_val) if site_val else ""))
            for l in logs[:4]:
                desc   = (l.get("description") or "—")[:40]
                status = l.get("status") or "pending"
                cost   = "£" + ("%.2f" % float(l.get("cost_estimate") or 0)) if l.get("cost_estimate") else ""
                ltype  = (l.get("type") or "").replace("MATERIAL_ORDER","Mat Order").replace("_"," ").title()
                icon   = {"pending": "📋", "chasing": "⏰", "sent": "📄",
                          "approved": "✅", "cancelled": "❌"}.get(status, "•")
                row = "  " + icon + " " + desc + " [" + ltype + (", " + cost if cost else "") + "]"
                out.append(row)
            if len(logs) > 4:
                out.append("  ... and " + str(len(logs) - 4) + " more")
            out.append("")

        out.append("Reply *summary* for full overview or *pending* to see outstanding items.")
        return _reply("\n".join(out))
    except Exception as e:
        return _reply("Couldn't get logs for that period. (" + str(e)[:80] + ")")


# ── Correction handler ────────────────────────────────────────────────────────
def handle_correction(from_number, msg):
    try:
        encoded = encode_number(from_number)
        logs = db_get("site_logs?from_number=eq." + encoded + "&order=created_at.desc&limit=1")
        if not isinstance(logs, list) or not logs:
            return handle_log(from_number, msg)

        last = logs[0]
        log_id = last["id"]
        msg_lower = msg.lower()
        updates = {}

        # ── Type correction (most important — check first) ──────────────
        type_keywords = {
            "VARIATION": ["variation", "vo", "it's a variation", "its a variation",
                          "variation not", "change to variation", "should be variation",
                          "it was variation", "it was a variation"],
            "DAYWORK":   ["daywork", "day work", "change to daywork", "should be daywork"],
            "MATERIAL_ORDER": ["material order", "purchase order", "po", "material"],
        }
        detected_type = None
        for t, keywords in type_keywords.items():
            if any(kw in msg_lower for kw in keywords):
                detected_type = t
                break
        if detected_type:
            updates["type"] = detected_type

        # ── Cost correction ───────────────────────────────────────────────
        cost_match = re.search(r"[£$]?\s*(\d+(?:\.\d+)?)\s*(?:quid|pounds?)?", msg)
        if cost_match and any(kw in msg_lower for kw in ["£", "quid", "pound", "cost", "price", "not £", "its £"]):
            updates["cost_estimate"] = float(cost_match.group(1))

        # ── Hours correction ──────────────────────────────────────────────
        hours_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)", msg_lower)
        if hours_match:
            updates["hours"] = float(hours_match.group(1))

        # ── Description correction ────────────────────────────────────────
        no_match = re.search(r"(?:not|wrong|no)\s+.{0,30}(?:it[''s]*|actually|should be|i meant)\s+(.+)", msg_lower)
        if no_match and "type" not in updates and "cost_estimate" not in updates:
            updates["description"] = no_match.group(1).strip().capitalize()

        if not updates:
            return handle_log(from_number, msg)

        for field, value in updates.items():
            db_patch("site_logs?id=eq." + str(log_id), {field: value})

        desc = last.get("description", "last entry")[:40]
        parts = []
        if "type" in updates: parts.append("type changed to " + updates["type"].replace("_"," ").title())
        if "cost_estimate" in updates: parts.append("cost updated to £" + ("%.2f" % updates["cost_estimate"]))
        if "hours" in updates: parts.append("hours updated to " + str(updates["hours"]))
        if "description" in updates: parts.append("description updated")

        return _reply("✅ Corrected — " + (", ".join(parts) if parts else "updated") +
                      "\nEntry: " + desc)
    except Exception as e:
        return handle_log(from_number, msg)


# ── Material order reminder ───────────────────────────────────────────────────
def send_reminder_message(from_number, log_id, description):
    try:
        from twilio.rest import Client as _TC
        client = _TC(os.environ.get("TWILIO_ACCOUNT_SID", ""),
                     os.environ.get("TWILIO_AUTH_TOKEN", ""))
        client.messages.create(
            from_=os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886"),
            to=from_number,
            body=("⏰ Material order reminder\n\n" + description[:60] +
                  "\n\nReply done if sorted, or snooze to remind you tomorrow."))
    except Exception as e:
        print("Reminder send error:", e)
