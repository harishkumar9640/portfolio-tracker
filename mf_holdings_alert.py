"""
mf_holdings_alert.py
--------------------
Daily MF-holdings change detector + email notifier.

Runs once per day (after market close ~4 PM IST) and emails the user
whenever the MF holdings data for any of the 8 portfolio tickers has
changed since the last check.

Two entry points:
  1. CLI:  python3 mf_holdings_alert.py
            Fetches today's snapshot, compares with the persisted
            "previous" snapshot (data/mf_holdings_prev.json), and
            emails a digest if anything changed.

  2. Daemon: register a thread via ``start_daily_scheduler()`` to
            run automatically once a day inside the FastAPI app.

Environment variables (or secrets.local.json — gitignored):
  MF_ALERT_SMTP_HOST   e.g. smtp.gmail.com
  MF_ALERT_SMTP_PORT   e.g. 587 (default)
  MF_ALERT_SMTP_USER   full email address (auth user)
  MF_ALERT_SMTP_PASS   app password (NOT your real Gmail password;
                       use https://myaccount.google.com/apppasswords)
  MF_ALERT_FROM        "From" address (defaults to SMTP_USER)
  MF_ALERT_TO          recipient (defaults to SMTP_USER)
  MF_ALERT_DRY_RUN     "1" / "true" -> print email instead of sending
                       (useful for tests; also enables when SMTP creds
                       are missing)

If SMTP credentials are missing, the module runs in dry-run mode
automatically and just logs the email body to the application log.

Storage:
  - data/mf_holdings_prev.json  — last snapshot we compared against
  - data/mf_holdings_alert_log.json — last N runs (success/failure/timestamp)
"""
from __future__ import annotations

import json
import os
import smtplib
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from logging_setup import get_logger
import mf_holdings

log = get_logger("mf_holdings_alert")

PROJECT = Path(__file__).resolve().parent
DATA_DIR = PROJECT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Load .env as early as possible so the CLI picks up SMTP creds without
# requiring the user to `export VAR=...` first. python-dotenv is a
# dependency of the project (used by angel_client, mf_sgb, etc.) so we
# can rely on it being installed.
try:
    from dotenv import load_dotenv
    _env_file = PROJECT / ".env"
    if _env_file.exists():
        load_dotenv(_env_file, override=False)
except ImportError:
    pass

PREV_SNAPSHOT_FILE = DATA_DIR / "mf_holdings_prev.json"
ALERT_LOG_FILE = DATA_DIR / "mf_holdings_alert_log.json"

# IST = UTC+5:30 (no DST in India)
IST = timezone(timedelta(hours=5, minutes=30))


# ---------- Diff detection ----------

@dataclass
class MfHoldingChange:
    """A single field-level change between two snapshots for one ticker."""
    ticker: str
    name: str
    field: str         # e.g. "mfs_bought", "net_change_shares", "top_buyer.name"
    old: object
    new: object
    delta: Optional[float] = None  # numeric delta when both sides are numeric


def diff_snapshots(
    prev: dict[str, dict],
    curr: dict[str, dict],
) -> list[MfHoldingChange]:
    """
    Compare two ``{ticker: parsed_dict}`` snapshots field by field and
    return a list of changes. Empty list means "nothing changed".

    Fields we watch:
      - mfs_bought / mfs_sold (counts of MFs buying/selling)
      - net_change_shares (signed net change for the month)
      - total_mfs_holding (how many MFs hold this stock now)
      - top_buyer.name / top_buyer.shares (the leading buyer)
      - top_seller.name / top_seller.shares (the leading seller)
      - asof (month label — if this changed, it's a new month)
    """
    changes: list[MfHoldingChange] = []
    watched = (
        "mfs_bought", "mfs_sold", "net_change_shares",
        "total_mfs_holding", "asof",
    )
    for tkr in set(prev) | set(curr):
        p = prev.get(tkr) or {}
        c = curr.get(tkr) or {}
        name = c.get("name") or p.get("name") or tkr

        for f in watched:
            pv, cv = p.get(f), c.get(f)
            if pv != cv:
                delta = None
                if isinstance(pv, (int, float)) and isinstance(cv, (int, float)):
                    delta = cv - pv
                changes.append(MfHoldingChange(
                    ticker=tkr, name=name, field=f,
                    old=pv, new=cv, delta=delta,
                ))

        # Top buyer / seller name + shares
        for role in ("top_buyer", "top_seller"):
            pn = (p.get(role) or {}).get("name")
            cn = (c.get(role) or {}).get("name")
            if pn != cn:
                changes.append(MfHoldingChange(
                    ticker=tkr, name=name, field=f"{role}.name",
                    old=pn, new=cn,
                ))
            ps = (p.get(role) or {}).get("shares")
            cs = (c.get(role) or {}).get("shares")
            if ps != cs:
                delta = None
                if isinstance(ps, (int, float)) and isinstance(cs, (int, float)):
                    delta = cs - ps
                changes.append(MfHoldingChange(
                    ticker=tkr, name=name, field=f"{role}.shares",
                    old=ps, new=cs, delta=delta,
                ))
    return changes


# ---------- Email rendering ----------

def _fmt_int(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _signed(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return ("+" if v >= 0 else "") + f"{v:,}"
    return str(v)


def render_email(
    changes: list[MfHoldingChange],
    curr_snapshot: dict[str, dict],
    prev_snapshot: dict[str, dict] | None = None,
) -> tuple[str, str, str]:
    """
    Build (subject, plain_text, html_body) for the alert email.

    Subject examples:
      - "[MF Alert] 3 stocks changed: RELIANCE, ITC, NTPCGREEN"
      - "[MF Alert] No changes (May 2026 data)"
    """
    today = datetime.now(IST).strftime("%d %b %Y")
    asof = next(
        (d.get("asof") for d in curr_snapshot.values() if d.get("asof")),
        "latest month",
    )

    # Group changes by ticker
    by_ticker: dict[str, list[MfHoldingChange]] = {}
    for ch in changes:
        by_ticker.setdefault(ch.ticker, []).append(ch)

    n = len(by_ticker)
    if n == 0:
        subject = f"[MF Alert] No changes ({asof})"
        intro = f"No mutual-fund activity changes detected for {today} ({asof})."
    else:
        names = sorted({
            (curr_snapshot.get(t) or prev_snapshot or {}).get("name", t)
            for t in by_ticker
        })
        subject = f"[MF Alert] {n} stock{'s' if n != 1 else ''} changed: {', '.join(names[:5])}"
        if len(names) > 5:
            subject += f" (+{len(names) - 5} more)"
        intro = (
            f"{n} of your 8 stocks show new mutual-fund activity "
            f"for {asof}:"
        )

    # ----- Plain text body -----
    lines = [f"Mutual Fund Holdings Alert — {today}", "", intro, ""]
    if n == 0:
        lines.append("All clear. No changes detected since last check.")
        lines.append("")
        lines.append("Per-stock snapshot:")
        for tkr in sorted(curr_snapshot):
            d = curr_snapshot[tkr]
            lines.append(
                f"  {tkr:12s}  holders={_fmt_int(d.get('total_mfs_holding'))}  "
                f"bought={_fmt_int(d.get('mfs_bought'))}  sold={_fmt_int(d.get('mfs_sold'))}  "
                f"net={_signed(d.get('net_change_shares'))}"
            )
    else:
        for tkr in sorted(by_ticker):
            chs = by_ticker[tkr]
            d = curr_snapshot.get(tkr, {}) or prev_snapshot.get(tkr, {}) if prev_snapshot else {}
            d = curr_snapshot.get(tkr, {})
            lines.append(f"--- {tkr} ({d.get('name', '')}) ---")
            lines.append(
                f"  holders:   {_fmt_int(d.get('total_mfs_holding'))}    "
                f"bought/sold: {_fmt_int(d.get('mfs_bought'))} / {_fmt_int(d.get('mfs_sold'))}    "
                f"net change: {_signed(d.get('net_change_shares'))}"
            )
            for ch in chs:
                lines.append(
                    f"    {ch.field}: {_fmt_int(ch.old)} -> {_fmt_int(ch.new)}"
                    + (f"   (delta {_signed(ch.delta)})" if ch.delta is not None else "")
                )
            lines.append("")

    lines.append("")
    lines.append("— Portfolio Tracker")

    # ----- HTML body -----
    rows_html = []
    if n == 0:
        # "all clear" table
        for tkr in sorted(curr_snapshot):
            d = curr_snapshot[tkr]
            nc = d.get("net_change_shares")
            color = "#16a34a" if nc and nc >= 0 else "#dc2626"
            rows_html.append(f"""
              <tr>
                <td style="padding:6px 12px;font-weight:600;">{tkr}</td>
                <td style="padding:6px 12px;">{d.get("name", "")}</td>
                <td style="padding:6px 12px;text-align:right;">{_fmt_int(d.get("total_mfs_holding"))}</td>
                <td style="padding:6px 12px;text-align:right;color:#16a34a;">+{_fmt_int(d.get("mfs_bought"))}</td>
                <td style="padding:6px 12px;text-align:right;color:#dc2626;">-{_fmt_int(d.get("mfs_sold"))}</td>
                <td style="padding:6px 12px;text-align:right;color:{color};font-weight:600;">{_signed(nc)}</td>
              </tr>
            """)
        body_table = f"""
          <table style="border-collapse:collapse;font-family:sans-serif;font-size:14px;width:100%;">
            <thead>
              <tr style="background:#f3f4f6;text-align:left;">
                <th style="padding:8px 12px;">Stock</th>
                <th style="padding:8px 12px;">Name</th>
                <th style="padding:8px 12px;text-align:right;">Holders</th>
                <th style="padding:8px 12px;text-align:right;">Buying</th>
                <th style="padding:8px 12px;text-align:right;">Selling</th>
                <th style="padding:8px 12px;text-align:right;">Net change</th>
              </tr>
            </thead>
            <tbody>{''.join(rows_html)}</tbody>
          </table>
        """
    else:
        # Per-stock change cards
        cards = []
        for tkr in sorted(by_ticker):
            chs = by_ticker[tkr]
            d = curr_snapshot.get(tkr, {})
            nc = d.get("net_change_shares")
            color = "#16a34a" if nc and nc >= 0 else "#dc2626"
            change_rows = []
            for ch in chs:
                delta_str = (
                    f" (<span style='color:#9ca3af;'>{_signed(ch.delta)}</span>)"
                    if ch.delta is not None else ""
                )
                change_rows.append(f"""
                  <tr>
                    <td style="padding:4px 8px;color:#6b7280;">{ch.field}</td>
                    <td style="padding:4px 8px;">{_fmt_int(ch.old)}</td>
                    <td style="padding:4px 8px;color:#9ca3af;">→</td>
                    <td style="padding:4px 8px;font-weight:600;">{_fmt_int(ch.new)}{delta_str}</td>
                  </tr>
                """)
            cards.append(f"""
              <div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;margin-bottom:12px;background:#ffffff;">
                <div style="font-size:16px;font-weight:600;color:#111827;">{tkr} <span style="font-weight:400;color:#6b7280;">— {d.get("name", "")}</span></div>
                <div style="margin:4px 0 8px;color:#6b7280;font-size:13px;">
                  Holders: <strong>{_fmt_int(d.get("total_mfs_holding"))}</strong> ·
                  Buying: <strong style="color:#16a34a;">+{_fmt_int(d.get("mfs_bought"))}</strong> ·
                  Selling: <strong style="color:#dc2626;">-{_fmt_int(d.get("mfs_sold"))}</strong> ·
                  Net: <strong style="color:{color};">{_signed(nc)}</strong>
                </div>
                <table style="border-collapse:collapse;font-family:monospace;font-size:13px;width:100%;">
                  <tbody>{''.join(change_rows)}</tbody>
                </table>
              </div>
            """)
        body_table = "\n".join(cards)

    html = f"""
    <html>
    <body style="background:#f9fafb;padding:24px;font-family:-apple-system,Segoe UI,sans-serif;color:#111827;">
      <div style="max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;padding:24px;border:1px solid #e5e7eb;">
        <h2 style="margin-top:0;color:#1f2937;">📈 Mutual Fund Holdings Alert</h2>
        <p style="color:#6b7280;margin:0 0 16px;">{today} · data as of <strong>{asof}</strong></p>
        <p style="font-size:15px;">{intro}</p>
        {body_table}
        <p style="color:#9ca3af;font-size:12px;margin-top:24px;border-top:1px solid #e5e7eb;padding-top:12px;">
          Sent by Portfolio Tracker · mf_holdings_alert.py
        </p>
      </div>
    </body>
    </html>
    """
    return subject, "\n".join(lines), html


# ---------- SMTP ----------

def _env(name: str, default: str | None = None) -> str | None:
    """Read env var with .env + secrets.local.json fallback."""
    val = os.environ.get(name)
    if val:
        return val
    # Try .env via python-dotenv (if installed)
    try:
        from dotenv import load_dotenv
        # Load .env from project root (idempotent)
        _ENV_PATH = PROJECT / ".env"
        if _ENV_PATH.exists():
            load_dotenv(_ENV_PATH, override=False)
            val = os.environ.get(name)
            if val:
                return val
    except ImportError:
        pass
    # Optional fallback to secrets.local.json (gitignored)
    secrets_path = PROJECT / "secrets.local.json"
    if secrets_path.exists():
        try:
            data = json.loads(secrets_path.read_text())
            val = data.get(name) or data.get(name.lower())
            if val:
                return val
        except (json.JSONDecodeError, OSError):
            pass
    return default


def is_dry_run() -> bool:
    """If true, we should print the email instead of sending it via SMTP."""
    if os.environ.get("MF_ALERT_DRY_RUN", "").lower() in ("1", "true", "yes"):
        return True
    if not _env("MF_ALERT_SMTP_HOST"):
        return True
    if not _env("MF_ALERT_SMTP_USER") or not _env("MF_ALERT_SMTP_PASS"):
        return True
    if not _env("MF_ALERT_TO"):
        return True
    return False


def send_email(subject: str, plain: str, html: str) -> dict:
    """
    Send an email. Returns a status dict: {"sent": bool, "mode": str,
    "reason": str, "to": str, "subject": str}.

    If SMTP credentials are missing or MF_ALERT_DRY_RUN=1, we log the
    email body and return ``{"sent": False, "mode": "dry_run", ...}``.
    """
    host = _env("MF_ALERT_SMTP_HOST")
    port = int(_env("MF_ALERT_SMTP_PORT") or "587")
    user = _env("MF_ALERT_SMTP_USER")
    pw = _env("MF_ALERT_SMTP_PASS")
    sender = _env("MF_ALERT_FROM") or user or "noreply@portfolio.local"
    recipient = _env("MF_ALERT_TO") or user
    dry = is_dry_run()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    if dry:
        log.info("[dry-run] would send email to=%s subject=%r", recipient, subject)
        # Log a snippet of the plain text for the operator
        for line in plain.splitlines()[:30]:
            log.info("  %s", line)
        return {
            "sent": False, "mode": "dry_run",
            "reason": "missing SMTP creds or MF_ALERT_DRY_RUN=1",
            "to": recipient, "subject": subject,
        }

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
        log.info("email sent to=%s subject=%r", recipient, subject)
        return {"sent": True, "mode": "smtp", "to": recipient, "subject": subject}
    except Exception as e:  # noqa: BLE001
        log.error("email send failed: %s", e)
        return {
            "sent": False, "mode": "smtp", "error": str(e),
            "to": recipient, "subject": subject,
        }


# ---------- Daily run ----------

def _load_prev_snapshot() -> dict[str, dict]:
    if PREV_SNAPSHOT_FILE.exists():
        try:
            return json.loads(PREV_SNAPSHOT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_curr_snapshot(snap: dict[str, dict]) -> None:
    """Persist the current snapshot for next-day diff."""
    tmp = PREV_SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=2, default=str))
    tmp.replace(PREV_SNAPSHOT_FILE)


def _append_alert_log(entry: dict) -> None:
    """Append a run-log entry to data/mf_holdings_alert_log.json (last 30)."""
    log_list: list[dict] = []
    if ALERT_LOG_FILE.exists():
        try:
            log_list = json.loads(ALERT_LOG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log_list = []
    log_list.append(entry)
    log_list = log_list[-30:]
    tmp = ALERT_LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(log_list, indent=2, default=str))
    tmp.replace(ALERT_LOG_FILE)


def run_once(force_email: bool = False) -> dict:
    """
    Run one check: fetch latest snapshot, diff against persisted prev,
    email if anything changed (or always if force_email).

    Returns a status dict for logging / API responses:
        {
            "ran_at":      ISO timestamp (IST),
            "snapshot_ok": bool,
            "stocks_with_changes": int,
            "tickers_changed":      [...],
            "email":     { "sent": bool, "subject": str, ... },
            "errors":    [str, ...]   (if any)
        }
    """
    ran_at = datetime.now(IST).isoformat(timespec="seconds")
    errors: list[str] = []

    try:
        curr = mf_holdings.get_mf_holdings(force=True)
    except Exception as e:
        log.error("snapshot fetch failed: %s", e)
        errors.append(f"snapshot fetch failed: {e}")
        return {
            "ran_at": ran_at, "snapshot_ok": False,
            "stocks_with_changes": 0, "tickers_changed": [],
            "email": {"sent": False, "reason": "snapshot failed"},
            "errors": errors,
        }

    prev = _load_prev_snapshot()
    changes = diff_snapshots(prev, curr)
    tickers_changed = sorted({c.ticker for c in changes})

    subject, plain, html = render_email(
        changes, curr, prev_snapshot=prev,
    )
    if not tickers_changed and not force_email:
        log.info("no changes detected (compared %d tickers)", len(curr))
        # Even with no changes, persist the snapshot for next-day diff
        _save_curr_snapshot(curr)
        result = {
            "ran_at": ran_at, "snapshot_ok": True,
            "stocks_with_changes": 0, "tickers_changed": [],
            "email": {"sent": False, "reason": "no changes"},
            "errors": errors,
        }
        _append_alert_log(result)
        return result

    email_status = send_email(subject, plain, html)
    _save_curr_snapshot(curr)
    result = {
        "ran_at": ran_at, "snapshot_ok": True,
        "stocks_with_changes": len(tickers_changed),
        "tickers_changed": tickers_changed,
        "email": email_status,
        "errors": errors,
    }
    _append_alert_log(result)
    log.info(
        "alert run: %d stocks changed, email sent=%s",
        len(tickers_changed), email_status.get("sent"),
    )
    return result


# ---------- Background scheduler ----------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _next_run_ist(hour: int = 16, minute: int = 30) -> datetime:
    """
    Next 16:30 IST (4:30 PM, just after market close + buffer for
    Trendlyne to update). Convert to a naive UTC datetime for sleep().
    """
    now_ist = datetime.now(IST)
    target = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_ist:
        target = target + timedelta(days=1)
    # Convert to UTC for sleep math (threading doesn't care about tz)
    return target.astimezone(timezone.utc).replace(tzinfo=None)


def _scheduler_loop(stop_event: threading.Event) -> None:
    """Background thread: run once a day at 16:30 IST."""
    while not stop_event.is_set():
        next_run = _next_run_ist()
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        wait_secs = (next_run - now_utc).total_seconds()
        log.info("mf_holdings_alert scheduler: next run at %s IST (in %.0fs)",
                 next_run.strftime("%Y-%m-%d %H:%M:%S"), wait_secs)
        # Sleep in 1-minute chunks so we can respond to stop quickly
        while wait_secs > 0 and not stop_event.is_set():
            chunk = min(60, wait_secs)
            stop_event.wait(chunk)
            wait_secs -= chunk
        if stop_event.is_set():
            break
        try:
            run_once()
        except Exception as e:
            log.exception("scheduled run failed: %s", e)


def start_daily_scheduler() -> threading.Event:
    """
    Start a daemon thread that runs the alert once a day at 16:30 IST.

    Returns a threading.Event; call .set() to stop the scheduler
    (mostly for tests).
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            log.info("mf_holdings_alert scheduler already running")
            return threading.Event()  # placeholder, won't be used
        _scheduler_started = True
        stop_event = threading.Event()
        t = threading.Thread(
            target=_scheduler_loop, args=(stop_event,),
            daemon=True, name="mf-holdings-alert",
        )
        t.start()
        log.info("mf_holdings_alert scheduler started")
        return stop_event


# ---------- CLI ----------

def _cli():
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Print email instead of sending it via SMTP.")
    p.add_argument("--force-email", action="store_true",
                   help="Send the email even if no changes detected.")
    p.add_argument("--start-scheduler", action="store_true",
                   help="Start the background scheduler (runs forever).")
    args = p.parse_args()

    if args.dry_run:
        os.environ["MF_ALERT_DRY_RUN"] = "1"

    if args.start_scheduler:
        stop = start_daily_scheduler()
        log.info("scheduler running. Ctrl-C to stop.")
        try:
            while not stop.wait(60):
                pass
        except KeyboardInterrupt:
            stop.set()
        return

    result = run_once(force_email=args.force_email)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _cli()