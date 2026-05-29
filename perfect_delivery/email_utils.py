# email_utils.py
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime


SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.resend.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "resend")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")
FROM_NAME = os.environ.get("FROM_NAME", "Pennyfarthing Perfect Delivery")
BASE_URL = os.environ.get("PD_BASE_URL", "http://localhost:5000")


def _send(to_email: str, subject: str, html: str, text: str = ""):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email
    if text:
        msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(FROM_EMAIL, to_email, msg.as_string())


def send_manager_notification(submission: dict, plot: dict, site: dict, review_url: str):
    """Notify site manager of a new PD submission."""
    stage_name = submission["stage_name"]
    plot_number = plot["plot_number"]
    site_name = site["name"]
    sub_name = submission["submitted_by_name"]
    sub_company = submission["submitted_by_company"]
    sub_time = submission["submitted_at"]
    if hasattr(sub_time, "strftime"):
        sub_time_str = sub_time.strftime("%d %b %Y %H:%M")
    else:
        sub_time_str = str(sub_time)[:16].replace("T", " ")

    answers = submission.get("answers", {})
    yes_count = sum(1 for v in answers.values() if isinstance(v, dict) and v.get("value") == "yes")
    no_count = sum(1 for v in answers.values() if isinstance(v, dict) and v.get("value") == "no")
    na_count = sum(1 for v in answers.values() if isinstance(v, dict) and v.get("value") == "n/a")

    subject = f"PD Submission — {site_name} | Plot {plot_number} | {stage_name}"

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 0; }}
  .wrapper {{ max-width: 600px; margin: 0 auto; background: #fff; }}
  .header {{ background: #1a1a2e; padding: 24px 32px; }}
  .header h1 {{ color: #C5962A; margin: 0; font-size: 20px; }}
  .header p {{ color: #aaa; margin: 4px 0 0; font-size: 13px; }}
  .body {{ padding: 32px; }}
  .badge {{ display: inline-block; background: #fff3cd; color: #856404; padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: bold; margin-bottom: 20px; }}
  .info-grid {{ background: #f8f9fa; border-radius: 8px; padding: 16px 20px; margin: 16px 0; }}
  .info-row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #e9ecef; font-size: 14px; }}
  .info-row:last-child {{ border-bottom: none; }}
  .info-label {{ color: #6c757d; }}
  .info-value {{ font-weight: bold; color: #212529; }}
  .stats {{ display: flex; gap: 12px; margin: 20px 0; }}
  .stat {{ flex: 1; text-align: center; padding: 12px; border-radius: 8px; }}
  .stat.yes {{ background: #d4edda; }}
  .stat.no {{ background: #f8d7da; }}
  .stat.na {{ background: #e2e3e5; }}
  .stat .num {{ font-size: 28px; font-weight: bold; }}
  .stat .lbl {{ font-size: 12px; color: #555; }}
  .cta {{ text-align: center; margin: 28px 0; }}
  .btn {{ display: inline-block; background: #C5962A; color: #fff; text-decoration: none; padding: 14px 32px; border-radius: 8px; font-size: 16px; font-weight: bold; }}
  .footer {{ background: #f8f9fa; padding: 16px 32px; text-align: center; font-size: 12px; color: #999; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>Perfect Delivery — New Submission</h1>
    <p>Pennyfarthing Homes · Quality Management</p>
  </div>
  <div class="body">
    <span class="badge">⏳ Awaiting Your Review</span>
    <p>A subcontractor has submitted a Perfect Delivery checklist for your review.</p>
    <div class="info-grid">
      <div class="info-row"><span class="info-label">Site</span><span class="info-value">{site_name}</span></div>
      <div class="info-row"><span class="info-label">Plot</span><span class="info-value">{plot_number}</span></div>
      <div class="info-row"><span class="info-label">Stage</span><span class="info-value">{stage_name}</span></div>
      <div class="info-row"><span class="info-label">Submitted by</span><span class="info-value">{sub_name} — {sub_company}</span></div>
      <div class="info-row"><span class="info-label">Submitted at</span><span class="info-value">{sub_time_str}</span></div>
    </div>
    <div class="stats">
      <div class="stat yes"><div class="num" style="color:#155724">{yes_count}</div><div class="lbl">Yes</div></div>
      <div class="stat no"><div class="num" style="color:#721c24">{no_count}</div><div class="lbl">No</div></div>
      <div class="stat na"><div class="num" style="color:#383d41">{na_count}</div><div class="lbl">N/A</div></div>
    </div>
    <p style="font-size:14px;color:#555">Review all answers and uploaded photos, then approve or reject with notes.</p>
    <div class="cta">
      <a href="{review_url}" class="btn">Review &amp; Decide →</a>
    </div>
    <p style="font-size:12px;color:#999;text-align:center">Or copy this link: {review_url}</p>
  </div>
  <div class="footer">Pennyfarthing Perfect Delivery System · This email was generated automatically</div>
</div>
</body>
</html>
"""
    _send(site["manager_email"], subject, html)


def send_decision_to_subcontractor(submission: dict, plot: dict, site: dict, decision: str, manager_notes: str):
    """Notify subcontractor of approval or rejection."""
    is_approved = decision == "approved"
    stage_name = submission["stage_name"]
    plot_number = plot["plot_number"]
    site_name = site["name"]
    sub_name = submission["submitted_by_name"]
    sub_email = submission["submitted_by_email"]
    manager_name = site["manager_name"]

    status_color = "#28a745" if is_approved else "#dc3545"
    status_bg = "#d4edda" if is_approved else "#f8d7da"
    status_text = "APPROVED ✓" if is_approved else "REJECTED ✗"
    status_msg = (
        "Your checklist has been reviewed and approved. Payment can now be progressed for this stage."
        if is_approved
        else "Your checklist has been reviewed and rejected. Please address the points below and resubmit."
    )

    subject = f"PD Stage {('Approved' if is_approved else 'Rejected')} — {site_name} | Plot {plot_number} | {stage_name}"

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 0; }}
  .wrapper {{ max-width: 600px; margin: 0 auto; background: #fff; }}
  .header {{ background: #1a1a2e; padding: 24px 32px; }}
  .header h1 {{ color: #C5962A; margin: 0; font-size: 20px; }}
  .header p {{ color: #aaa; margin: 4px 0 0; font-size: 13px; }}
  .body {{ padding: 32px; }}
  .status {{ background: {status_bg}; border-left: 4px solid {status_color}; padding: 16px 20px; border-radius: 0 8px 8px 0; margin: 16px 0; }}
  .status h2 {{ color: {status_color}; margin: 0 0 6px; font-size: 18px; }}
  .status p {{ color: #333; margin: 0; font-size: 14px; }}
  .info-grid {{ background: #f8f9fa; border-radius: 8px; padding: 16px 20px; margin: 16px 0; }}
  .info-row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #e9ecef; font-size: 14px; }}
  .info-row:last-child {{ border-bottom: none; }}
  .info-label {{ color: #6c757d; }}
  .info-value {{ font-weight: bold; color: #212529; }}
  .notes {{ background: #fff3cd; border-left: 4px solid #ffc107; padding: 16px 20px; border-radius: 0 8px 8px 0; margin: 16px 0; }}
  .notes h3 {{ margin: 0 0 8px; font-size: 15px; color: #856404; }}
  .notes p {{ margin: 0; font-size: 14px; color: #333; white-space: pre-wrap; }}
  .footer {{ background: #f8f9fa; padding: 16px 32px; text-align: center; font-size: 12px; color: #999; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>Perfect Delivery — Stage Decision</h1>
    <p>Pennyfarthing Homes · Quality Management</p>
  </div>
  <div class="body">
    <p>Hi {sub_name},</p>
    <div class="status">
      <h2>{status_text}</h2>
      <p>{status_msg}</p>
    </div>
    <div class="info-grid">
      <div class="info-row"><span class="info-label">Site</span><span class="info-value">{site_name}</span></div>
      <div class="info-row"><span class="info-label">Plot</span><span class="info-value">{plot_number}</span></div>
      <div class="info-row"><span class="info-label">Stage</span><span class="info-value">{stage_name}</span></div>
      <div class="info-row"><span class="info-label">Reviewed by</span><span class="info-value">{manager_name}</span></div>
    </div>
    {'<div class="notes"><h3>Notes from Site Manager</h3><p>' + manager_notes + '</p></div>' if manager_notes else ''}
    <p style="font-size:14px;color:#555;margin-top:24px">
      {'If you have any questions, please speak to your site manager directly.' if is_approved
       else 'Please address the points above and resubmit by scanning the plot QR code again.'}
    </p>
  </div>
  <div class="footer">Pennyfarthing Perfect Delivery System · This email was generated automatically</div>
</div>
</body>
</html>
"""
    _send(sub_email, subject, html)
