# email_utils.py — uses Resend HTTP API directly (no SMTP needed)
import os
import json
try:
    import requests as http_requests
except ImportError:
    import urllib.request as http_requests

RESEND_API_KEY = os.environ.get("SMTP_PASSWORD", "")  # Resend API key stored in SMTP_PASSWORD
FROM_EMAIL = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")
FROM_NAME = os.environ.get("FROM_NAME", "Pennyfarthing Perfect Delivery")
BASE_URL = os.environ.get("PD_BASE_URL", "http://localhost:5000")


def _send(to_email: str, subject: str, html: str, pdf_bytes: bytes = None, pdf_filename: str = None):
    """Send email via Resend HTTP API, optionally with PDF attachment."""
    import requests, base64
    payload = {
        "from": f"{FROM_NAME} <{FROM_EMAIL}>",
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if pdf_bytes and pdf_filename:
        payload["attachments"] = [{
            "filename": pdf_filename,
            "content":  base64.b64encode(pdf_bytes).decode("utf-8"),
        }]
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post("https://api.resend.com/emails", json=payload, headers=headers, timeout=30)
        if r.status_code not in (200, 201):
            print(f"Resend API error {r.status_code}: {r.text}")
        else:
            print(f"Email sent to {to_email} — id: {r.json().get('id')}")
    except Exception as e:
        print(f"Email send error: {e}")


def send_manager_notification(submission: dict, plot: dict, site: dict, review_url: str):
    stage_name = submission.get("stage_name", "")
    plot_number = plot.get("plot_number", "")
    site_name = site.get("name", "")
    sub_name = submission.get("submitted_by_name", "")
    sub_company = submission.get("submitted_by_company", "")
    sub_time = submission.get("submitted_at", "")
    sub_time_str = str(sub_time)[:16].replace("T", " ") if sub_time else ""

    answers = submission.get("answers", {}) or {}
    yes_count = sum(1 for v in answers.values() if isinstance(v, dict) and v.get("value") == "yes")
    no_count  = sum(1 for v in answers.values() if isinstance(v, dict) and v.get("value") == "no")
    na_count  = sum(1 for v in answers.values() if isinstance(v, dict) and v.get("value") == "n/a")

    subject = f"PD Submission — {site_name} | Plot {plot_number} | {stage_name}"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:0}}
.w{{max-width:600px;margin:0 auto;background:#fff}}
.h{{background:#1a1a2e;padding:24px 32px}}
.h h1{{color:#C5962A;margin:0;font-size:20px}}
.h p{{color:#aaa;margin:4px 0 0;font-size:13px}}
.b{{padding:32px}}
.badge{{display:inline-block;background:#fff3cd;color:#856404;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:bold;margin-bottom:20px}}
.grid{{background:#f8f9fa;border-radius:8px;padding:16px 20px;margin:16px 0}}
.row{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #e9ecef;font-size:14px}}
.row:last-child{{border-bottom:none}}
.lbl{{color:#6c757d}} .val{{font-weight:bold;color:#212529}}
.stats{{display:flex;gap:12px;margin:20px 0}}
.stat{{flex:1;text-align:center;padding:12px;border-radius:8px}}
.sy{{background:#d4edda}} .sn{{background:#f8d7da}} .sa{{background:#e2e3e5}}
.num{{font-size:28px;font-weight:bold}} .lb{{font-size:12px;color:#555}}
.cta{{text-align:center;margin:28px 0}}
.btn{{display:inline-block;background:#C5962A;color:#fff;text-decoration:none;padding:14px 32px;border-radius:8px;font-size:16px;font-weight:bold}}
.ft{{background:#f8f9fa;padding:16px 32px;text-align:center;font-size:12px;color:#999}}
</style></head>
<body><div class="w">
<div class="h"><h1>Perfect Delivery — New Submission</h1><p>Pennyfarthing Homes · Quality Management</p></div>
<div class="b">
<span class="badge">⏳ Awaiting Your Review</span>
<p>A subcontractor has submitted a Perfect Delivery checklist for your review.</p>
<div class="grid">
  <div class="row"><span class="lbl">Site</span><span class="val">{site_name}</span></div>
  <div class="row"><span class="lbl">Plot</span><span class="val">{plot_number}</span></div>
  <div class="row"><span class="lbl">Stage</span><span class="val">{stage_name}</span></div>
  <div class="row"><span class="lbl">Submitted by</span><span class="val">{sub_name} — {sub_company}</span></div>
  <div class="row"><span class="lbl">Submitted at</span><span class="val">{sub_time_str}</span></div>
</div>
<div class="stats">
  <div class="stat sy"><div class="num" style="color:#155724">{yes_count}</div><div class="lb">Yes</div></div>
  <div class="stat sn"><div class="num" style="color:#721c24">{no_count}</div><div class="lb">No</div></div>
  <div class="stat sa"><div class="num" style="color:#383d41">{na_count}</div><div class="lb">N/A</div></div>
</div>
<p style="font-size:14px;color:#555">Review all answers and photos, then approve or reject.</p>
<div class="cta"><a href="{review_url}" class="btn">Review &amp; Decide →</a></div>
<p style="font-size:12px;color:#999;text-align:center">Or copy: {review_url}</p>
</div>
<div class="ft">Pennyfarthing Perfect Delivery · Auto-generated</div>
</div></body></html>"""

    _send(site.get("manager_email", ""), subject, html)


def send_decision_to_subcontractor(submission: dict, plot: dict, site: dict, decision: str, manager_notes: str, flagged_items: dict = None, resubmit_url: str = None, pdf_bytes: bytes = None):
    is_approved = decision == "approved"
    stage_name  = submission.get("stage_name", "")
    plot_number = plot.get("plot_number", "")
    site_name   = site.get("name", "")
    sub_name    = submission.get("submitted_by_name", "")
    sub_email   = submission.get("submitted_by_email", "")
    manager_name = site.get("manager_name", "Site Manager")

    sc = "#28a745" if is_approved else "#dc3545"
    sb = "#d4edda" if is_approved else "#f8d7da"
    st = "APPROVED ✓" if is_approved else "REJECTED ✗"
    sm = ("Your checklist has been approved. Payment can now be progressed for this stage."
          if is_approved else
          "Your checklist has been rejected. Please address the points below and resubmit.")

    subject = f"PD Stage {'Approved' if is_approved else 'Rejected'} — {site_name} | Plot {plot_number} | {stage_name}"

    notes_html = f'<div style="background:#fff3cd;border-left:4px solid #ffc107;padding:14px;margin:16px 0;border-radius:0 8px 8px 0"><strong style="color:#856404">Notes from Site Manager</strong><p style="margin-top:8px;white-space:pre-wrap;color:#333">{manager_notes}</p></div>' if manager_notes else ""

    # Flagged items
    flagged_html = ""
    if flagged_items and not is_approved:
        rows = "".join(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #fecaca;font-size:13px;color:#374151">' +
            f'Item {iid.replace("_",".")} &mdash; {(d.get("comment") or "No comment provided")}</td></tr>'
            for iid, d in (flagged_items or {}).items() if isinstance(d, dict)
        )
        if rows:
            flagged_html = (
                '<div style="margin:16px 0">' +
                '<div style="font-weight:700;color:#dc2626;font-size:13px;margin-bottom:8px">Flagged Items — Action Required</div>' +
                '<table style="width:100%;border-collapse:collapse;background:#fff5f5;border-radius:8px;overflow:hidden">' +
                rows + '</table></div>'
            )

    # Resubmit button
    resubmit_html = ""
    if resubmit_url and not is_approved:
        resubmit_html = (
            '<div style="text-align:center;margin:24px 0">' +
            f'<a href="{resubmit_url}" style="display:inline-block;background:#1a1a2e;color:#C5962A;text-decoration:none;padding:14px 32px;border-radius:8px;font-size:16px;font-weight:bold">Resubmit Stage</a>' +
            '<p style="font-size:12px;color:#999;margin-top:8px">Your previous answers are saved &mdash; only flagged items need updating</p></div>'
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:Arial,sans-serif;background:#f5f5f5;margin:0}}
.w{{max-width:600px;margin:0 auto;background:#fff}}
.h{{background:#1a1a2e;padding:24px 32px}}
.h h1{{color:#C5962A;margin:0;font-size:20px}}
.h p{{color:#aaa;margin:4px 0 0;font-size:13px}}
.b{{padding:32px}}
.grid{{background:#f8f9fa;border-radius:8px;padding:16px 20px;margin:16px 0}}
.row{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #e9ecef;font-size:14px}}
.row:last-child{{border-bottom:none}}
.lbl{{color:#6c757d}} .val{{font-weight:bold;color:#212529}}
.ft{{background:#f8f9fa;padding:16px 32px;text-align:center;font-size:12px;color:#999}}
</style></head>
<body><div class="w">
<div class="h"><h1>Perfect Delivery — Stage Decision</h1><p>Pennyfarthing Homes · Quality Management</p></div>
<div class="b">
<p>Hi {sub_name},</p>
<div style="background:{sb};border-left:4px solid {sc};padding:16px;border-radius:0 8px 8px 0;margin:16px 0">
  <h2 style="color:{sc};margin:0 0 6px;font-size:18px">{st}</h2>
  <p style="color:#333;margin:0;font-size:14px">{sm}</p>
</div>
<div class="grid">
  <div class="row"><span class="lbl">Site</span><span class="val">{site_name}</span></div>
  <div class="row"><span class="lbl">Plot</span><span class="val">{plot_number}</span></div>
  <div class="row"><span class="lbl">Stage</span><span class="val">{stage_name}</span></div>
  <div class="row"><span class="lbl">Reviewed by</span><span class="val">{manager_name}</span></div>
</div>
{notes_html}
{flagged_html}
{resubmit_html}
<p style="font-size:14px;color:#555;margin-top:24px">
  {'Please speak to your site manager if you have any questions.' if is_approved else 'Please address all flagged items above and use the Resubmit button to resend.'}
</p>
</div>
<div class="ft">Pennyfarthing Perfect Delivery · Auto-generated</div>
</div></body></html>"""

    pdf_filename = None
    if pdf_bytes and is_approved:
        site_slug = (site.get("name") or "site").replace(" ","_")[:20]
        plot_num  = plot.get("plot_number","")
        stage_num = submission.get("stage_number","")
        pdf_filename = f"PD_Approved_{site_slug}_Plot{plot_num}_Stage{stage_num}.pdf"
    _send(sub_email, subject, html, pdf_bytes if is_approved else None, pdf_filename)


def send_password_reset(to_email: str, user_name: str, reset_url: str):
    """Send password reset email."""
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;color:#1c1c1e">
      <div style="background:#1a1a2e;padding:28px 32px;border-radius:12px 12px 0 0">
        <h2 style="color:#fff;margin:0;font-size:20px">Password Reset</h2>
        <p style="color:#a0a0b0;margin:6px 0 0;font-size:14px">Perfect Delivery Portal</p>
      </div>
      <div style="background:#f9f9fb;padding:28px 32px;border-radius:0 0 12px 12px;border:1px solid #e5e5ea;border-top:none">
        <p style="margin:0 0 16px">Hi {user_name},</p>
        <p style="margin:0 0 24px;color:#636366">A password reset was requested for your account. Click the button below to set a new password. This link expires in <strong>1 hour</strong>.</p>
        <div style="text-align:center;margin:28px 0">
          <a href="{reset_url}" style="background:#1a1a2e;color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;display:inline-block">
            Reset My Password
          </a>
        </div>
        <p style="margin:24px 0 0;font-size:13px;color:#8e8e93">If you didn't request this, you can safely ignore this email. Your password won't change.</p>
        <hr style="border:none;border-top:1px solid #e5e5ea;margin:20px 0">
        <p style="margin:0;font-size:12px;color:#c7c7cc">If the button doesn't work, copy this link:<br>
        <a href="{reset_url}" style="color:#636366;word-break:break-all">{reset_url}</a></p>
      </div>
    </div>
    """
    _send(to_email, "Reset your Perfect Delivery password", html)
