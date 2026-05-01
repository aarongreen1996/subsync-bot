import os
import stripe
from flask import Blueprint, request, jsonify, send_file
import requests as http_requests
from datetime import datetime

admin_bp = Blueprint("admin", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin-changeme")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

stripe.api_key = STRIPE_SECRET_KEY


def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def db_get(path):
    r = http_requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers())
    return r.json()


def check_auth():
    return request.headers.get("X-Admin-Password", "") == ADMIN_PASSWORD


# ── Serve admin HTML ───────────────────────────────────────────────────────────
@admin_bp.route("/admin")
def serve_admin():
    return send_file("admin.html")


# ── Auth ──────────────────────────────────────────────────────────────────────
@admin_bp.route("/api/admin/auth", methods=["POST"])
def auth():
    data = request.json or {}
    if data.get("password") == ADMIN_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Wrong password"}), 401


# ── Overview stats ────────────────────────────────────────────────────────────
@admin_bp.route("/api/admin/overview")
def overview():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    companies  = db_get("companies?order=created_at.desc")
    all_logs   = db_get("site_logs?select=id,status,from_number,created_at")
    all_projects = db_get("projects?select=id,whatsapp_number,site_name")

    if not isinstance(companies, list):  companies = []
    if not isinstance(all_logs, list):   all_logs = []
    if not isinstance(all_projects, list): all_projects = []

    total_customers = len(companies)
    total_logs      = len(all_logs)
    total_sent      = sum(1 for l in all_logs if l.get("status") == "sent")
    total_pending   = sum(1 for l in all_logs if l.get("status") == "pending")

    # Get Stripe subscriptions
    stripe_data = {}
    trial_count = 0
    active_count = 0
    mrr = 0.0

    try:
        subs = stripe.Subscription.list(limit=100, status="all")
        for sub in subs.auto_paging_iter():
            customer_id = sub.get("customer")
            stripe_data[customer_id] = {
                "status":       sub.get("status"),
                "trial_end":    sub.get("trial_end"),
                "current_period_end": sub.get("current_period_end"),
                "id":           sub.get("id"),
            }
            if sub.get("status") == "trialing":
                trial_count += 1
            elif sub.get("status") == "active":
                active_count += 1
                mrr += 49.0
    except Exception:
        pass

    # Build customer list with usage
    customer_list = []
    for company in companies:
        number = company.get("whatsapp_number", "")
        company_logs = [l for l in all_logs if l.get("from_number") == number]
        company_projects = [p for p in all_projects if p.get("whatsapp_number") == number]

        # Try to find Stripe customer
        stripe_status = "unknown"
        stripe_sub_id = None
        try:
            customers = stripe.Customer.list(email=company.get("email", ""), limit=1)
            if customers.data:
                cust = customers.data[0]
                sd = stripe_data.get(cust.id, {})
                stripe_status = sd.get("status", "no_subscription")
                stripe_sub_id = sd.get("id")
        except Exception:
            pass

        customer_list.append({
            "id":             company.get("id"),
            "company_name":   company.get("company_name", "Unknown"),
            "email":          company.get("email", ""),
            "phone":          company.get("phone", ""),
            "whatsapp":       number,
            "trade":          company.get("trade", ""),
            "created_at":     company.get("created_at", ""),
            "primary_color":  company.get("primary_color", "#1a3a6b"),
            "total_logs":     len(company_logs),
            "sent_docs":      sum(1 for l in company_logs if l.get("status") == "sent"),
            "pending_items":  sum(1 for l in company_logs if l.get("status") == "pending"),
            "sites":          len(company_projects),
            "stripe_status":  stripe_status,
            "stripe_sub_id":  stripe_sub_id,
        })

    return jsonify({
        "total_customers": total_customers,
        "trial_count":     trial_count,
        "active_count":    active_count,
        "mrr":             mrr,
        "total_logs":      total_logs,
        "total_sent":      total_sent,
        "total_pending":   total_pending,
        "customers":       customer_list,
    })


# ── Customer detail ───────────────────────────────────────────────────────────
@admin_bp.route("/api/admin/customer/<int:company_id>")
def customer_detail(company_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    companies = db_get(f"companies?id=eq.{company_id}&limit=1")
    if not isinstance(companies, list) or not companies:
        return jsonify({"error": "Not found"}), 404

    company = companies[0]
    number  = company.get("whatsapp_number", "").replace("+", "%2B")
    logs    = db_get(f"site_logs?from_number=eq.{number}&order=created_at.desc&limit=50")
    projects = db_get(f"projects?whatsapp_number=eq.{number}&order=site_name.asc")

    if not isinstance(logs, list): logs = []
    if not isinstance(projects, list): projects = []

    return jsonify({
        "company":  company,
        "logs":     logs,
        "projects": projects,
    })


# ── Cancel subscription ───────────────────────────────────────────────────────
@admin_bp.route("/api/admin/cancel-subscription", methods=["POST"])
def cancel_subscription():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data   = request.json or {}
    sub_id = data.get("subscription_id")

    if not sub_id:
        return jsonify({"error": "No subscription ID provided"}), 400

    try:
        stripe.Subscription.cancel(sub_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
