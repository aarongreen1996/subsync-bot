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
APP_URL                = os.environ.get("APP_URL", "https://www.note2quote.co.uk")
STRIPE_PAYMENT_LINK    = os.environ.get("STRIPE_PAYMENT_LINK", "https://note2quote.co.uk/signup")


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

def get_logs(whatsapp, limit=200):
    return db_get(f"site_logs?from_number=eq.{encode(whatsapp)}&order=created_at.desc&limit={limit}")


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

        # ── Day 3 — Check in ─────────────────────────────────────────────
        if days >= 3 and not has_sent(company_id, "day3_checkin"):
            msg = "\n".join([
                f"👋 Hey {name}! Just checking in on your Note2Quote trial.",
                "",
                "Have you managed to log anything from site yet?",
                "If you're stuck or anything feels confusing just reply here — I'll sort it.",
                "",
                "Quick reminder:",
                "🎤 Just send a voice note or text from site, e.g.",
                "'Extra rad in bedroom 3, 2 hours, £80'",
                "",
                "Reply *help* for the full guide 👷"
            ])
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day3_checkin")

        # ── Day 7 — Tips ─────────────────────────────────────────────────
        elif days >= 7 and not has_sent(company_id, "day7_tips"):
            msg = "\n".join([
                f"💡 *Note2Quote Tip — Material Orders*",
                "",
                "Did you know you can log material orders too?",
                "",
                "Just say:",
                "'Need to order 50 joist hangers from Screwfix for Brookfield Site'",
                "",
                "It saves it as a Purchase Order and generates a proper PO PDF.",
                "No more scribbling on paper and losing track.",
                "",
                "Reply *help* to see all commands 👍"
            ])
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day7_tips")

        # ── Day 10 — Trial warning (4 days left) ─────────────────────────
        elif days >= 10 and not has_sent(company_id, "day10_trial_warning"):
            logs  = get_logs(whatsapp)
            total = len(logs) if isinstance(logs, list) else 0
            pend_val = sum(float(l.get("cost_estimate") or 0)
                          for l in logs if isinstance(l, dict) and l.get("status") == "pending") if isinstance(logs, list) else 0
            msg = "\n".join([
                f"⏰ *{name} — 4 days left on your free trial*",
                "",
                f"You've logged *{total} items* worth *£{pend_val:.0f}* so far.",
                "",
                "Your trial ends in 4 days. After that it's *£49/month* to keep going.",
                "",
                "If you want to continue just head here to set up payment:",
                STRIPE_PAYMENT_LINK,
                "",
                "Any questions just reply here 👍"
            ])
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day10_trial_warning")

        # ── Day 12 — Trial ending soon (2 days left) ─────────────────────
        elif days >= 12 and not has_sent(company_id, "day12_trial_ending"):
            msg = "\n".join([
                f"🔔 *{name} — trial ends in 2 days*",
                "",
                "Just a heads up — your free Note2Quote trial ends on day 14.",
                "",
                "To keep all your logs, sites and dashboard access:",
                f"👉 {STRIPE_PAYMENT_LINK}",
                "",
                "It's £49/month — less than an hour's labour.",
                "Cancel anytime, no contracts.",
                "",
                "Want to keep going? Tap the link above and you're sorted ✅"
            ])
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day12_trial_ending")

        # ── Day 14 — Trial ended ──────────────────────────────────────────
        elif days >= 14 and not has_sent(company_id, "day14_trial_ended"):
            logs  = get_logs(whatsapp)
            total = len(logs) if isinstance(logs, list) else 0
            pend_val = sum(float(l.get("cost_estimate") or 0)
                          for l in logs if isinstance(l, dict) and l.get("status") == "pending") if isinstance(logs, list) else 0
            msg = "\n".join([
                f"⚠️ *{name} — your free trial has ended*",
                "",
                f"You logged *{total} items* worth *£{pend_val:.0f}* during your trial.",
                "",
                "To keep using Note2Quote and keep all your data:",
                f"👉 {STRIPE_PAYMENT_LINK}",
                "",
                "£49/month. No contracts. Cancel anytime.",
                "",
                "If you want to chat or have any questions just reply here.",
                "Thanks for trialling Note2Quote 🙏"
            ])
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day14_trial_ended")

        # ── Day 30 — Monthly review ───────────────────────────────────────
        elif days >= 30 and not has_sent(company_id, "day30_monthly"):
            logs  = get_logs(whatsapp)
            total = len(logs) if isinstance(logs, list) else 0
            sent  = sum(1 for l in logs if isinstance(l, dict) and l.get("status") == "sent") if isinstance(logs, list) else 0
            msg = "\n".join([
                f"📊 *{name} — your first month with Note2Quote*",
                "",
                f"This month you logged *{total} items* and sent *{sent} documents*.",
                "",
                "Keep logging everything on site — every variation logged is money protected 💰",
                "",
                "Reply *summary* to see your full overview."
            ])
            if send_whatsapp(whatsapp, msg):
                mark_sent(company_id, "day30_monthly")


# ── MONDAY WEEKLY SUMMARY ─────────────────────────────────────────────────────
def run_weekly_summary():
    now = datetime.now(timezone.utc)
    if now.weekday() != 0 or now.hour not in (7, 8, 9): return

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

        # Calculate this week's earnings across all work types
        from datetime import date as _d
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%dT00:00:00+00:00").replace("+","%2B")
        week_all   = db_get(f"site_logs?from_number=eq.{encode(whatsapp)}&created_at=gte.{week_start}")
        week_earned = 0
        if isinstance(week_all, list):
            week_earned = sum(float(l.get("cost_estimate") or 0) for l in week_all
                              if l.get("type") in ["STANDARD_WORK","TIMESHEET","VARIATION","DAYWORK"])

        earned_line = f"💰 *Earned this week:* £{week_earned:.0f}" if week_earned else ""
        msg_lines = [
            f"☀️ *Good morning {name}! Weekly summary*",
            "",
        ]
        if earned_line: msg_lines.append(earned_line)
        msg_lines += [
            f"📋 *Pending variations:* {len(pending)} items · £{pend_val:.2f}",
            f"⏰ *Chasing client:* {len(chasing)} items · £{chase_val:.2f}",
            f"✅ *Approved:* {len(approved)} items",
            "",
            f"📍 Active sites: {', '.join(sites[:3])}",
            "",
            "Reply *how did I do this week* for full breakdown.",
            "Reply *generate variations for [site]* to claim.",
            "",
            "Have a great week 💪"
        ]
        msg = "\n".join(msg_lines)

        if send_whatsapp(whatsapp, msg):
            mark_sent(company_id, msg_type)


# ── CHASING REMINDER (Mon + Thu 9am) ─────────────────────────────────────────
def run_chasing_reminders():
    now = datetime.now(timezone.utc)
    if now.weekday() not in (0, 3) or now.hour not in (8, 9, 10): return

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

        old_chasing = []
        for l in chasing:
            try:
                date_str = l.get("created_at", "")
                if date_str:
                    created = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    if (now - created).days >= 3:
                        old_chasing.append(l)
                else:
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
        lines.append(f"\nReply *summary* to see full details.")

        if send_whatsapp(whatsapp, "\n".join(lines)):
            mark_sent(company_id, msg_type)


# ── HIGH VALUE ALERT (daily 8am) ──────────────────────────────────────────────
def run_high_value_alerts():
    now = datetime.now(timezone.utc)
    if now.hour not in (7, 8, 9): return

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
        lines = [f"🚨 *Priority — high value items pending*\n"]
        for l in alerts:
            desc = l.get("description","—")[:40]
            site = l.get("site_name","—")
            cost = float(l.get("cost_estimate") or 0)
            lines.append(f"• £{cost:.0f} — {desc} ({site})")
        lines.append(f"\n*Total: £{total:.0f}* — pending for 7+ days.")
        lines.append(f"\nReply *approve [site name]* or chase your client today.")

        if send_whatsapp(whatsapp, "\n".join(lines)):
            mark_sent(company_id, msg_type)


# ── FRIDAY NUDGE (Friday 4pm) ─────────────────────────────────────────────────
def run_friday_nudge():
    now = datetime.now(timezone.utc)
    if now.weekday() != 4 or now.hour not in (15, 16, 17): return

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

        msg = "\n".join([
            f"📋 *Friday reminder — {len(old_pending)} items need documents*",
            "",
            f"You have items logged 14+ days ago not yet turned into PDFs.",
            "",
            f"Sites: {', '.join(sites[:3])}",
            f"Est. value: *£{val:.0f}*",
            "",
            "Generate before the weekend:",
            "'Generate variations for [Site Name]'",
            "",
            "Have a good weekend 🍺"
        ])

        if send_whatsapp(whatsapp, msg):
            mark_sent(company_id, msg_type)


# ── MONTH END SUMMARY ─────────────────────────────────────────────────────────
def run_month_end_summary():
    now = datetime.now(timezone.utc)
    if now.weekday() != 4: return

    next_friday = now + timedelta(days=7)
    if next_friday.month == now.month: return

    if now.hour not in (8, 9, 10): return

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

        msg = "\n".join([
            f"📅 *{month} month end — {name}*",
            "",
            f"✅ Approved: {len(approved)} items · £{appr_val:.2f}",
            f"⏰ Still chasing: {len(chasing)} items · £{chase_val:.2f}",
            f"📋 Pending (not yet sent): {len(pending)} items · £{pend_val:.2f}",
            f"📄 Docs sent: {len(sent)}",
            "",
            "⚠️ Make sure you've generated and sent all outstanding documents before month closes!",
            "",
            "Reply *summary* for full breakdown."
        ])

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
        time.sleep(3600)


def start_scheduler():
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    return thread
