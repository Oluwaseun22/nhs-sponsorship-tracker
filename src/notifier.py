"""
notifier.py — Email Notification Module
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
import config

log = logging.getLogger(__name__)

def _build_html_body(jobs):
    rows = ""
    for job in jobs:
        kw_badges = " ".join(
            f'<span style="background:#005eb8;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;margin-right:4px;">{kw}</span>'
            for kw in job.get("sponsorship_keywords_found", [])
        )
        rows += f"""
        <div style="border:1px solid #d8dde0;border-radius:6px;padding:16px;margin-bottom:16px;">
            <h3 style="color:#005eb8;margin:0 0 6px 0;">{job['title']}</h3>
            <p style="margin:0 0 4px 0;"><strong>Organisation:</strong> {job['employer']}</p>
            <p style="margin:0 0 4px 0;"><strong>Location:</strong> {job['location']}</p>
            <p style="margin:0 0 4px 0;"><strong>Salary:</strong> {job['salary']}</p>
            <p style="margin:0 0 8px 0;"><strong>Closing:</strong> {job['closing_date']}</p>
            <p style="margin:0 0 8px 0;">{kw_badges}</p>
            <p style="color:#555;font-size:14px;">{job['summary'][:300]}...</p>
            <a href="{job['url']}" style="color:#005eb8;font-weight:bold;">View Job on NHS Jobs →</a>
        </div>"""
    return f"""<html><body style="max-width:680px;margin:auto;padding:24px;">
        <div style="background:#005eb8;padding:16px 24px;border-radius:6px;margin-bottom:24px;">
            <h2 style="color:#fff;margin:0;">NHS Sponsorship Job Alert</h2>
            <p style="color:#cde;margin:4px 0 0 0;">{len(jobs)} new visa-sponsoring role(s) found</p>
        </div>
        {rows}
        <p style="font-size:12px;color:#999;margin-top:24px;">Sent by your NHS Job Sponsorship Tracker</p>
    </body></html>"""

def send_via_ses(jobs, cv_path=None):
    import boto3
    from botocore.exceptions import ClientError
    subject = f"{len(jobs)} NHS Sponsored Role(s) Found — Job Alert"
    html_body = _build_html_body(jobs)
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = config.SES_SENDER
    msg["To"] = config.SES_RECIPIENT
    body_part = MIMEMultipart("alternative")
    body_part.attach(MIMEText(html_body, "html"))
    msg.attach(body_part)
    if cv_path:
        cv_file = Path(cv_path)
        if cv_file.exists():
            with open(cv_path, "rb") as f:
                att = MIMEBase("application", "octet-stream")
                att.set_payload(f.read())
            encoders.encode_base64(att)
            att.add_header("Content-Disposition", f'attachment; filename="{cv_file.name}"')
            msg.attach(att)
    client = boto3.client("ses", region_name=config.SES_REGION)
    try:
        client.send_raw_email(
            Source=config.SES_SENDER,
            Destinations=[config.SES_RECIPIENT],
            RawMessage={"Data": msg.as_string()},
        )
        log.info(f"SES email sent to {config.SES_RECIPIENT}")
    except ClientError as e:
        log.error(f"SES send failed: {e.response['Error']['Message']}")

def send_via_smtp(jobs, cv_path=None):
    subject = f"{len(jobs)} NHS Sponsored Role(s) Found — Job Alert"
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER
    msg["To"] = config.SMTP_RECIPIENT
    msg.attach(MIMEText(_build_html_body(jobs), "html"))
    if cv_path:
        cv_file = Path(cv_path)
        if cv_file.exists():
            with open(cv_path, "rb") as f:
                att = MIMEBase("application", "octet-stream")
                att.set_payload(f.read())
            encoders.encode_base64(att)
            att.add_header("Content-Disposition", f'attachment; filename="{cv_file.name}"')
            msg.attach(att)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.ehlo(); server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.sendmail(config.SMTP_USER, config.SMTP_RECIPIENT, msg.as_string())
    log.info(f"SMTP email sent to {config.SMTP_RECIPIENT}")

def notify(jobs, cv_path=None):
    if not jobs:
        log.info("No jobs to notify about.")
        return
    try:
        if config.EMAIL_PROVIDER == "ses":
            send_via_ses(jobs, cv_path)
        else:
            send_via_smtp(jobs, cv_path)
    except Exception as e:
        log.error(f"Notification failed: {e}")
        raise
