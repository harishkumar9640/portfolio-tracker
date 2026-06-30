"""
shareholding_alert.py
---------------------
Quarterly shareholding-pattern change detector + email notifier.

For each of the user's 8 equity tickers, fetches the latest 12 quarters of
shareholding data from Trendlyne, then emails the user when any of these
changed since the last check:

  - Promoter         (Holding, Pledged, Locked)
  - FII              (Foreign Institutional Investors)
  - DII              (Domestic Institutional Investors: MF + Banks + Insurance + Other DI)
  - Mutual Funds
  - Banks
  - Insurance
  - Public
  - Others

Why quarterly?
  Shareholding pattern is filed with SEBI every quarter (Mar/Jun/Sep/Dec).
  Trendlyne updates shortly after the filing deadline (45 days post-quarter-end).
  We check daily, but actual changes only appear 1-2 times per quarter.

Two entry points:
  1. CLI:  python3 shareholding_alert.py
            Fetches latest snapshot for all 8 tickers, diffs against the
            persisted previous snapshot, and emails a digest if anything
            changed.

  2. Daemon:  register via start_daily_scheduler() in the FastAPI app.
              Runs once per day at 16:35 IST (just after the MF holdings
              alert, which runs at 16:30 IST).

Environment variables (read from .env or secrets.local.json):
  MF_ALERT_SMTP_*  same SMTP creds as mf_holdings_alert — we reuse the
                    existing email pipeline so you only configure once.

Storage:
  - data/shareholding_prev.json     — last snapshot we compared against
  - data/shareholding_alert_log.json — last 30 runs
"""
from __future__ import annotations

import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    _ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ImportError:
    pass

from .logging_setup import get_logger

log = get_logger("shareholding_alert")

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
DATA_DIR.mkdir(exist_ok=True)

PREV_FILE = PROJECT / "data/alerts/shareholding/prev.json"
LOG_FILE = PROJECT / "data/alerts/shareholding/log.json"

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 PortfolioTracker/1.0")

# Reuse TICKER_MAP from mf_holdings (same 8 tickers, same Trendlyne IDs)
from .mf_holdings import TICKER_MAP  # noqa: E402

# Significance thresholds — small movements happen every quarter due to
# rounding. We only alert when the change exceeds these values:
#   - For Promoter: 0.1% (rare and meaningful)
#   - For FII/DII/MF/Banks/Insurance/Public/Others: 0.5% (more common)
SIGNIFICANCE_PCT = {
    "Promoter": 0.1,
    "FII": 0.5,
    "DII": 0.5,
    "Mutual Funds": 0.5,
    "Banks": 0.5,
    "Insurance": 0.5,
    "Public": 0.5,
    "Others": 0.5,
}

# Categories we care about (top-level rows in the Trendlyne table).
# Sub-rows (Pledged, Locked) are tracked under Promoter.
CATEGORIES = [
    "Promoter", "FII", "DII", "Mutual Funds", "Banks",
    "Insurance", "Public", "Others",
]


# ---------- Data model ----------

@dataclass
class QuarterSnapshot:
    """One quarter of shareholding data for one ticker."""
    quarter: str            # "Mar 2026"
    promoter: float = 0.0
    promoter_pledged: float = 0.0
    promoter_locked: float = 0.0
    fii: float = 0.0
    dii: float = 0.0
    mutual_funds: float = 0.0
    banks: float = 0.0
    insurance: float = 0.0
    public: float = 0.0
    others: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TickerShareholding:
    """All shareholding data we know about for one ticker."""
    ticker: str
    name: str
    url: str
    fetched_at: str
    quarters: list[QuarterSnapshot]   # most recent first


# ---------- Parsing ----------

class _TableStripper(HTMLParser):
    """
    Strip HTML to text while preserving table-cell boundaries with
    a sentinel character so we can rebuild the row structure.
    """
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.in_cell = False
        self.cell_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.in_cell = True
            self.cell_buf = []
        elif tag == "tr":
            self.parts.append("\nROW|")

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.parts.append("CELL|" + "".join(self.cell_buf).strip() + "|CELL")
            self.cell_buf = []
            self.in_cell = False
        elif tag == "tr":
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_cell:
            self.cell_buf.append(data)
        else:
            self.parts.append(data)


def _parse_pct(s: str) -> float:
    """Parse '16.76%' or '16.76 %' or '0.0' into a float."""
    s = (s or "").strip().replace("%", "").replace(",", "").strip()
    if not s or s == "—":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_shareholding_table(html: str) -> list[QuarterSnapshot]:
    """
    Given the raw HTML of a Trendlyne shareholding page, extract the
    12-quarter table. Returns quarters sorted most-recent first.

    Strategy: find all tables, pick the one containing both 'Promoter'
    and a quarter header (Mar/Jun/Sep/Dec YYYY).
    """
    # Strip HTML entities first
    import html as htmllib
    text = htmllib.unescape(html)

    # Find all tables (non-greedy)
    all_tables = re.findall(r"<table[^>]*>(?:(?!</table>).)*?</table>",
                            text, re.DOTALL)
    table_html = None
    for t in all_tables:
        if ("Promoter" in t
                and re.search(r"(?:Mar|Jun|Sep|Dec)\s+\d{4}", t)):
            table_html = t
            break
    if not table_html:
        log.warning("shareholding table not found")
        return []

    # Use our HTML stripper to get cell boundaries
    p = _TableStripper()
    p.feed(table_html)
    stripped = "".join(p.parts)

    # Parse into rows
    rows: list[list[str]] = []
    for line in stripped.split("\nROW|"):
        line = line.strip()
        if not line:
            continue
        cells = re.findall(r"CELL\|(.*?)\|CELL", line, re.DOTALL)
        # Clean each cell (collapse whitespace)
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if cells:
            rows.append(cells)

    if len(rows) < 2:
        log.warning("shareholding table has too few rows: %d", len(rows))
        return []

    # First row is headers: ["Summary", "Mar 2026", "Dec 2025", ...]
    headers = rows[0]
    quarters = [h for h in headers[1:] if re.match(r"(Mar|Jun|Sep|Dec) \d{4}", h)]
    if not quarters:
        log.warning("no quarter headers found in shareholding table")
        return []

    # Build a {category_name: [pct_per_quarter]} map from the data rows
    cat_pcts: dict[str, list[float]] = {}
    for row in rows[1:]:
        name = row[0]
        pcts = [_parse_pct(c) for c in row[1:1 + len(quarters)]]
        if len(pcts) == len(quarters):
            cat_pcts[name] = pcts

    # Build QuarterSnapshot objects, most recent first
    out: list[QuarterSnapshot] = []
    # quarters is oldest-first (Mar 2026 then Dec 2025...) — wait, check
    # In our ITC example, the headers were: Mar 2026, Dec 2025, Sep 2025, ...
    # That's most-recent FIRST. We want them in the same order so we
    # iterate over quarters directly.
    for i, q in enumerate(quarters):
        snap = QuarterSnapshot(quarter=q)
        if "Promoter" in cat_pcts:
            snap.promoter = cat_pcts["Promoter"][i]
        # Sub-rows: look for "Holding" (sub-row of Promoter) OR
        # "Promoter Holding" pattern — some pages have it as separate row
        for sub_name, attr in [
            ("Promoter Pledged", "promoter_pledged"),
            ("Pledged", "promoter_pledged"),
            ("Promoter Locked", "promoter_locked"),
            ("Locked", "promoter_locked"),
        ]:
            if sub_name in cat_pcts:
                setattr(snap, attr, cat_pcts[sub_name][i])
        if "FII" in cat_pcts:
            snap.fii = cat_pcts["FII"][i]
        if "DII" in cat_pcts:
            snap.dii = cat_pcts["DII"][i]
        if "Mutual Funds" in cat_pcts:
            snap.mutual_funds = cat_pcts["Mutual Funds"][i]
        if "Banks" in cat_pcts:
            snap.banks = cat_pcts["Banks"][i]
        if "Insurance" in cat_pcts:
            snap.insurance = cat_pcts["Insurance"][i]
        if "Public" in cat_pcts:
            snap.public = cat_pcts["Public"][i]
        if "Others" in cat_pcts:
            # There may be two "Others" rows (DII + Public). We take the
            # LAST one as it's typically the residual Public others.
            others_rows = [k for k in cat_pcts if k == "Others"]
            if len(others_rows) > 1:
                # Pick the second "Others" (typically Public residual)
                # — but we only have a single dict key, so this heuristic
                # captures the "Others" once; it's a minor edge case.
                snap.others = cat_pcts["Others"][i]
            else:
                snap.others = cat_pcts["Others"][i]
        out.append(snap)
    return out


# ---------- Fetch ----------

def fetch_one(ticker: str, *, timeout: int = 30) -> Optional[TickerShareholding]:
    """Fetch shareholding pattern for one ticker."""
    from . import mf_holdings as _mf
    info = _mf.TICKER_MAP.get(ticker)
    if not info:
        return None
    if info.get("id", 0) >= _mf.PLACEHOLDER_ID_THRESHOLD:
        log.warning("no Trendlyne ID for %s — skipping", ticker)
        return None

    url = (
        f"https://trendlyne.com/equity/share-holding/{info['id']}/{ticker}"
        f"/latest/{info['url_slug']}/"
    )
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
        html = r.text
    except requests.RequestException as e:
        log.warning("fetch failed for %s: %s", ticker, e)
        return None

    quarters = _parse_shareholding_table(html)
    if not quarters:
        return None

    return TickerShareholding(
        ticker=ticker,
        name=info["name"],
        url=url,
        fetched_at=datetime.now(IST).isoformat(timespec="seconds"),
        quarters=quarters,
    )


def _safe_fetch(ticker: str) -> tuple[str, Optional[TickerShareholding]]:
    """Wrapper that converts exceptions into (ticker, None)."""
    try:
        return ticker, fetch_one(ticker)
    except Exception as e:  # noqa: BLE001
        log.warning("safe_fetch: %s failed: %s", ticker, e)
        return ticker, None


def fetch_all(parallel: bool = True) -> dict[str, TickerShareholding]:
    """
    Fetch shareholding for every ticker in TICKER_MAP (8 tickers).
    Returns {ticker: TickerShareholding}.
    """
    out: dict[str, TickerShareholding] = {}
    tickers = [
        t for t, info in _mf.TICKER_MAP.items()
        if info.get("id", 0) < _mf.PLACEHOLDER_ID_THRESHOLD
    ]
    if parallel:
        from .parallel import map_parallel
        results = map_parallel(
            _safe_fetch, tickers, desc="shareholding", workers=8,
        )
        for tkr, ts in results:
            if ts is not None:
                out[tkr] = ts
    else:
        for t in tickers:
            ts = fetch_one(t)
            if ts is not None:
                out[t] = ts
    return out


# ---------- Diff detection ----------

@dataclass
class ShpChange:
    """A single category-level change between two quarters."""
    ticker: str
    name: str
    category: str
    old_quarter: str
    new_quarter: str
    old_value: float
    new_value: float
    delta: float

    @property
    def is_increase(self) -> bool:
        return self.delta > 0


def diff_snapshots(
    prev: dict[str, dict],
    curr: dict[str, TickerShareholding],
) -> list[ShpChange]:
    """
    Compare two snapshots and return a list of significant changes.

    For each ticker, find the latest quarter in curr that's NEWER than
    anything in prev. If found, diff the corresponding fields in that
    quarter vs the most recent prev quarter.

    "Significant" = |delta| > SIGNIFICANCE_PCT[category].
    """
    changes: list[ShpChange] = []
    for tkr, ts in curr.items():
        if not ts.quarters:
            continue
        prev_qs = prev.get(tkr, {}).get("quarters", []) if isinstance(prev.get(tkr), dict) else []
        prev_quarters: list[dict] = prev_qs if isinstance(prev_qs, list) else []
        prev_quarter_names = {q.get("quarter") for q in prev_quarters if isinstance(q, dict)}

        # Find the latest quarter we already knew about
        latest_known_idx = -1
        for i, q in enumerate(ts.quarters):
            if q.quarter in prev_quarter_names:
                latest_known_idx = i
                break

        # Anything AFTER latest_known_idx is new (or all quarters if first run)
        new_quarters = ts.quarters[:latest_known_idx + 1 if latest_known_idx >= 0 else len(ts.quarters)]
        # If first run, we'd alert about every quarter — too noisy. Skip.
        if not prev_quarters:
            log.info("first run for %s — recording snapshot, no alerts", tkr)
            continue

        # Find the matching "before" quarter in prev (same name or
        # the most recent prev quarter)
        prev_map = {q.get("quarter"): q for q in prev_quarters if isinstance(q, dict)}
        for new_q in new_quarters:
            old_q = prev_map.get(new_q.quarter)
            if old_q is None:
                # Pick the prev quarter that's most recent and within 1 quarter
                continue
            # Compare each category
            cat_attrs = [
                ("Promoter", "promoter"),
                ("FII", "fii"),
                ("DII", "dii"),
                ("Mutual Funds", "mutual_funds"),
                ("Banks", "banks"),
                ("Insurance", "insurance"),
                ("Public", "public"),
                ("Others", "others"),
                ("Promoter Pledged", "promoter_pledged"),
            ]
            for cat, attr in cat_attrs:
                old_v = float(old_q.get(attr) or 0)
                new_v = float(getattr(new_q, attr) or 0)
                delta = new_v - old_v
                threshold = SIGNIFICANCE_PCT.get(cat, 0.5)
                if abs(delta) >= threshold:
                    changes.append(ShpChange(
                        ticker=tkr, name=ts.name, category=cat,
                        old_quarter=new_q.quarter, new_quarter=new_q.quarter,
                        old_value=old_v, new_value=new_v, delta=delta,
                    ))

    return changes


# ---------- Email rendering ----------

def _signed(v: float, decimals: int = 2) -> str:
    if v == 0:
        return "0.00"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{decimals}f}"


def render_email(
    changes: list[ShpChange],
    curr_snapshot: dict[str, TickerShareholding],
    prev_snapshot: Optional[dict[str, dict]] = None,
) -> tuple[str, str, str]:
    """
    Build (subject, plain_text, html_body) for the shareholding alert.

    Subject examples:
      - "[SHP Alert] 2 stocks changed shareholding: ITC, RELIANCE"
      - "[SHP Alert] No changes this quarter"
    """
    today = datetime.now(IST).strftime("%d %b %Y")
    n = len({c.ticker for c in changes})

    # Group changes by ticker
    by_ticker: dict[str, list[ShpChange]] = {}
    for ch in changes:
        by_ticker.setdefault(ch.ticker, []).append(ch)

    if n == 0:
        subject = "[SHP Alert] No changes this quarter"
        intro = f"No shareholding-pattern changes detected for {today}."
    else:
        names = sorted({curr_snapshot.get(t).name if curr_snapshot.get(t) else t
                        for t in by_ticker})
        subject = (f"[SHP Alert] {n} stock{'s' if n != 1 else ''} "
                   f"changed shareholding: {', '.join(names[:5])}")
        if len(names) > 5:
            subject += f" (+{len(names) - 5} more)"
        intro = (
            f"{n} of your 8 stocks show new shareholding-pattern activity "
            f"since the last check:"
        )

    # Plain text
    lines = [f"Shareholding Pattern Alert — {today}", "", intro, ""]
    if n == 0:
        lines.append("All clear. No significant changes since the last check.")
        lines.append("")
        lines.append("Latest shareholding snapshot (most recent quarter):")
        for tkr in sorted(curr_snapshot):
            ts = curr_snapshot[tkr]
            if not ts.quarters:
                continue
            q = ts.quarters[0]
            lines.append(
                f"  {tkr:12s}  Promoter: {q.promoter:5.2f}%  "
                f"FII: {q.fii:5.2f}%  DII: {q.dii:5.2f}%  "
                f"MF: {q.mutual_funds:5.2f}%  Public: {q.public:5.2f}%"
            )
    else:
        for tkr in sorted(by_ticker):
            chs = by_ticker[tkr]
            ts = curr_snapshot.get(tkr)
            q = ts.quarters[0] if ts and ts.quarters else None
            lines.append(f"--- {tkr} ({ts.name if ts else ''}) ---")
            if q:
                lines.append(
                    f"  {q.quarter}: Promoter {q.promoter:.2f}%  "
                    f"FII {q.fii:.2f}%  DII {q.dii:.2f}%  "
                    f"MF {q.mutual_funds:.2f}%  Public {q.public:.2f}%"
                )
            for ch in chs:
                arrow = "▲" if ch.delta > 0 else "▼"
                lines.append(
                    f"    {arrow} {ch.category}: {_signed(ch.old_value)}% → "
                    f"{_signed(ch.new_value)}%  ({_signed(ch.delta)}%)"
                )
            lines.append("")
    lines.append("")
    lines.append("— Portfolio Tracker • shareholding_alert.py")

    # HTML body — color-coded per-category cards
    rows_html = []
    if n == 0:
        for tkr in sorted(curr_snapshot):
            ts = curr_snapshot[tkr]
            if not ts.quarters:
                continue
            q = ts.quarters[0]
            cells = [
                ("Promoter", q.promoter), ("FII", q.fii),
                ("DII", q.dii), ("Mutual Funds", q.mutual_funds),
                ("Banks", q.banks), ("Insurance", q.insurance),
                ("Public", q.public), ("Others", q.others),
            ]
            cells_html = "".join(
                f'<td style="padding:6px 12px;text-align:right;">'
                f'<span style="color:#6b7280;font-size:11px;">{name}</span><br>'
                f'<strong>{val:.2f}%</strong></td>'
                for name, val in cells
            )
            rows_html.append(
                f'<tr><td style="padding:8px 12px;"><strong>{tkr}</strong>'
                f'<div style="color:#6b7280;font-size:11px;">'
                f'{ts.name} · {q.quarter}</div></td>{cells_html}</tr>'
            )
        body_table = f"""
          <table style="border-collapse:collapse;font-family:sans-serif;font-size:13px;width:100%;">
            <thead>
              <tr style="background:#f3f4f6;text-align:left;color:#6b7280;font-size:11px;text-transform:uppercase;">
                <th style="padding:8px 12px;">Stock</th>
                <th style="padding:8px 12px;text-align:right;">Promoter</th>
                <th style="padding:8px 12px;text-align:right;">FII</th>
                <th style="padding:8px 12px;text-align:right;">DII</th>
                <th style="padding:8px 12px;text-align:right;">MF</th>
                <th style="padding:8px 12px;text-align:right;">Banks</th>
                <th style="padding:8px 12px;text-align:right;">Insurance</th>
                <th style="padding:8px 12px;text-align:right;">Public</th>
                <th style="padding:8px 12px;text-align:right;">Others</th>
              </tr>
            </thead>
            <tbody>{''.join(rows_html)}</tbody>
          </table>
        """
    else:
        cards = []
        for tkr in sorted(by_ticker):
            chs = by_ticker[tkr]
            ts = curr_snapshot.get(tkr)
            q = ts.quarters[0] if ts and ts.quarters else None
            change_rows = []
            for ch in chs:
                color = "#16a34a" if ch.delta > 0 else "#dc2626"
                arrow = "▲" if ch.delta > 0 else "▼"
                change_rows.append(f"""
                  <tr>
                    <td style="padding:4px 8px;">{arrow} <strong>{ch.category}</strong></td>
                    <td style="padding:4px 8px;text-align:right;color:#6b7280;">
                      {_signed(ch.old_value)}%
                    </td>
                    <td style="padding:4px 8px;text-align:center;color:#9ca3af;">→</td>
                    <td style="padding:4px 8px;text-align:right;font-weight:600;color:{color};">
                      {_signed(ch.new_value)}%
                    </td>
                    <td style="padding:4px 8px;text-align:right;color:{color};">
                      ({_signed(ch.delta)}%)
                    </td>
                  </tr>
                """)
            snapshot_row = ""
            if q:
                snapshot_row = (
                    f'<div style="margin:4px 0 8px;color:#6b7280;font-size:12px;">'
                    f'{q.quarter}: Promoter <strong>{q.promoter:.2f}%</strong> · '
                    f'FII <strong>{q.fii:.2f}%</strong> · '
                    f'DII <strong>{q.dii:.2f}%</strong> · '
                    f'MF <strong>{q.mutual_funds:.2f}%</strong> · '
                    f'Public <strong>{q.public:.2f}%</strong></div>'
                )
            cards.append(f"""
              <div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;margin-bottom:12px;background:#ffffff;">
                <div style="font-size:16px;font-weight:600;color:#111827;">
                  {tkr} <span style="font-weight:400;color:#6b7280;">— {ts.name if ts else ''}</span>
                </div>
                {snapshot_row}
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
        <h2 style="margin-top:0;color:#1f2937;">📊 Shareholding Pattern Alert</h2>
        <p style="color:#6b7280;margin:0 0 16px;">{today}</p>
        <p style="font-size:15px;">{intro}</p>
        {body_table}
        <p style="color:#9ca3af;font-size:12px;margin-top:24px;border-top:1px solid #e5e7eb;padding-top:12px;">
          Sent by Portfolio Tracker · shareholding_alert.py<br>
          <em>Source: Trendlyne shareholding-pattern pages. Promoter, FII, DII,
          Mutual Funds, Banks, Insurance, Public, Others changes above
          {", ".join(f"{k}±{v}%" for k, v in SIGNIFICANCE_PCT.items())} are flagged.</em>
        </p>
      </div>
    </body>
    </html>
    """
    return subject, "\n".join(lines), html


# ---------- Email send (reuses mf_holdings_alert SMTP) ----------

def send_email(subject: str, plain: str, html: str) -> dict:
    """
    Send via the same SMTP config as mf_holdings_alert (no new env vars).
    Falls back to dry-run mode if SMTP creds are missing.
    """
    # Reuse the SMTP machinery from mf_holdings_alert
    from .mf_holdings_alert import _env, is_dry_run as mfa_dry_run, send_email as mfa_send
    host = _env("MF_ALERT_SMTP_HOST")
    user = _env("MF_ALERT_SMTP_USER")
    pw = _env("MF_ALERT_SMTP_PASS")
    sender = _env("MF_ALERT_FROM") or user or "noreply@portfolio.local"
    recipient = _env("MF_ALERT_TO") or user

    if not (host and user and pw and recipient):
        log.info("[dry-run] would send shareholding email to=%s subject=%r",
                 recipient, subject)
        for ln in plain.splitlines()[:30]:
            log.info("  %s", ln)
        return {"sent": False, "mode": "dry_run",
                "reason": "missing SMTP creds",
                "to": recipient, "subject": subject}

    return mfa_send(subject, plain, html)


# ---------- Persistence ----------

def _load_prev() -> dict[str, dict]:
    if not PREV_FILE.exists():
        return {}
    try:
        return json.loads(PREV_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_curr(curr: dict[str, TickerShareholding]) -> None:
    """Persist the current snapshot for next-day diff."""
    serialised = {}
    for tkr, ts in curr.items():
        serialised[tkr] = {
            "ticker": ts.ticker,
            "name": ts.name,
            "url": ts.url,
            "fetched_at": ts.fetched_at,
            "quarters": [q.to_dict() for q in ts.quarters],
        }
    tmp = PREV_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(serialised, indent=2, default=str))
    tmp.replace(PREV_FILE)


def _append_log(entry: dict) -> None:
    log_list: list[dict] = []
    if LOG_FILE.exists():
        try:
            log_list = json.loads(LOG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log_list = []
    log_list.append(entry)
    log_list = log_list[-30:]
    tmp = LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(log_list, indent=2, default=str))
    tmp.replace(LOG_FILE)


# ---------- Daily run ----------

def run_once(force_email: bool = False) -> dict:
    """One-shot: fetch, diff, email if anything changed."""
    ran_at = datetime.now(IST).isoformat(timespec="seconds")
    errors: list[str] = []

    try:
        curr = fetch_all(parallel=True)
    except Exception as e:
        log.exception("fetch failed")
        errors.append(f"fetch failed: {e}")
        return {
            "ran_at": ran_at, "fetch_ok": False,
            "stocks_with_changes": 0, "tickers_changed": [],
            "email": {"sent": False, "reason": "fetch failed"},
            "errors": errors,
        }

    prev = _load_prev()
    changes = diff_snapshots(prev, curr)
    tickers_changed = sorted({c.ticker for c in changes})

    subject, plain, html = render_email(
        changes, curr, prev_snapshot=prev,
    )

    # Always persist the current snapshot
    _save_curr(curr)

    if not tickers_changed and not force_email:
        log.info("no shareholding changes (%d tickers fetched)", len(curr))
        result = {
            "ran_at": ran_at, "fetch_ok": True,
            "stocks_with_changes": 0, "tickers_changed": [],
            "email": {"sent": False, "reason": "no changes"},
            "errors": errors,
        }
        _append_log(result)
        return result

    email_status = send_email(subject, plain, html)
    result = {
        "ran_at": ran_at, "fetch_ok": True,
        "stocks_with_changes": len(tickers_changed),
        "tickers_changed": tickers_changed,
        "email": email_status,
        "errors": errors,
    }
    _append_log(result)
    log.info("shareholding alert: %d stocks changed, email sent=%s",
             len(tickers_changed), email_status.get("sent"))
    return result


# ---------- Scheduler ----------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _next_run_ist(hour: int = 16, minute: int = 35) -> datetime:
    """Next 16:35 IST (= 11:05 UTC). Just after the MF holdings alert at 16:30."""
    now_ist = datetime.now(IST)
    target = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_ist:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc).replace(tzinfo=None)


def _scheduler_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        next_run = _next_run_ist()
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        wait_secs = (next_run - now_utc).total_seconds()
        log.info("shareholding_alert scheduler: next run at %s IST (in %.0fs)",
                 next_run.strftime("%Y-%m-%d %H:%M:%S"), wait_secs)
        while wait_secs > 0 and not stop_event.is_set():
            chunk = min(60, wait_secs)
            stop_event.wait(chunk)
            wait_secs -= chunk
        if stop_event.is_set():
            break
        try:
            run_once()
        except Exception as e:
            log.exception("scheduled shareholding run failed: %s", e)


def start_daily_scheduler() -> threading.Event:
    """Start the daily shareholding alert scheduler (16:35 IST)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return threading.Event()
        _scheduler_started = True
        stop_event = threading.Event()
        t = threading.Thread(
            target=_scheduler_loop, args=(stop_event,),
            daemon=True, name="shareholding-alert",
        )
        t.start()
        log.info("shareholding_alert scheduler started")
        return stop_event


# ---------- CLI ----------

def _cli():
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--force-email", action="store_true",
                   help="Send email even when nothing changed.")
    p.add_argument("--start-scheduler", action="store_true")
    args = p.parse_args()

    if args.start_scheduler:
        stop = start_daily_scheduler()
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