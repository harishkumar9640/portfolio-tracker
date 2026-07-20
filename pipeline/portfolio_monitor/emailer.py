"""
portfolio_monitor.emailer
------------------------
Lightweight SMTP helper for the portfolio-monitor scripts.

Reuses the same env-var conventions as pipeline.mf_holdings_alert:
  PM_ALERT_SMTP_HOST   e.g. smtp.gmail.com
  PM_ALERT_SMTP_PORT   587
  PM_ALERT_SMTP_USER   full email address
  PM_ALERT_SMTP_PASS   app password
  PM_ALERT_FROM        "From" (defaults to SMTP_USER)
  PM_ALERT_TO          recipient (defaults to SMTP_USER)
  PM_ALERT_DRY_RUN=1   log body instead of sending (default)

Falls back to MF_ALERT_* env vars if PM_ALERT_* not set, so users who
already configured MF alert SMTP don't need to set up twice.
"""
from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str | None = None) -> str | None:
    """Read env var; fall back to .env (via python-dotenv) then secrets.local.json."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        from dotenv import load_dotenv
        env_path = PROJECT / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            val = os.environ.get(name)
            if val:
                return val
    except ImportError:
        pass
    sp = PROJECT / "secrets.local.json"
    if sp.exists():
        try:
            data = json.loads(sp.read_text())
            val = data.get(name) or data.get(name.lower())
            if val:
                return val
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _resolve(name: str) -> str | None:
    """Try PM_ALERT_* first, then MF_ALERT_* as fallback."""
    return _env(name) or _env(name.replace("PM_ALERT_", "MF_ALERT_"))


def is_dry_run() -> bool:
    if os.environ.get("PM_ALERT_DRY_RUN", "").lower() in ("1", "true", "yes"):
        return True
    if not _resolve("PM_ALERT_SMTP_HOST"):
        return True
    if not _resolve("PM_ALERT_SMTP_USER") or not _resolve("PM_ALERT_SMTP_PASS"):
        return True
    if not _resolve("PM_ALERT_TO"):
        return True
    return False


def send_email(subject: str, plain: str, html: str) -> dict:
    """
    Send an email via SMTP. If creds are missing or PM_ALERT_DRY_RUN=1,
    log the body and return dry_run status.

    Returns: {"sent": bool, "mode": str, "to": str, "subject": str, ...}
    """
    host = _resolve("PM_ALERT_SMTP_HOST")
    port = int(_resolve("PM_ALERT_SMTP_PORT") or "587")
    user = _resolve("PM_ALERT_SMTP_USER")
    pw = _resolve("PM_ALERT_SMTP_PASS")
    sender = _resolve("PM_ALERT_FROM") or user or "noreply@portfolio.local"
    recipient = _resolve("PM_ALERT_TO") or user
    dry = is_dry_run()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    if dry:
        from pipeline.logging_setup import get_logger
        log = get_logger("portfolio_monitor")
        log.info("[dry-run] would send email to=%s subject=%r", recipient, subject)
        for line in plain.splitlines()[:40]:
            log.info("  %s", line)
        return {
            "sent": False, "mode": "dry_run",
            "reason": "missing SMTP creds or PM_ALERT_DRY_RUN=1",
            "to": recipient, "subject": subject,
        }

    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        from pipeline.logging_setup import get_logger
        log = get_logger("portfolio_monitor")
        log.info("email sent to=%s subject=%r", recipient, subject)
        return {"sent": True, "mode": "smtp", "to": recipient, "subject": subject}
    except Exception as e:
        from pipeline.logging_setup import get_logger
        log = get_logger("portfolio_monitor")
        log.exception("SMTP send failed: %s", e)
        return {"sent": False, "mode": "error", "error": str(e),
                "to": recipient, "subject": subject}
