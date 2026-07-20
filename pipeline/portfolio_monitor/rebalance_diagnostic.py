"""
pipeline.portfolio_monitor.rebalance_diagnostic
-------------------------------------------------
Monthly portfolio drift diagnostic.

Per user spec (2026-07-01):
  - Sector mix snapshot (% in FMCG, banks, power, etc.)
  - Top-2 concentration
  - Average P/E and ROE of the equity book (yfinance)
  - Detects drift from baseline (set on first run)

Schedule (monthly, 1st of month):
  0 9 1 * *  /usr/bin/python3 -m pipeline.portfolio_monitor.rebalance_diagnostic

CLI:
  python -m pipeline.portfolio_monitor.rebalance_diagnostic
      → monthly report, email only if drift > threshold
  python -m pipeline.portfolio_monitor.rebalance_diagnostic --force
      → always send (e.g. first of month)
  python -m pipeline.portfolio_monitor.rebalance_diagnostic --init-baseline
      → capture current state as the reference for drift detection
  python -m pipeline.portfolio_monitor.rebalance_diagnostic --status
      → print current state, no email
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yfinance as yf

from pipeline.logging_setup import get_logger
from pipeline.portfolio_monitor.holdings import (
    get_snapshot, get_sector_mix, get_large_cap_share,
    LARGE_CAP, SECTOR_MAP,
)
from pipeline.portfolio_monitor.emailer import send_email, is_dry_run

log = get_logger("portfolio_monitor.rebalance")

PROJECT = Path(__file__).resolve().parents[2]
BASELINE_FILE = PROJECT / "data" / "rebalance_baseline.json"

# Drift thresholds (% vs baseline)
SECTOR_DRIFT_PCT = 5.0      # any sector drifting ±5% from baseline flags
CONC_DRIFT_PCT = 3.0        # top-2 concentration drifting ±3% flags
LARGE_CAP_DRIFT_PCT = 5.0   # large-cap share drifting ±5% flags

# Cache file for yfinance fundamentals (avoid refetching monthly)
FUND_CACHE_FILE = PROJECT / "data" / "fundamentals_cache.json"
FUND_CACHE_TTL_DAYS = 7


# ---------- Fundamentals from yfinance ----------
def _load_fund_cache() -> dict:
    if not FUND_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(FUND_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_fund_cache(data: dict) -> None:
    FUND_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FUND_CACHE_FILE.write_text(json.dumps(data, indent=2, default=str))


def fetch_fundamentals(tickers: list[str]) -> dict[str, dict]:
    """
    Fetch P/E, ROE, and market cap for a list of NSE tickers.
    Caches results for FUND_CACHE_TTL_DAYS to avoid rate limits.
    Skips ETF tickers (GOLDBEES, METALIETF, NEXT50IETF).

    NB on dividendYield: yfinance is inconsistent — some tickers return
    a fraction (0.04 = 4%), others return a percentage (4.0). We treat
    anything > 1.0 as already-in-percentage and normalise everything to
    percentage (so 4.0 means 4%).
    """
    cache = _load_fund_cache()
    now = datetime.now()
    cutoff_iso = now.isoformat()

    out: dict[str, dict] = {}
    fetch_needed: list[str] = []

    for t in tickers:
        if t in ("GOLDBEES", "METALIETF", "NEXT50IETF"):
            out[t] = {"is_etf": True}
            continue
        cached = cache.get(t)
        if cached and (now - datetime.fromisoformat(cached["fetched_at"])).days < FUND_CACHE_TTL_DAYS:
            out[t] = cached
        else:
            fetch_needed.append(t)

    if fetch_needed:
        log.info("fetching fundamentals for %d tickers via yfinance", len(fetch_needed))
        for t in fetch_needed:
            try:
                sym = f"{t}.NS"
                info = yf.Ticker(sym).info or {}
                time.sleep(0.4)
                # Normalise dividendYield: yfinance is inconsistent. For Indian
                # stocks a sensible yield is 0-20%. Values > 0.20 are treated as
                # already-in-percent (e.g. 5.51 = 5.51%); values <= 0.20 are
                # treated as fraction (e.g. 0.0046 = 0.46%). We then cap at 20%
                # to filter out clearly-wrong yfinance values for unusual cases
                # (JIOFIN/UNOMINDA/KNRCON all return raw values 19-24 which are
                # almost certainly yfinance computation bugs, not real yields).
                raw_dy = info.get("dividendYield") or 0
                if raw_dy > 0.20:
                    norm_dy_pct = raw_dy
                else:
                    norm_dy_pct = raw_dy * 100
                if norm_dy_pct > 20.0:
                    log.warning("yfinance returned div yield %s%% for %s — treating as data error, capping at 0", norm_dy_pct, t)
                    norm_dy_pct = 0
                out[t] = {
                    "fetched_at": now.isoformat(),
                    "pe": info.get("trailingPE"),
                    "pb": info.get("priceToBook"),
                    "roe_pct": (info.get("returnOnEquity") or 0) * 100,
                    "div_yield_pct": norm_dy_pct,
                    "mcap_cr": (info.get("marketCap") or 0) / 1e7,
                }
            except Exception as e:
                log.warning("yfinance failed for %s: %s", t, e)
                out[t] = {"fetched_at": now.isoformat(), "error": str(e)}
        # Save updated cache
        cache.update({k: v for k, v in out.items() if "fetched_at" in v})
        _save_fund_cache(cache)

    return out


def compute_portfolio_metrics(snap: dict) -> dict:
    """Compute weighted-avg P/E, P/B, ROE, div yield for non-ETF positions."""
    tickers = [p["ticker"] for p in snap["positions"]]
    funds = fetch_fundamentals(tickers)

    weighted_pe = 0.0
    weighted_pb = 0.0
    weighted_roe = 0.0
    weighted_dy = 0.0
    total_weight = 0.0
    counted = 0
    n_with_pe = 0
    n_with_roe = 0

    for p in snap["positions"]:
        t = p["ticker"]
        f = funds.get(t, {})
        if f.get("is_etf"):
            continue
        if not f or f.get("error"):
            continue
        w = p["weight"]  # already in %
        total_weight += w
        if f.get("pe") and f["pe"] > 0:
            weighted_pe += f["pe"] * w
            n_with_pe += 1
        if f.get("pb") and f["pb"] > 0:
            weighted_pb += f["pb"] * w
        if f.get("roe_pct"):
            weighted_roe += f["roe_pct"] * w
            n_with_roe += 1
        if f.get("div_yield_pct"):
            weighted_dy += f["div_yield_pct"] * w
        counted += 1

    return {
        "n_counted": counted,
        "n_with_pe": n_with_pe,
        "n_with_roe": n_with_roe,
        "weighted_pe": round(weighted_pe / total_weight, 2) if total_weight else None,
        "weighted_pb": round(weighted_pb / total_weight, 2) if total_weight else None,
        "weighted_roe_pct": round(weighted_roe / total_weight, 2) if total_weight else None,
        "weighted_div_yield_pct": round(weighted_dy / total_weight, 2) if total_weight else None,
    }


# ---------- Baseline + drift ----------
def _load_baseline() -> dict:
    if not BASELINE_FILE.exists():
        return {}
    try:
        return json.loads(BASELINE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_baseline(snap: dict, sector_mix: dict, large_cap_pct: float, metrics: dict) -> None:
    baseline = {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "total_value": snap["total_value"],
        "top1_pct": snap["positions"][0]["weight"] if snap["positions"] else 0,
        "top2_pct": sum(p["weight"] for p in snap["positions"][:2]),
        "top3_pct": sum(p["weight"] for p in snap["positions"][:3]),
        "sector_mix": sector_mix["by_sector"],
        "large_cap_pct": large_cap_pct,
        "metrics": metrics,
    }
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2, default=str))
    log.info("baseline saved to %s", BASELINE_FILE)


def detect_drift(current: dict, baseline: dict) -> list[dict]:
    """Return list of drift alerts."""
    if not baseline:
        return []
    alerts = []

    # Sector drift
    base_sectors = baseline.get("sector_mix", {})
    cur_sectors = current.get("sector_mix", {}).get("by_sector", {})
    for sec in set(base_sectors) | set(cur_sectors):
        b = base_sectors.get(sec, 0)
        c = cur_sectors.get(sec, 0)
        diff = c - b
        if abs(diff) >= SECTOR_DRIFT_PCT:
            direction = "↑" if diff > 0 else "↓"
            alerts.append({
                "type": "SECTOR",
                "label": f"{sec}: {b:.1f}% → {c:.1f}% ({direction}{abs(diff):.1f}%)",
                "drift_pct": abs(diff),
            })

    # Concentration drift
    base_top2 = baseline.get("top2_pct", 0)
    cur_top2 = current.get("top2_pct", 0)
    diff = cur_top2 - base_top2
    if abs(diff) >= CONC_DRIFT_PCT:
        direction = "↑" if diff > 0 else "↓"
        alerts.append({
            "type": "CONC",
            "label": f"Top-2 concentration: {base_top2:.1f}% → {cur_top2:.1f}% ({direction}{abs(diff):.1f}%)",
            "drift_pct": abs(diff),
        })

    # Large-cap drift
    base_lc = baseline.get("large_cap_pct", 0)
    cur_lc = current.get("large_cap_pct", 0)
    diff = cur_lc - base_lc
    if abs(diff) >= LARGE_CAP_DRIFT_PCT:
        direction = "↑" if diff > 0 else "↓"
        alerts.append({
            "type": "LARGE_CAP",
            "label": f"Large-cap share: {base_lc:.1f}% → {cur_lc:.1f}% ({direction}{abs(diff):.1f}%)",
            "drift_pct": abs(diff),
        })

    return alerts


# ---------- Report ----------
def build_report(force: bool = False) -> tuple[Optional[str], Optional[str], bool, str]:
    snap = get_snapshot()
    sector_mix = get_sector_mix()
    large_cap_pct = get_large_cap_share()
    metrics = compute_portfolio_metrics(snap)
    top2_pct = sum(p["weight"] for p in snap["positions"][:2])
    top3_pct = sum(p["weight"] for p in snap["positions"][:3])

    current = {
        "total_value": snap["total_value"],
        "top2_pct": top2_pct,
        "top3_pct": top3_pct,
        "sector_mix": sector_mix,
        "large_cap_pct": large_cap_pct,
        "metrics": metrics,
    }
    baseline = _load_baseline()
    drift = detect_drift(current, baseline)

    should_send = force or bool(drift) or not baseline  # always send if no baseline yet

    if not should_send:
        log.info("no drift detected, no baseline; skipping email")
        return (None, None, False, "")

    # Build subject
    if not baseline:
        subject = f"📊  Portfolio Monthly Diagnostic (no baseline yet — will start tracking drift)"
    elif drift:
        subject = f"⚠️  Portfolio drift: {len(drift)} items"
    else:
        subject = f"✓  Portfolio monthly diagnostic — within drift thresholds"

    # Sector table
    sector_rows = []
    for sec, pct in sector_mix["by_sector"].items():
        base_pct = baseline.get("sector_mix", {}).get(sec, 0) if baseline else 0
        drift_pct = pct - base_pct
        drift_str = f"{drift_pct:+.1f}%" if baseline else "—"
        drift_color = "#d62728" if abs(drift_pct) >= SECTOR_DRIFT_PCT else "#666"
        sector_rows.append(f"""
        <tr>
          <td style="padding:4px">{sec}</td>
          <td style="padding:4px;text-align:right">{pct:.1f}%</td>
          <td style="padding:4px;text-align:right">{base_pct:.1f}%</td>
          <td style="padding:4px;text-align:right;color:{drift_color}">{drift_str}</td>
        </tr>""")

    # Metrics table
    m = metrics
    metrics_html = f"""
    <h3 style="margin-top:24px">Equity book fundamentals (weighted by holding %)</h3>
    <table style="border-collapse:collapse">
      <tr><td style="padding:3px">Weighted P/E:</td>
          <td style="padding:3px;text-align:right"><b>{m.get('weighted_pe') or '—'}</b>
              <span style="color:#888;font-size:11px">  ({m.get('n_with_pe', 0)} of {m.get('n_counted', 0)} stocks with P/E)</span></td></tr>
      <tr><td style="padding:3px">Weighted P/B:</td>
          <td style="padding:3px;text-align:right"><b>{m.get('weighted_pb') or '—'}</b></td></tr>
      <tr><td style="padding:3px">Weighted ROE:</td>
          <td style="padding:4px;text-align:right"><b>{m.get('weighted_roe_pct') or '—'}%</b>
              <span style="color:#888;font-size:11px">  ({m.get('n_with_roe', 0)} of {m.get('n_counted', 0)} stocks with ROE)</span></td></tr>
      <tr><td style="padding:3px">Weighted Div Yield:</td>
          <td style="padding:3px;text-align:right"><b>{m.get('weighted_div_yield_pct') or '—'}%</b></td></tr>
    </table>
    <p style="color:#888;font-size:11px">ETFs (GOLDBEES, METALIETF, NEXT50IETF) excluded from fundamentals.</p>
    """

    # Concentration summary
    top1 = snap["positions"][0] if snap["positions"] else None
    conc_html = f"""
    <h3>Concentration</h3>
    <table style="border-collapse:collapse">
      <tr><td style="padding:3px">Top-1 ({top1['ticker'] if top1 else '—'}):</td>
          <td style="padding:3px;text-align:right"><b>{top1['weight']:.1f}%</b></td></tr>
      <tr><td style="padding:3px">Top-2:</td>
          <td style="padding:3px;text-align:right"><b>{top2_pct:.1f}%</b>
              <span style="color:#888;font-size:11px">  (baseline: {baseline.get('top2_pct', '—')}%)</span></td></tr>
      <tr><td style="padding:3px">Top-3:</td>
          <td style="padding:3px;text-align:right"><b>{top3_pct:.1f}%</b></td></tr>
      <tr><td style="padding:3px">Large-cap share:</td>
          <td style="padding:3px;text-align:right"><b>{large_cap_pct:.1f}%</b>
              <span style="color:#888;font-size:11px">  (baseline: {baseline.get('large_cap_pct', '—')}%)</span></td></tr>
    </table>
    """

    # Drift alerts
    drift_html = ""
    if drift:
        items = "".join(f"<li>{a['label']}</li>" for a in drift)
        drift_html = f"""
        <h3 style="color:#d62728;margin-top:24px">⚠️ Drift alerts</h3>
        <ul>{items}</ul>
        <p style="color:#888;font-size:11px">
          Thresholds: sector ±{SECTOR_DRIFT_PCT:.0f}%, top-2 ±{CONC_DRIFT_PCT:.0f}%, large-cap ±{LARGE_CAP_DRIFT_PCT:.0f}%.
        </p>
        """
    elif baseline:
        drift_html = "<p style='color:#2ca02c'>✓ No drift above thresholds.</p>"

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px">
      <h2>Portfolio Monthly Diagnostic — {datetime.now().strftime('%B %Y')}</h2>
      <p>Source: <b>{snap['source']}</b>. Total value: <b>₹{snap['total_value']:,.0f}</b>.</p>

      {conc_html}

      <h3 style="margin-top:18px">Sector mix</h3>
      <table style="border-collapse:collapse;width:100%">
        <thead>
          <tr style="background:#eee">
            <th style="text-align:left;padding:4px">Sector</th>
            <th style="text-align:right;padding:4px">Current</th>
            <th style="text-align:right;padding:4px">Baseline</th>
            <th style="text-align:right;padding:4px">Drift</th>
          </tr>
        </thead>
        <tbody>{''.join(sector_rows)}</tbody>
      </table>

      {metrics_html}

      {drift_html}

      <p style="color:#888;font-size:11px;margin-top:24px">
        Automated report from pipeline.portfolio_monitor.rebalance_diagnostic.<br>
        Baseline file: {BASELINE_FILE}<br>
        To re-baseline, run: <code>python -m pipeline.portfolio_monitor.rebalance_diagnostic --init-baseline</code>
      </p>
    </body></html>
    """

    plain = f"""PORTFOLIO MONTHLY DIAGNOSTIC — {datetime.now().strftime('%B %Y')}
Source: {snap['source']}. Total value: ₹{snap['total_value']:,.0f}.

CONCENTRATION:
  Top-1: {top1['ticker'] if top1 else '—'} = {top1['weight'] if top1 else 0:.1f}%
  Top-2: {top2_pct:.1f}%  (baseline: {baseline.get('top2_pct', '—')}%)
  Top-3: {top3_pct:.1f}%
  Large-cap share: {large_cap_pct:.1f}%  (baseline: {baseline.get('large_cap_pct', '—')}%)

SECTOR MIX:
"""
    for sec, pct in sector_mix["by_sector"].items():
        base_pct = baseline.get("sector_mix", {}).get(sec, 0) if baseline else 0
        plain += f"  {sec:<28} {pct:>5.1f}%  (baseline {base_pct:.1f}%)\n"

    plain += f"""
FUNDAMENTALS (weighted by holding %):
  Weighted P/E:      {m.get('weighted_pe') or '—'}
  Weighted P/B:      {m.get('weighted_pb') or '—'}
  Weighted ROE:      {m.get('weighted_roe_pct') or '—'}%
  Weighted Div Yld:  {m.get('weighted_div_yield_pct') or '—'}%
"""
    if drift:
        plain += "\nDRIFT ALERTS:\n"
        for a in drift:
            plain += f"  - {a['label']}\n"
    return subject, html, True, plain


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="Always send email")
    p.add_argument("--init-baseline", action="store_true",
                   help="Capture current state as the drift baseline")
    p.add_argument("--status", action="store_true",
                   help="Print current state and exit (no email)")
    args = p.parse_args()

    if args.init_baseline:
        snap = get_snapshot()
        sector_mix = get_sector_mix()
        large_cap_pct = get_large_cap_share()
        metrics = compute_portfolio_metrics(snap)
        save_baseline(snap, sector_mix, large_cap_pct, metrics)
        print(f"Baseline saved to {BASELINE_FILE}")
        return 0

    if args.status:
        snap = get_snapshot()
        sm = get_sector_mix()
        lc = get_large_cap_share()
        m = compute_portfolio_metrics(snap)
        top2 = sum(p["weight"] for p in snap["positions"][:2])
        print(f"Source: {snap['source']}  Total: ₹{snap['total_value']:,.0f}")
        print(f"Top-2: {top2:.1f}%  Large-cap: {lc:.1f}%")
        print(f"\nSector mix:")
        for s, p in sm["by_sector"].items():
            print(f"  {s:<28} {p:>5.1f}%")
        print(f"\nFundamentals (non-ETF):")
        print(f"  Weighted P/E:    {m.get('weighted_pe') or '—'}")
        print(f"  Weighted P/B:    {m.get('weighted_pb') or '—'}")
        print(f"  Weighted ROE:    {m.get('weighted_roe_pct') or '—'}%")
        print(f"  Weighted Div:    {m.get('weighted_div_yield_pct') or '—'}%")
        baseline = _load_baseline()
        if baseline:
            drift = detect_drift({
                "total_value": snap["total_value"],
                "top2_pct": top2,
                "sector_mix": sm,
                "large_cap_pct": lc,
            }, baseline)
            print(f"\nDrift alerts: {len(drift)}")
            for a in drift:
                print(f"  {a['label']}")
        else:
            print("\nNo baseline yet — run --init-baseline")
        return 0

    result = build_report(force=args.force)
    subject, html, should_send, plain = result
    if not should_send:
        print("No drift; nothing to send.")
        return 0

    print(f"Subject: {subject}")
    print(f"Sending email (dry_run={is_dry_run()})...")
    res = send_email(subject, plain or subject, html)
    print(f"Result: {res}")
    return 0 if res.get("sent") or res.get("mode") == "dry_run" else 1


if __name__ == "__main__":
    sys.exit(main())
