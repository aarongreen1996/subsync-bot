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


def send_welcome_email(user: dict, tenant: dict, password: str, sites: list):
    """Send welcome email to a new portal user (site manager or tenant admin)."""
    name         = user.get("name", "")
    email        = user.get("email", "")
    role         = user.get("role", "site_manager")
    company      = tenant.get("company_name", "Perfect Delivery")
    portal_url   = f"{BASE_URL}/pd/portal"
    is_admin     = role == "tenant_admin"

    role_label   = "Tenant Admin" if is_admin else "Site Manager"
    role_colour  = "#1a1a2e" if is_admin else "#C5962A"

    # Build site list HTML
    site_rows = ""
    for site in sites:
        site_name = site.get("name", "")
        site_rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;font-size:14px;color:#1a1a2e">{site_name}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#6b7280">Active</td>
        </tr>"""

    site_section = ""
    if sites:
        site_section = f"""
        <div style="margin:24px 0">
          <p style="font-size:14px;font-weight:700;color:#1a1a2e;margin-bottom:8px">Your assigned sites:</p>
          <table style="width:100%;border-collapse:collapse;background:#f9f9fb;border-radius:8px;overflow:hidden">
            <tr style="background:#f2f2f7">
              <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase">Site</th>
              <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280;font-weight:600;text-transform:uppercase">Status</th>
            </tr>
            {site_rows}
          </table>
        </div>"""

    admin_section = ""
    if is_admin:
        admin_section = f"""
        <div style="background:#fff8e7;border:1px solid #f59e0b;border-radius:10px;padding:16px 20px;margin:20px 0">
          <p style="font-size:13px;font-weight:700;color:#92400e;margin:0 0 6px">Admin Access</p>
          <p style="font-size:13px;color:#78350f;margin:0">As a Tenant Admin you can manage sites, plots, users and checklist settings from your dashboard.</p>
        </div>"""

    guide_rows = [
        ("📱", "Scan QR codes", "Subcontractors scan plot QR codes to submit stage checklists"),
        ("✅", "Review & approve", "You receive an email for each submission — review photos and approve or reject"),
        ("📊", "Dashboard", "View all sites, plots and submission history from your portal"),
        ("📷", "Photo library", "All photos are stored and searchable by site, plot and stage"),
    ]
    guide_html = ""
    for icon, title, desc in guide_rows:
        guide_html += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:20px;width:40px">{icon}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0">
            <div style="font-size:13px;font-weight:700;color:#1a1a2e">{title}</div>
            <div style="font-size:12px;color:#6b7280;margin-top:2px">{desc}</div>
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#f5f5f5;margin:0;padding:0}}
.w{{max-width:600px;margin:0 auto;background:#fff}}
.h{{background:#1a1a2e;padding:28px 32px}}
.h h1{{color:#C5962A;margin:0;font-size:22px;font-weight:800}}
.h p{{color:#9ca3af;margin:4px 0 0;font-size:13px}}
.b{{padding:32px}}
.badge{{display:inline-block;background:{role_colour};color:#fff;padding:4px 14px;border-radius:20px;font-size:12px;font-weight:700;margin-bottom:20px}}
.creds{{background:#f9f9fb;border:1.5px solid #e5e5ea;border-radius:10px;padding:16px 20px;margin:20px 0}}
.cred-row{{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #e5e5ea;font-size:14px}}
.cred-row:last-child{{border-bottom:none}}
.cred-lbl{{color:#6b7280;font-weight:500}} .cred-val{{font-weight:700;color:#1a1a2e;font-family:monospace}}
.btn{{display:inline-block;background:#C5962A;color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-size:16px;font-weight:700;margin:8px 4px}}
.btn-ghost{{display:inline-block;background:#f2f2f7;color:#1a1a2e;text-decoration:none;padding:14px 32px;border-radius:10px;font-size:14px;font-weight:600;margin:8px 4px}}
.ft{{background:#f8f9fa;padding:20px 32px;text-align:center;font-size:12px;color:#9ca3af;border-top:1px solid #f0f0f0}}
</style></head>
<body><div class="w">
<div class="h">
  <h1>Perfect Delivery</h1>
  <p>{company} · Quality Management Portal</p>
</div>
<div class="b">
  <span class="badge">{role_label}</span>
  <h2 style="font-size:20px;color:#1a1a2e;margin:0 0 8px">Welcome, {name}! 👋</h2>
  <p style="color:#6b7280;font-size:14px;line-height:1.6;margin:0 0 24px">
    Your Perfect Delivery account has been set up. Use the details below to log in and start reviewing stage submissions from your sites.
  </p>

  <div class="creds">
    <p style="font-size:12px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin:0 0 10px">Your login details</p>
    <div class="cred-row">
      <span class="cred-lbl">Portal URL</span>
      <span class="cred-val" style="font-family:inherit;font-size:13px"><a href="{portal_url}" style="color:#C5962A">{portal_url.replace("https://","")}</a></span>
    </div>
    <div class="cred-row">
      <span class="cred-lbl">Email</span>
      <span class="cred-val">{email}</span>
    </div>
    <div class="cred-row">
      <span class="cred-lbl">Password</span>
      <span class="cred-val">{password}</span>
    </div>
  </div>

  <div style="text-align:center;margin:24px 0">
    <a href="{portal_url}" class="btn">Open Dashboard →</a>
  </div>

  {site_section}
  {admin_section}

  <div style="margin:24px 0">
    <p style="font-size:14px;font-weight:700;color:#1a1a2e;margin-bottom:8px">How Perfect Delivery works:</p>
    <table style="width:100%;border-collapse:collapse">
      {guide_html}
    </table>
  </div>

  <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:10px;padding:16px 20px;margin:20px 0">
    <p style="font-size:13px;font-weight:700;color:#166534;margin:0 0 6px">💡 Quick tip</p>
    <p style="font-size:13px;color:#166534;margin:0">
      When a subcontractor submits a stage checklist you'll receive an email with a review link. 
      You can approve or reject directly from that email — no need to log in every time.
    </p>
  </div>

  <p style="font-size:13px;color:#9ca3af;margin-top:24px">
    Need help? Reply to this email or contact your system administrator.
    You can change your password at any time from your dashboard settings.
  </p>
</div>
<div class="ft">
  Perfect Delivery · {company}<br>
  <a href="{portal_url}" style="color:#C5962A;text-decoration:none">Access your dashboard</a>
</div>
</div></body></html>"""

    subject = f"Welcome to Perfect Delivery — {company}"
    _send(email, subject, html)


def send_site_manager_assignment_email(user: dict, tenant: dict, new_sites: list):
    """Email sent when a site manager is assigned to new sites."""
    name       = user.get("name", "")
    email      = user.get("email", "")
    company    = tenant.get("company_name", "Perfect Delivery")
    portal_url = f"{BASE_URL}/pd/portal"

    site_list = "".join(
        f"<li style='padding:4px 0;font-size:14px;color:#1a1a2e'>📍 {s.get('name','')}</li>"
        for s in new_sites
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Arial,sans-serif;background:#f5f5f5;margin:0;padding:0">
<div style="max-width:600px;margin:0 auto;background:#fff">
  <div style="background:#1a1a2e;padding:24px 32px">
    <h1 style="color:#C5962A;margin:0;font-size:20px">Perfect Delivery</h1>
    <p style="color:#9ca3af;margin:4px 0 0;font-size:13px">{company}</p>
  </div>
  <div style="padding:32px">
    <h2 style="font-size:18px;color:#1a1a2e;margin:0 0 12px">New site assignment, {name}</h2>
    <p style="font-size:14px;color:#6b7280;line-height:1.6">You've been assigned to the following site(s) on Perfect Delivery:</p>
    <ul style="background:#f9f9fb;border-radius:10px;padding:16px 16px 16px 32px;margin:16px 0">
      {site_list}
    </ul>
    <p style="font-size:14px;color:#6b7280;line-height:1.6">
      You'll receive an email notification each time a subcontractor submits a stage checklist for these sites.
      Log in to your dashboard to view plots, submissions and photos.
    </p>
    <div style="text-align:center;margin:24px 0">
      <a href="{portal_url}" style="display:inline-block;background:#C5962A;color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-size:15px;font-weight:700">Open Dashboard →</a>
    </div>
  </div>
  <div style="background:#f8f9fa;padding:16px 32px;text-align:center;font-size:12px;color:#9ca3af">
    Perfect Delivery · {company}
  </div>
</div>
</body></html>"""

    subject = f"New site assigned — {', '.join(s.get('name','') for s in new_sites)}"
    _send(email, subject, html)
