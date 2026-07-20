"""
pipeline.portfolio_monitor.calendar
-----------------------------------
100-day review script for the user's mid-cap positions.

Per user spec (2026-07-01):
  - Reviews BALRAMCHIN, KNRCON, UNOMINDA
  - Run date = 100 days from now (~2026-10-10)
  - Sends a 1-page "review needed" email to PM_ALERT_TO

Threshold rules (suggested in conversation):
  - TRIM if position is down > 15% from current price (compare to last known
    reference price stored in this script, updated each run)
  - TRIM if fundamentals visibly deteriorated in any quarterly result
    (caller can set the "deteriorated" flag in the email body manually)
  - HOLD otherwise

Schedule via scheduler.py or cron:
  0 9 * * * /usr/bin/python3 -m pipeline.portfolio_monitor.calendar
  (or run once on 2026-10-10 specifically)

CLI:
  python -m pipeline.portfolio_monitor.calendar
      → check thresholds, send email if any position is flagged
  python -m pipeline.portfolio_monitor.calendar --force
      → send email regardless of whether any position is flagged
  python -m pipeline.portfolio_monitor.calendar --init
      → store current prices as the "100-day reference" baseline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pipeline.logging_setup import get_logger
from pipeline.portfolio_monitor.holdings import (
    get_positions, get_snapshot, LARGE_CAP, SECTOR_MAP,
)
from pipeline.portfolio_monitor.emailer import send_email, is_dry_run

log = get_logger("portfolio_monitor.calendar")

PROJECT = Path(__file__).resolve().parents[2]
STATE_FILE = PROJECT / "data" / "calendar_100day_state.json"

# 100-day review positions (set 2026-07-01)
REVIEW_TICKERS = ["BALRAMCHIN", "KNRCON", "UNOMINDA"]

# Trim threshold: % below the reference price (set on --init or first run)
TRIM_THRESHOLD_PCT = 15.0

# Add a "review window" around the 100-day mark so we get reminded
REVIEW_DAYS_WINDOW = 7  # send the email if today is within ±7 days of 100-day

# The 100-day clock starts from this date (first run of --init)
INITIAL_DATE = datetime(2026, 7, 1)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"initialised_at": None, "reference_date": None,
                "reference_prices": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("could not read state file, resetting")
        return {"initialised_at": None, "reference_date": None,
                "reference_prices": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    log.info("state saved: %s", STATE_FILE)


def init_baseline() -> None:
    """Capture current prices as the 100-day reference baseline."""
    state = _load_state()
    if state.get("reference_prices"):
        log.warning("state already has reference prices; use --reset to overwrite")
    positions, source = get_positions()
    state["initialised_at"] = datetime.now().isoformat(timespec="seconds")
    state["reference_date"] = INITIAL_DATE.isoformat()
    state["reference_prices"] = {
        p.ticker: {"ltp": p.ltp, "qty": p.qty, "avg": p.avg,
                   "asof": p.asof}
        for p in positions if p.ticker in REVIEW_TICKERS
    }
    _save_state(state)
    print(f"Initialised 100-day reference with {source} data.")
    for t, v in state["reference_prices"].items():
        print(f"  {t}: ref ₹{v['ltp']:,.2f}  (qty {v['qty']}, avg ₹{v['avg']:,.2f})")


def reset_baseline() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    init_baseline()


def _days_since_initial(state: dict) -> int:
    rd = state.get("reference_date")
    if not rd:
        return 0
    try:
        ref = datetime.fromisoformat(rd)
        return (datetime.now() - ref).days
    except (ValueError, TypeError):
        return 0


def _is_review_window() -> bool:
    """True if today is within ±REVIEW_DAYS_WINDOW of day 100 from initial date."""
    state = _load_state()
    if not state.get("reference_date"):
        return False
    days = _days_since_initial(state)
    return 100 - REVIEW_DAYS_WINDOW <= days <= 100 + REVIEW_DAYS_WINDOW


def _evaluate_position(ticker: str, current: dict, ref: dict) -> dict:
    """Decide what to do with one position. Returns dict with action + reasoning."""
    cur_ltp = current["ltp"]
    ref_ltp = ref["ltp"]
    pct_change = (cur_ltp / ref_ltp - 1.0) * 100.0 if ref_ltp else 0.0
    pnl_pct = (cur_ltp / ref["avg"] - 1.0) * 100.0 if ref["avg"] else 0.0

    if pct_change <= -TRIM_THRESHOLD_PCT:
        action = "TRIM"
        reason = f"Down {pct_change:+.1f}% from 100-day ref ₹{ref_ltp:,.0f} (>{TRIM_THRESHOLD_PCT:.0f}% threshold)"
    elif pct_change >= 20.0:
        action = "BOOK PARTIAL PROFIT"
        reason = f"Up {pct_change:+.1f}% from 100-day ref ₹{ref_ltp:,.0f} — consider taking some off"
    else:
        action = "HOLD"
        reason = f"{pct_change:+.1f}% from 100-day ref ₹{ref_ltp:,.0f} (within ±{TRIM_THRESHOLD_PCT:.0f}% threshold)"

    return {
        "ticker": ticker,
        "action": action,
        "reason": reason,
        "ref_ltp": ref_ltp,
        "cur_ltp": cur_ltp,
        "pct_change": pct_change,
        "pnl_pct": pnl_pct,
        "qty": current["qty"],
        "cur_value": current["qty"] * cur_ltp,
    }


def build_report(force: bool = False) -> tuple[Optional[str], Optional[str], bool]:
    """
    Build the report. Returns (subject, html, should_send).
    should_send is True if at least one position warrants action OR --force
    was passed OR we're in the review window.
    """
    state = _load_state()
    if not state.get("reference_prices"):
        log.warning("no baseline — run --init first")
        return (None, None, False)

    positions, source = get_positions()
    current_map = {p.ticker: {
        "ltp": p.ltp, "qty": p.qty, "avg": p.avg, "asof": p.asof
    } for p in positions}

    days = _days_since_initial(state)
    in_window = _is_review_window()
    evaluations = []
    for t in REVIEW_TICKERS:
        ref = state["reference_prices"].get(t)
        cur = current_map.get(t)
        if not ref:
            evaluations.append({
                "ticker": t, "action": "NO DATA",
                "reason": f"No reference price stored for {t} — run --init again",
                "ref_ltp": None, "cur_ltp": None, "pct_change": 0,
                "pnl_pct": 0, "qty": 0, "cur_value": 0,
            })
            continue
        if not cur:
            evaluations.append({
                "ticker": t, "action": "MISSING",
                "reason": f"{t} not in current holdings (sold or not fetched)",
                "ref_ltp": ref["ltp"], "cur_ltp": None, "pct_change": 0,
                "pnl_pct": 0, "qty": 0, "cur_value": 0,
            })
            continue
        evaluations.append(_evaluate_position(t, cur, ref))

    # Decide whether to send
    any_action = any(e["action"] in ("TRIM", "BOOK PARTIAL PROFIT") for e in evaluations)
    should_send = force or in_window or any_action

    if not should_send:
        log.info("no actions needed, day %d since initial; skipping email", days)
        return (None, None, False)

    # Build subject + body
    trim_count = sum(1 for e in evaluations if e["action"] == "TRIM")
    book_count = sum(1 for e in evaluations if e["action"] == "BOOK PARTIAL PROFIT")
    hold_count = sum(1 for e in evaluations if e["action"] == "HOLD")
    if trim_count or book_count:
        subject = f"⚠️  100-day review (day {days}): {trim_count} TRIM, {book_count} BOOK"
    else:
        subject = f"✓  100-day review (day {days}): all 3 mid-caps HOLD"

    rows = []
    for e in evaluations:
        if e["cur_ltp"] is None:
            rows.append(f"<tr><td>{e['ticker']}</td><td colspan='4'>{e['reason']}</td></tr>")
            continue
        action_color = {
            "TRIM": "#d62728", "BOOK PARTIAL PROFIT": "#ff7f0e",
            "HOLD": "#2ca02c", "MISSING": "#999", "NO DATA": "#999",
        }.get(e["action"], "#000")
        rows.append(f"""
        <tr>
          <td style="font-weight:bold">{e['ticker']}</td>
          <td style="color:{action_color};font-weight:bold">{e['action']}</td>
          <td>₹{e['ref_ltp']:,.0f} → ₹{e['cur_ltp']:,.0f}</td>
          <td style="color:{action_color}">{e['pct_change']:+.1f}%</td>
          <td>{e['reason']}</td>
        </tr>""")

    initial_date = state.get("reference_date", "unknown")
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px">
      <h2 style="color:#333">100-Day Review — Day {days} from {initial_date[:10]}</h2>
      <p>Source: <b>{source}</b>. Generated {datetime.now().isoformat(timespec='seconds')}.</p>
      <table style="border-collapse:collapse;width:100%;margin-top:12px">
        <thead>
          <tr style="background:#eee">
            <th style="text-align:left;padding:6px">Ticker</th>
            <th style="text-align:left;padding:6px">Action</th>
            <th style="text-align:left;padding:6px">100d Ref → Current</th>
            <th style="text-align:left;padding:6px">Δ</th>
            <th style="text-align:left;padding:6px">Reasoning</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
      <h3 style="margin-top:24px">Summary</h3>
      <ul>
        <li><b>TRIM</b>: {trim_count} position(s) below -{TRIM_THRESHOLD_PCT:.0f}% threshold</li>
        <li><b>BOOK PARTIAL PROFIT</b>: {book_count} position(s) up &gt; 20%</li>
        <li><b>HOLD</b>: {hold_count} position(s) within threshold</li>
      </ul>
      <h3 style="margin-top:24px">Decision rules</h3>
      <ol>
        <li>If a position is down &gt; 15% from its 100-day ref price → TRIM</li>
        <li>If a position is up &gt; 20% from ref → BOOK PARTIAL PROFIT (10-30%)</li>
        <li>Otherwise HOLD and re-review in 100 days</li>
        <li>Manual override: if fundamentals changed (Q result miss, mgmt change, sector regulation), ignore the price-based action and re-decide manually</li>
      </ol>
      <p style="color:#888;font-size:12px;margin-top:24px">
        This is an automated alert from pipeline.portfolio_monitor.calendar.<br>
        State file: {STATE_FILE}<br>
        To re-baseline, run: <code>python -m pipeline.portfolio_monitor.calendar --reset</code>
      </p>
    </body></html>
    """

    plain = f"""100-DAY REVIEW — Day {days} from {initial_date[:10]}
Source: {source}

"""
    for e in evaluations:
        if e["cur_ltp"] is None:
            plain += f"{e['ticker']}: {e['reason']}\n"
        else:
            plain += (f"{e['ticker']}: {e['action']}  "
                      f"₹{e['ref_ltp']:,.0f} → ₹{e['cur_ltp']:,.0f}  "
                      f"({e['pct_change']:+.1f}%)  — {e['reason']}\n")
    plain += f"""

Decision rules:
- TRIM if down > 15% from ref
- BOOK PARTIAL PROFIT if up > 20% from ref
- HOLD otherwise
- Manual override if fundamentals changed
"""
    return subject, html, True, plain


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--init", action="store_true",
                   help="Capture current prices as 100-day reference baseline")
    p.add_argument("--reset", action="store_true",
                   help="Reset and re-init the 100-day reference")
    p.add_argument("--force", action="store_true",
                   help="Send email regardless of whether any action is needed")
    p.add_argument("--status", action="store_true",
                   help="Print current state and exit")
    args = p.parse_args()

    if args.init or args.reset:
        if args.reset:
            reset_baseline()
        else:
            init_baseline()
        return 0

    if args.status:
        state = _load_state()
        print(f"Initialised at: {state.get('initialised_at')}")
        print(f"Reference date: {state.get('reference_date')}")
        print(f"Day count:      {_days_since_initial(state)}")
        print(f"In review window: {_is_review_window()}")
        print(f"Reference prices:")
        for t, v in state.get("reference_prices", {}).items():
            print(f"  {t}: ₹{v['ltp']:,.2f}  (qty {v['qty']}, avg ₹{v['avg']:,.2f})")
        return 0

    result = build_report(force=args.force)
    if len(result) == 3:
        subject, html, should_send = result
        plain = ""
    else:
        subject, html, should_send, plain = result

    if not should_send:
        print("No actions needed; nothing to send.")
        return 0

    print(f"Subject: {subject}")
    print(f"Sending email (dry_run={is_dry_run()})...")
    res = send_email(subject, plain or subject, html)
    print(f"Result: {res}")
    return 0 if res.get("sent") or res.get("mode") == "dry_run" else 1


if __name__ == "__main__":
    sys.exit(main())
