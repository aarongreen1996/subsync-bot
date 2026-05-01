import os
import stripe
from flask import Blueprint, request, jsonify, send_file, redirect
import requests as http_requests
from twilio.rest import Client

onboarding_bp = Blueprint("onboarding", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
APP_URL = os.environ.get("APP_URL", "https://web-production-b725f.up.railway.app")

stripe.api_key = STRIPE_SECRET_KEY


def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def db_post(path, payload):
    r = http_requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        json=payload,
        headers=sb_headers()
    )
    return r


# ── Serve pages ───────────────────────────────────────────────────────────────
@onboarding_bp.route("/")
def landing():
    return send_file("landing.html")


@onboarding_bp.route("/signup")
def signup_page():
    return send_file("signup.html")


@onboarding_bp.route("/welcome")
def welcome_page():
    return """
    <!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>Welcome to SubSync!</title>
    <style>
      body { font-family: -apple-system, sans-serif; display: flex;
             align-items: center; justify-content: center; min-height: 100vh;
             background: #f8fafc; margin: 0; }
      .box { background: white; border-radius: 16px; padding: 48px;
             text-align: center; max-width: 480px; box-shadow: 0 4px 24px rgba(0,0,0,.1); }
      h1 { color: #1a3a6b; font-size: 28px; margin-bottom: 12px; }
      p { color: #64748b; line-height: 1.7; margin-bottom: 16px; }
      .highlight { color: #10b981; font-weight: 700; font-size: 18px; }
      a { display: inline-block; background: #1a3a6b; color: white;
          padding: 14px 28px; border-radius: 8px; text-decoration: none;
          font-weight: 700; margin-top: 16px; }
    </style></head><body>
    <div class="box">
      <div style="font-size:56px;margin-bottom:16px">🎉</div>
      <h1>You're all set!</h1>
      <p class="highlight">Check WhatsApp — your bot is ready.</p>
      <p>We've sent you a welcome message with everything you need to get started. 
      Log your first variation in the next 60 seconds — it really is that easy.</p>
      <p>Your dashboard is ready at:</p>
      <a href="/dashboard">Open Dashboard →</a>
    </div></body></html>
    """


# ── Sign up API ───────────────────────────────────────────────────────────────
@onboarding_bp.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.json or {}

    required = ["name", "email", "company_name", "whatsapp", "trade"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    whatsapp = data["whatsapp"].strip()
    if not whatsapp.startswith("+"):
        whatsapp = "+" + whatsapp
    whatsapp_full = f"whatsapp:{whatsapp}"

    try:
        # Create Stripe customer
        customer = stripe.Customer.create(
            email=data["email"],
            name=data["name"],
            metadata={
                "company_name": data["company_name"],
                "whatsapp":     whatsapp_full,
                "trade":        data.get("trade", ""),
                "vat":          data.get("vat", ""),
                "address":      data.get("address", ""),
                "primary_color":data.get("primary_color", "#1a3a6b"),
            }
        )

        # Create Stripe checkout session with 14-day trial
        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            subscription_data={"trial_period_days": 14},
            success_url=f"{APP_URL}/welcome",
            cancel_url=f"{APP_URL}/signup",
        )

        return jsonify({"checkout_url": session.url})

    except stripe.error.StripeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Stripe webhook ────────────────────────────────────────────────────────────
@onboarding_bp.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # When checkout is complete — provision the company
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.get("customer")

        try:
            customer = stripe.Customer.retrieve(customer_id)
            meta = customer.get("metadata", {})

            whatsapp = meta.get("whatsapp", "")
            company_name = meta.get("company_name", "")
            email = customer.get("email", "")
            phone = whatsapp.replace("whatsapp:", "")

            # Create company record in Supabase
            db_post("companies", {
                "whatsapp_number": whatsapp,
                "company_name":    company_name,
                "address":         meta.get("address", ""),
                "email":           email,
                "phone":           phone,
                "vat_number":      meta.get("vat", ""),
                "primary_color":   meta.get("primary_color", "#1a3a6b"),
            })

            # Send welcome WhatsApp message
            send_welcome_message(whatsapp, company_name)

        except Exception as e:
            print(f"Provisioning error: {e}")

    return jsonify({"ok": True})


def send_welcome_message(to_number, company_name):
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_number,
            body=(
                f"👋 Welcome to SubSync, {company_name}!\n\n"
                f"Your AI admin assistant is ready. Here's how to get started:\n\n"
                f"*LOG A VARIATION*\n"
                f"Just text naturally — e.g:\n"
                f"_'Site manager wants extra sockets in room 4, 2 hours, £40 materials'_\n\n"
                f"*LOG A MATERIAL ORDER*\n"
                f"_'Need to order 50 joist hangers from Travis Perkins'_\n\n"
                f"*GENERATE DOCUMENTS*\n"
                f"_'Generate variations for [Site Name]'_\n\n"
                f"*GET HELP ANYTIME*\n"
                f"Just text: *Help*\n\n"
                f"Your dashboard: {os.environ.get('APP_URL', '')}/dashboard\n\n"
                f"Happy building! ⚡"
            )
        )
    except Exception as e:
        print(f"Welcome message error: {e}")
