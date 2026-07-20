"""
pipeline.portfolio_monitor.concentration_check
-----------------------------------------------
Weekly check on portfolio concentration + Gold:Silver rotation signal.

Per user spec (2026-07-01):
  - Flags any position crossing the 20% concentration threshold
  - Includes the Gold:Silver ratio so the user knows when to rotate
    GOLDBEES → SILVERBEES (per their roadmap)
  - Sends email alert when thresholds are breached

Default thresholds (configurable via env):
  PM_CONC_TOP1_MAX        20.0   (% — single-position limit)
  PM_CONC_TOP2_MAX        35.0   (% — top-2 combined limit)
  PM_CONC_TOP3_MAX        50.0   (% — top-3 combined limit)
  PM_CONC_GSILVER_RATIO_HIGH  90.0  (Gold:Silver ratio > this = buy silver)
  PM_CONC_GSILVER_RATIO_LOW   60.0  (Gold:Silver ratio < this = take profit on silver)

Schedule (weekly):
  0 10 * * 1   /usr/bin/python3 -m pipeline.portfolio_monitor.concentration_check
  (every Monday 10am IST)

CLI:
  python -m pipeline.portfolio_monitor.concentration_check
      → check, email only if any threshold breached
  python -m pipeline.portfolio_monitor.concentration_check --force
      → always send the email (weekly status)
  python -m pipeline.portfolio_monitor.concentration_check --status
      → print current state, no email
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yfinance as yf

from pipeline.logging_setup import get_logger
from pipeline.portfolio_monitor.holdings import get_snapshot
from pipeline.portfolio_monitor.emailer import send_email, is_dry_run

log = get_logger("portfolio_monitor.concentration")

PROJECT = Path(__file__).resolve().parents[2]

# Thresholds (overridable via env)
def _threshold(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return default

TOP1_MAX = _threshold("PM_CONC_TOP1_MAX", 20.0)
TOP2_MAX = _threshold("PM_CONC_TOP2_MAX", 35.0)
TOP3_MAX = _threshold("PM_CONC_TOP3_MAX", 50.0)
GSILVER_HIGH = _threshold("PM_CONC_GSILVER_RATIO_HIGH", 90.0)
GSILVER_LOW = _threshold("PM_CONC_GSILVER_RATIO_LOW", 60.0)


# ---------- Gold:Silver ratio ----------
def get_gold_silver_ratio() -> Optional[dict]:
    """
    Fetch the Gold:Silver ratio (oz/oz) from yfinance.
    GC=F is gold futures, SI=F is silver futures. Both in USD/oz.
    """
    try:
        gold = yf.Ticker("GC=F").info or {}
        time.sleep(0.3)
        silver = yf.Ticker("SI=F").info or {}
        time.sleep(0.3)
        g = gold.get("regularMarketPrice") or gold.get("previousClose")
        s = silver.get("regularMarketPrice") or silver.get("previousClose")
        if not g or not s or s == 0:
            return None
        ratio = g / s
        # Historical mean ~60-70, recent range 80-100
        return {
            "gold_usd_oz": g,
            "silver_usd_oz": s,
            "ratio": round(ratio, 2),
            "asof": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as e:
        log.warning("gold:silver ratio fetch failed: %s", e)
        return None


def _gsilver_signal(ratio: float) -> tuple[str, str]:
    """Return (signal_label, color) for the gold:silver ratio."""
    if ratio >= GSILVER_HIGH:
        return ("BUY SILVER — ratio is historically high", "#d62728")
    elif ratio <= GSILVER_LOW:
        return ("TAKE PROFIT ON SILVER — ratio is historically low", "#ff7f0e")
    else:
        return ("HOLD — ratio in neutral zone", "#2ca02c")


# ---------- Concentration checks ----------
def check_concentration(snap: dict) -> list[dict]:
    """Return list of breach alerts: [{level, tickers, weight, threshold}, ...]"""
    weights = [(p["ticker"], p["weight"]) for p in snap["positions"]]
    weights.sort(key=lambda x: -x[1])
    alerts = []

    # Top-1
    if weights and weights[0][1] > TOP1_MAX:
        alerts.append({
            "level": "TOP-1",
            "tickers": [weights[0][0]],
            "weight": weights[0][1],
            "threshold": TOP1_MAX,
        })

    # Top-2
    if len(weights) >= 2:
        top2 = sum(w for _, w in weights[:2])
        if top2 > TOP2_MAX:
            alerts.append({
                "level": "TOP-2",
                "tickers": [t for t, _ in weights[:2]],
                "weight": top2,
                "threshold": TOP2_MAX,
            })

    # Top-3
    if len(weights) >= 3:
        top3 = sum(w for _, w in weights[:3])
        if top3 > TOP3_MAX:
            alerts.append({
                "level": "TOP-3",
                "tickers": [t for t, _ in weights[:3]],
                "weight": top3,
                "threshold": TOP3_MAX,
            })

    return alerts


# ---------- Report ----------
def build_report(force: bool = False) -> tuple[Optional[str], Optional[str], bool, str]:
    snap = get_snapshot()
    conc_alerts = check_concentration(snap)
    gs = get_gold_silver_ratio()

    gs_alert = False
    gs_signal = None
    if gs:
        gs_signal, _ = _gsilver_signal(gs["ratio"])
        if "BUY" in gs_signal or "PROFIT" in gs_signal:
            gs_alert = True

    should_send = force or conc_alerts or gs_alert

    if not should_send:
        log.info("no concentration or rotation alerts; skipping email")
        return (None, None, False, "")

    # Build subject + body
    parts = []
    if conc_alerts:
        parts.append(f"{len(conc_alerts)} concentration breach")
    if gs_alert:
        parts.append("Gold:Silver rotation signal")
    if not parts:
        subject = f"✓  Portfolio concentration check (no breaches; G:S ratio {gs['ratio']:.1f})" if gs else "✓  Portfolio concentration check (no breaches)"
    else:
        subject = "⚠️  Portfolio: " + " + ".join(parts)

    # Build body
    rows = []
    for p in snap["positions"][:7]:  # top 7
        flag = ""
        if p["weight"] > TOP1_MAX:
            flag = " 🔴"
        elif p["weight"] > TOP1_MAX * 0.85:
            flag = " 🟡"
        rows.append(f"""
        <tr>
          <td style="padding:4px">{p['ticker']}</td>
          <td style="padding:4px;text-align:right">₹{p['cur_value']:,.0f}</td>
          <td style="padding:4px;text-align:right">{p['weight']:.1f}%{flag}</td>
          <td style="padding:4px;text-align:right">{p['pnl_pct']:+.1f}%</td>
        </tr>""")

    conc_section = ""
    if conc_alerts:
        conc_items = "".join(
            f"<li><b>{a['level']}</b>: {', '.join(a['tickers'])} = {a['weight']:.1f}% "
            f"(threshold {a['threshold']:.0f}%)</li>"
            for a in conc_alerts
        )
        conc_section = f"""
        <h3 style="color:#d62728">⚠️ Concentration breaches</h3>
        <ul>{conc_items}</ul>
        <p><i>Threshold: Top-1 &gt; {TOP1_MAX:.0f}%, Top-2 &gt; {TOP2_MAX:.0f}%, Top-3 &gt; {TOP3_MAX:.0f}%</i></p>
        """

    gs_section = ""
    if gs:
        ratio = gs["ratio"]
        signal, color = _gsilver_signal(ratio)
        gs_section = f"""
        <h3>Gold:Silver ratio</h3>
        <table style="border-collapse:collapse">
          <tr><td style="padding:3px">Gold (USD/oz):</td>
              <td style="padding:3px;text-align:right">${gs['gold_usd_oz']:,.2f}</td></tr>
          <tr><td style="padding:3px">Silver (USD/oz):</td>
              <td style="padding:3px;text-align:right">${gs['silver_usd_oz']:,.2f}</td></tr>
          <tr><td style="padding:3px"><b>Ratio:</b></td>
              <td style="padding:3px;text-align:right"><b>{ratio:.2f}</b></td></tr>
        </table>
        <p style="color:{color};font-weight:bold;margin-top:6px">→ {signal}</p>
        <p style="color:#888;font-size:11px">
          Historical mean ≈ 60-70. Current reading: {ratio:.1f}.
          Threshold: BUY signal &gt; {GSILVER_HIGH:.0f}, take-profit &lt; {GSILVER_LOW:.0f}.
        </p>
        """
    else:
        gs_section = "<p><i>Gold:Silver ratio fetch failed — check yfinance connectivity.</i></p>"

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px">
      <h2>Portfolio Concentration Check — {datetime.now().strftime('%Y-%m-%d %H:%M')}</h2>
      <p>Source: <b>{snap['source']}</b>. Total value: <b>₹{snap['total_value']:,.0f}</b></p>

      {conc_section}

      <h3>Top 7 positions by weight</h3>
      <table style="border-collapse:collapse;width:100%">
        <thead>
          <tr style="background:#eee">
            <th style="text-align:left;padding:4px">Ticker</th>
            <th style="text-align:right;padding:4px">Value</th>
            <th style="text-align:right;padding:4px">Weight</th>
            <th style="text-align:right;padding:4px">P&amp;L %</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>

      {gs_section}

      <h3 style="margin-top:24px">Action items</h3>
      <ul>
        {''.join(f'<li>Concentration: trim {a["tickers"][0]} to bring {a["level"]} weight below {a["threshold"]:.0f}%</li>' for a in conc_alerts) or '<li>No concentration actions</li>'}
        {f'<li>Gold:Silver rotation: review GOLDBEES position — ratio at {gs["ratio"]:.1f}</li>' if gs_alert else ''}
      </ul>

      <p style="color:#888;font-size:11px;margin-top:24px">
        Automated alert from pipeline.portfolio_monitor.concentration_check.<br>
        Thresholds (configurable via env): TOP1={TOP1_MAX:.0f}%, TOP2={TOP2_MAX:.0f}%, TOP3={TOP3_MAX:.0f}%, GS_HI={GSILVER_HIGH:.0f}, GS_LO={GSILVER_LOW:.0f}.
      </p>
    </body></html>
    """

    plain = f"""PORTFOLIO CONCENTRATION CHECK — {datetime.now().strftime('%Y-%m-%d %H:%M')}
Source: {snap['source']}. Total value: ₹{snap['total_value']:,.0f}

"""
    if conc_alerts:
        plain += "CONCENTRATION BREACHES:\n"
        for a in conc_alerts:
            plain += f"  - {a['level']}: {', '.join(a['tickers'])} = {a['weight']:.1f}% (threshold {a['threshold']:.0f}%)\n"
        plain += "\n"
    plain += "TOP 7 POSITIONS:\n"
    for p in snap["positions"][:7]:
        plain += f"  {p['ticker']:<14} ₹{p['cur_value']:>10,.0f}  {p['weight']:>5.1f}%  P&L {p['pnl_pct']:+.1f}%\n"
    if gs:
        plain += f"\nGOLD:SILVER:\n  Gold ${gs['gold_usd_oz']:,.2f}/oz, Silver ${gs['silver_usd_oz']:,.2f}/oz, ratio {gs['ratio']:.1f}\n  → {gs_signal}\n"
    return subject, html, True, plain


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true",
                   help="Send email regardless of whether any threshold is breached")
    p.add_argument("--status", action="store_true",
                   help="Print current state and exit (no email)")
    args = p.parse_args()

    if args.status:
        snap = get_snapshot()
        conc = check_concentration(snap)
        gs = get_gold_silver_ratio()
        print(f"Source: {snap['source']}  Total: ₹{snap['total_value']:,.0f}")
        print(f"\nTop 3 positions:")
        for p in snap["positions"][:3]:
            print(f"  {p['ticker']:<14} {p['weight']:>5.1f}%")
        top2 = sum(w for _, w in [(p['ticker'], p['weight']) for p in snap["positions"][:2]])
        top3 = sum(w for _, w in [(p['ticker'], p['weight']) for p in snap["positions"][:3]])
        print(f"\nTop-2: {top2:.1f}% (limit {TOP2_MAX:.0f}%)")
        print(f"Top-3: {top3:.1f}% (limit {TOP3_MAX:.0f}%)")
        print(f"\nConcentration breaches: {len(conc)}")
        for a in conc:
            print(f"  {a['level']}: {', '.join(a['tickers'])} = {a['weight']:.1f}%")
        if gs:
            sig, _ = _gsilver_signal(gs["ratio"])
            print(f"\nGold:Silver ratio: {gs['ratio']:.1f} → {sig}")
        else:
            print("\nGold:Silver ratio: fetch failed")
        return 0

    result = build_report(force=args.force)
    subject, html, should_send, plain = result
    if not should_send:
        print("No alerts; nothing to send.")
        return 0

    print(f"Subject: {subject}")
    print(f"Sending email (dry_run={is_dry_run()})...")
    res = send_email(subject, plain or subject, html)
    print(f"Result: {res}")
    return 0 if res.get("sent") or res.get("mode") == "dry_run" else 1


if __name__ == "__main__":
    sys.exit(main())
