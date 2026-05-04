import os
import threading
import time
from datetime import datetime, timezone, timedelta
import requests as http_requests
from twilio.rest import Client

SUPABASE_URL           = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY           = os.environ.get("SUPABASE_KEY", "")
TWILIO_ACCOUNT_SID     = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN      = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
APP_URL                = os.environ.get("APP_URL", "https://www.subsync.xyz")


def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}

def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json() if r.status_code == 200 else []

def db_post(path, payload):
    http_requests.post(f"{SUPABASE_URL}/rest/v1/{path}", json=payload,
                       headers={**sb_headers(), "Prefer": "return=minimal"})

def send_whatsapp(to_number, message):
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=to_number, body=message)
        return True
    except Exception as e:
        print(f"WhatsApp send error to {to_number}: {e}")
        return False

def has_sent(company_id, message_type):
    result = db_get(f"onboarding_messages?company_id=eq.{company_id}&message_type=eq.{message_type}&limit=1")
    return isinstance(result, list) and len(result) > 0

def mark_sent(company_id, message_type):
    db_post("onboarding_messages", {
        "company_id":   company_id,
        "message_type": message_type,
        "sent_at":      datetime.now(timezone.utc).isoformat()
    })

def encode(n):
    return n.replace("+", "%2B")

def get_logs(whatsapp):
    return db_get(f"site_logs?from_number=eq.{encode(whatsapp)}&order=created_at.desc")


# ── ONBOARDING DRIP ───────────────────────────────────────────────────────────
def run_onboarding_drip():
    companies = db_get("companies?order=created_at.asc")
    if not isinstance(companies, list): return
    now = datetime.now(timezone.utc)

    for company in companies:
        whatsapp   = company.get("whatsapp_number", "")
        company_id = company.get("id")
        name       = company.get("company_name", "")
        created_raw = company.get("created_at", "")
        if not whatsapp or not created_raw: continue

        try:
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except Exception:
            continue

        days = (now - created_at).days

        if days >= 3 and not has_sent(company_id, "day3_checkin"):
            msg = (f"👋 Hey {name}! Just checking in — how are you getting on with Note2Quote?\n\n"
                   f"Have you logged your first variation yet? Reply *Help* if you need a hand 💪")
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day3_checkin")

        elif days >= 7 and not has_sent(company_id, "day7_tips"):
            msg = (f"💡 *Note2Quote Tip*\n\nDid you know you can log material orders too?\n\n"
                   f"Just say: _'Need to order 50 joist hangers from Screwfix for Brookfield Site'_\n\n"
                   f"It'll save it and include it in your next Purchase Order PDF 📦\n\n"
                   f"Reply *Help* to see all commands.")
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day7_tips")

        elif days >= 12 and not has_sent(company_id, "day12_trial_ending"):
            msg = (f"⏰ *Your trial ends in 2 days*\n\n"
                   f"If you're happy with Note2Quote, your £49/month subscription starts automatically.\n\n"
                   f"To cancel: {APP_URL}/account\n\n"
                   f"Any questions? Just reply here 👍")
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day12_trial_ending")

        elif days >= 30 and not has_sent(company_id, "day30_monthly"):
            logs  = get_logs(whatsapp)
            total = len(logs) if isinstance(logs, list) else 0
            sent  = sum(1 for l in logs if isinstance(l, dict) and l.get("status") == "sent") if isinstance(logs, list) else 0
            msg = (f"📊 *Your Note2Quote month in review*\n\n"
                   f"This month you logged *{total} items* and sent *{sent} documents*.\n\n"
                   f"Keep logging everything on site — every variation logged is money protected 💰\n\n"
                   f"Reply *summary* to see your full overview.")
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day30_monthly")


# ── MONDAY WEEKLY SUMMARY ─────────────────────────────────────────────────────
def run_weekly_summary():
    now = datetime.now(timezone.utc)
    if now.weekday() != 0 or now.hour != 8: return

    companies = db_get("companies?order=created_at.asc")
    if not isinstance(companies, list): return
    today = now.strftime("%Y-%m-%d")

    for company in companies:
        whatsapp   = company.get("whatsapp_number", "")
        company_id = company.get("id")
        name       = company.get("company_name", "")
        if not whatsapp: continue

        msg_type = f"weekly_summary_{today}"
        if has_sent(company_id, msg_type): continue

        logs     = get_logs(whatsapp)
        if not isinstance(logs, list): continue
        pending  = [l for l in logs if l.get("status") == "pending"]
        chasing  = [l for l in logs if l.get("status") == "chasing"]
        approved = [l for l in logs if l.get("status") == "approved"]
        if not pending and not chasing: continue

        pend_val  = sum(float(l.get("cost_estimate") or 0) for l in pending)
        chase_val = sum(float(l.get("cost_estimate") or 0) for l in chasing)
        sites     = list(set(l.get("site_name", "Unassigned") for l in pending))

        msg = (f"☀️ *Good morning {name}! Your weekly summary*\n\n"
               f"📋 *Pending:* {len(pending)} items · £{pend_val:.2f}\n"
               f"⏰ *Chasing client:* {len(chasing)} items · £{chase_val:.2f}\n"
               f"✅ *Approved:* {len(approved)} items\n\n"
               f"📍 Active sites: {', '.join(sites[:3])}\n\n"
               f"Ready to generate documents?\n"
               f"_'Generate variations for [Site Name]'_\n\n"
               f"Have a great week 💪")

        if send_whatsapp(whatsapp, msg):
            mark_sent(company_id, msg_type)


# ── CHASING REMINDER (Mon + Thu 9am) ─────────────────────────────────────────
def run_chasing_reminders():
    now = datetime.now(timezone.utc)
    # Run Monday and Thursday at 9am UTC
    if now.weekday() not in (0, 3) or now.hour != 9: return

    companies = db_get("companies?order=created_at.asc")
    if not isinstance(companies, list): return
    today = now.strftime("%Y-%m-%d")

    for company in companies:
        whatsapp   = company.get("whatsapp_number", "")
        company_id = company.get("id")
        name       = company.get("company_name", "")
        if not whatsapp: continue

        msg_type = f"chasing_reminder_{today}"
        if has_sent(company_id, msg_type): continue

        logs    = get_logs(whatsapp)
        if not isinstance(logs, list): continue
        chasing = [l for l in logs if l.get("status") == "chasing"]
        if not chasing: continue

        # Only flag items chasing for 3+ days
        old_chasing = []
        for l in chasing:
            try:
                updated = datetime.fromisoformat((l.get("updated_at") or l.get("created_at", "")).replace("Z", "+00:00"))
                if (now - updated).days >= 3:
                    old_chasing.append(l)
            except Exception:
                old_chasing.append(l)

        if not old_chasing: continue

        chase_val = sum(float(l.get("cost_estimate") or 0) for l in old_chasing)
        by_site   = {}
        for l in old_chasing:
            site = l.get("site_name") or "Unassigned"
            by_site[site] = by_site.get(site, 0) + 1

        lines = [f"⏰ *Chasing reminder — {len(old_chasing)} items need following up*\n"]
        for site, count in by_site.items():
            lines.append(f"📍 {site} — {count} item(s) awaiting approval")
        lines.append(f"\nTotal value chasing: *£{chase_val:.2f}*")
        lines.append(f"\nReply *summary* to see full details or contact your clients to get these approved.")

        if send_whatsapp(whatsapp, "\n".join(lines)):
            mark_sent(company_id, msg_type)


# ── HIGH VALUE ALERT (daily 8am) ──────────────────────────────────────────────
def run_high_value_alerts():
    now = datetime.now(timezone.utc)
    if now.hour != 8: return

    companies = db_get("companies?order=created_at.asc")
    if not isinstance(companies, list): return
    today = now.strftime("%Y-%m-%d")

    for company in companies:
        whatsapp   = company.get("whatsapp_number", "")
        company_id = company.get("id")
        if not whatsapp: continue

        msg_type = f"high_value_alert_{today}"
        if has_sent(company_id, msg_type): continue

        logs    = get_logs(whatsapp)
        if not isinstance(logs, list): continue
        pending = [l for l in logs if l.get("status") == "pending"]

        # Find items over £500 that are 7+ days old
        alerts = []
        for l in pending:
            cost = float(l.get("cost_estimate") or 0)
            if cost < 500: continue
            try:
                created = datetime.fromisoformat((l.get("created_at","")).replace("Z","+00:00"))
                if (now - created).days >= 7:
                    alerts.append(l)
            except Exception:
                pass

        if not alerts: continue

        total = sum(float(l.get("cost_estimate") or 0) for l in alerts)
        lines = [f"🚨 *Priority alert — high value items pending*\n"]
        for l in alerts:
            desc = l.get("description","—")[:40]
            site = l.get("site_name","—")
            cost = float(l.get("cost_estimate") or 0)
            lines.append(f"• £{cost:.2f} — {desc} ({site})")
        lines.append(f"\n*Total: £{total:.2f}* — these have been pending for 7+ days.")
        lines.append(f"\nReply *approve [site name]* or chase your client today.")

        if send_whatsapp(whatsapp, "\n".join(lines)):
            mark_sent(company_id, msg_type)


# ── FRIDAY UNSENT NUDGE (Friday 4pm) ─────────────────────────────────────────
def run_friday_nudge():
    now = datetime.now(timezone.utc)
    if now.weekday() != 4 or now.hour != 16: return

    companies = db_get("companies?order=created_at.asc")
    if not isinstance(companies, list): return
    today = now.strftime("%Y-%m-%d")

    for company in companies:
        whatsapp   = company.get("whatsapp_number", "")
        company_id = company.get("id")
        name       = company.get("company_name", "")
        if not whatsapp: continue

        msg_type = f"friday_nudge_{today}"
        if has_sent(company_id, msg_type): continue

        logs    = get_logs(whatsapp)
        if not isinstance(logs, list): continue
        pending = [l for l in logs if l.get("status") == "pending"]

        # Only items 14+ days old
        old_pending = []
        for l in pending:
            try:
                created = datetime.fromisoformat((l.get("created_at","")).replace("Z","+00:00"))
                if (now - created).days >= 14:
                    old_pending.append(l)
            except Exception:
                pass

        if not old_pending: continue

        val   = sum(float(l.get("cost_estimate") or 0) for l in old_pending)
        sites = list(set(l.get("site_name","Unassigned") for l in old_pending))

        msg = (f"📋 *Friday reminder — {len(old_pending)} items need documents*\n\n"
               f"You have items logged 14+ days ago that haven't been turned into PDFs yet.\n\n"
               f"Sites: {', '.join(sites[:3])}\n"
               f"Est. value: *£{val:.2f}*\n\n"
               f"Generate before the weekend:\n"
               f"_'Generate variations for [Site Name]'_\n\n"
               f"Have a good weekend 🍺")

        if send_whatsapp(whatsapp, msg):
            mark_sent(company_id, msg_type)


# ── MONTH END SUMMARY (last Friday of month) ──────────────────────────────────
def run_month_end_summary():
    now = datetime.now(timezone.utc)
    if now.weekday() != 4: return  # Friday only

    # Check if this is the last Friday of the month
    next_friday = now + timedelta(days=7)
    if next_friday.month == now.month: return  # Not last Friday

    if now.hour != 9: return

    companies = db_get("companies?order=created_at.asc")
    if not isinstance(companies, list): return
    today = now.strftime("%Y-%m-%d")

    for company in companies:
        whatsapp   = company.get("whatsapp_number", "")
        company_id = company.get("id")
        name       = company.get("company_name", "")
        if not whatsapp: continue

        msg_type = f"month_end_{today}"
        if has_sent(company_id, msg_type): continue

        logs     = get_logs(whatsapp)
        if not isinstance(logs, list): continue
        pending  = [l for l in logs if l.get("status") == "pending"]
        chasing  = [l for l in logs if l.get("status") == "chasing"]
        approved = [l for l in logs if l.get("status") == "approved"]
        sent     = [l for l in logs if l.get("status") == "sent"]

        pend_val  = sum(float(l.get("cost_estimate") or 0) for l in pending)
        appr_val  = sum(float(l.get("cost_estimate") or 0) for l in approved)
        chase_val = sum(float(l.get("cost_estimate") or 0) for l in chasing)
        month     = now.strftime("%B")

        msg = (f"📅 *{month} month end summary — {name}*\n\n"
               f"✅ Approved: {len(approved)} items · £{appr_val:.2f}\n"
               f"⏰ Still chasing: {len(chasing)} items · £{chase_val:.2f}\n"
               f"📋 Pending (not yet sent): {len(pending)} items · £{pend_val:.2f}\n"
               f"📄 Total docs sent: {len(sent)}\n\n"
               f"⚠️ Before the month closes, make sure you've generated and sent all outstanding documents!\n\n"
               f"Reply *summary* for full breakdown or *pending* to see the list.")

        if send_whatsapp(whatsapp, msg):
            mark_sent(company_id, msg_type)


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def scheduler_loop():
    print("Scheduler started ✅")
    while True:
        try:
            now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
            print(f"[Scheduler] Running checks at {now_str}")
            run_onboarding_drip()
            run_weekly_summary()
            run_chasing_reminders()
            run_high_value_alerts()
            run_friday_nudge()
            run_month_end_summary()
        except Exception as e:
            print(f"[Scheduler] Error: {e}")
        time.sleep(3600)  # Every hour


def start_scheduler():
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    return thread
