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
                f"👋 Welcome to Note2Quote, {company_name}!\n\n"
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


@onboarding_bp.route('/api/signup/logo', methods=['POST'])
def upload_signup_logo():
    """Upload logo at signup time and store against company."""
    whatsapp = request.args.get('whatsapp', '')
    if not whatsapp:
        return jsonify({'error': 'whatsapp required'}), 400

    file = request.files.get('logo')
    if not file:
        return jsonify({'error': 'No file'}), 400

    import requests as _r
    SURL = os.environ.get('SUPABASE_URL', '').rstrip('/')
    SKEY = os.environ.get('SUPABASE_KEY', '')

    content_type = file.content_type or 'image/png'
    ext = 'png' if 'png' in content_type else 'jpg' if 'jpg' in content_type else 'png'
    clean = whatsapp.replace('whatsapp:', '').replace('+', '').replace('%2B', '')
    filename = clean + '.' + ext

    r = _r.post(
        f"{SURL}/storage/v1/object/logos/{filename}",
        data=file.read(),
        headers={'apikey': SKEY, 'Authorization': f'Bearer {SKEY}',
                 'Content-Type': content_type, 'x-upsert': 'true'}
    )
    if r.status_code not in (200, 201):
        return jsonify({'error': 'Upload failed'}), 500

    logo_url = f"{SURL}/storage/v1/object/public/logos/{filename}"
    encoded  = whatsapp.replace('+', '%2B')

    _r.patch(
        f"{SURL}/rest/v1/companies?whatsapp_number=eq.{encoded}",
        json={'logo_url': logo_url},
        headers={'apikey': SKEY, 'Authorization': f'Bearer {SKEY}',
                 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}
    )
    return jsonify({'ok': True, 'logo_url': logo_url})
