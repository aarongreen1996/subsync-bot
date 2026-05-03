import os
import threading
import time
from datetime import datetime, timezone
import requests as http_requests
from twilio.rest import Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
APP_URL = os.environ.get("APP_URL", "https://www.note2quote.co.uk")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")


def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json() if r.status_code == 200 else []


def db_post(path, payload):
    http_requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        json=payload,
        headers={**sb_headers(), "Prefer": "return=minimal"}
    )


def send_whatsapp(to_number, message):
    """Send a WhatsApp message via Twilio."""
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_number,
            body=message
        )
        return True
    except Exception as e:
        print(f"WhatsApp send error to {to_number}: {e}")
        return False


def has_message_been_sent(company_id, message_type):
    """Check if we've already sent this message type to this company."""
    result = db_get(
        f"onboarding_messages?company_id=eq.{company_id}"
        f"&message_type=eq.{message_type}&limit=1"
    )
    return isinstance(result, list) and len(result) > 0


def mark_message_sent(company_id, message_type):
    """Record that we've sent this message."""
    db_post("onboarding_messages", {
        "company_id": company_id,
        "message_type": message_type,
        "sent_at": datetime.now(timezone.utc).isoformat()
    })


def run_onboarding_drip():
    """Check all companies and send appropriate drip messages."""
    companies = db_get("companies?order=created_at.asc")
    if not isinstance(companies, list):
        return

    now = datetime.now(timezone.utc)

    for company in companies:
        whatsapp = company.get("whatsapp_number", "")
        company_id = company.get("id")
        company_name = company.get("company_name", "")
        created_raw = company.get("created_at", "")

        if not whatsapp or not created_raw:
            continue

        try:
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except Exception:
            continue

        days_since_signup = (now - created_at).days

        # ── Day 0: Welcome + credentials (sent by onboarding webhook already)
        # ── Day 3: Check-in ───────────────────────────────────────────────────
        if days_since_signup >= 3 and not has_message_been_sent(company_id, "day3_checkin"):
            msg = (
                f"👋 Hey {company_name}! Just checking in — "
                f"how are you getting on with Note2Quote?\n\n"
                f"Have you logged your first variation yet? "
                f"If you need any help just reply *Help* and I'll walk you through it 💪"
            )
            if send_whatsapp(whatsapp, msg):
                mark_message_sent(company_id, "day3_checkin")

        # ── Day 7: Tips message ───────────────────────────────────────────────
        elif days_since_signup >= 7 and not has_message_been_sent(company_id, "day7_tips"):
            msg = (
                f"💡 *Note2Quote Tip of the Week*\n\n"
                f"Did you know you can log *material orders* too?\n\n"
                f"Just say something like:\n"
                f"_'Need to order 50 joist hangers from Screwfix for Brookfield Site'_\n\n"
                f"I'll save it and include it in your next Purchase Order PDF automatically 📦\n\n"
                f"Reply *Help* anytime to see all commands."
            )
            if send_whatsapp(whatsapp, msg):
                mark_message_sent(company_id, "day7_tips")

        # ── Day 12: Trial ending warning ──────────────────────────────────────
        elif days_since_signup >= 12 and not has_message_been_sent(company_id, "day12_trial_ending"):
            msg = (
                f"⏰ *Your Note2Quote trial ends in 2 days*\n\n"
                f"If you're happy with Note2Quote, your £49/month subscription "
                f"will start automatically — no action needed.\n\n"
                f"If you'd like to cancel, you can do so any time at:\n"
                f"{APP_URL}/dashboard\n\n"
                f"Any questions? Just reply here and we'll help 👍"
            )
            if send_whatsapp(whatsapp, msg):
                mark_message_sent(company_id, "day12_trial_ending")

        # ── Day 30: Monthly check-in ──────────────────────────────────────────
        elif days_since_signup >= 30 and not has_message_been_sent(company_id, "day30_monthly"):
            logs = db_get(
                f"site_logs?from_number=eq.{whatsapp.replace('+', '%2B')}"
                f"&created_at=gte.{created_raw[:10]}&select=id,status"
            )
            total = len(logs) if isinstance(logs, list) else 0
            sent  = sum(1 for l in logs if isinstance(l, dict) and l.get("status") == "sent") if isinstance(logs, list) else 0

            msg = (
                f"📊 *Your Note2Quote month in review*\n\n"
                f"This month you logged *{total} items* and generated *{sent} documents*.\n\n"
                f"Keep logging everything on site — every variation logged is money protected 💰\n\n"
                f"Reply *Help* to see all commands."
            )
            if send_whatsapp(whatsapp, msg):
                mark_message_sent(company_id, "day30_monthly")


def run_weekly_summary():
    """Send Monday morning summary to all active companies."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 0:  # 0 = Monday
        return
    if now.hour != 8:  # 8am UTC
        return

    companies = db_get("companies?order=created_at.asc")
    if not isinstance(companies, list):
        return

    today = now.strftime("%Y-%m-%d")

    for company in companies:
        whatsapp = company.get("whatsapp_number", "")
        company_id = company.get("id")
        if not whatsapp:
            continue

        # Only send once per Monday
        message_type = f"weekly_summary_{today}"
        if has_message_been_sent(company_id, message_type):
            continue

        encoded = whatsapp.replace("+", "%2B")
        pending = db_get(
            f"site_logs?from_number=eq.{encoded}&status=eq.pending&select=id,cost_estimate,site_name"
        )
        if not isinstance(pending, list) or not pending:
            continue

        total_value = sum(float(l.get("cost_estimate") or 0) for l in pending)
        sites = list(set(l.get("site_name", "Unassigned") for l in pending))
        site_list = ", ".join(sites[:3])

        msg = (
            f"☀️ *Good morning! Your Note2Quote weekly summary*\n\n"
            f"You have *{len(pending)} pending items* worth *£{total_value:.0f}*\n"
            f"Sites: {site_list}\n\n"
            f"Ready to generate documents? Just say:\n"
            f"_'Generate variations for [Site Name]'_\n\n"
            f"Have a great week on site 💪"
        )

        if send_whatsapp(whatsapp, msg):
            mark_message_sent(company_id, message_type)


def scheduler_loop():
    """Main scheduler loop — runs checks every hour."""
    print("Scheduler started ✅")
    while True:
        try:
            print(f"[Scheduler] Running checks at {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
            run_onboarding_drip()
            run_weekly_summary()
        except Exception as e:
            print(f"[Scheduler] Error: {e}")
        time.sleep(3600)  # Run every hour


def start_scheduler():
    """Start the scheduler in a background thread."""
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    return thread
