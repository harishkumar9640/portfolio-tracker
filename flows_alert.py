"""
FII/DII flows + bulk/block deal alerts.

Two data sources, three alert types:

1. FII/DII flows  (NSE /api/fiidiiTradeReact)
   - Daily snapshot of FII and DII buy/sell/net value (₹ cr)
   - Alerts: any day with absolute flow > ₹5,000 cr either direction
     (large flows move the market; small flows are noise)

2. Bulk deals  (NSE archives/content/equities/bulk.csv)
   - Pre-negotiated large block trades filed with the exchange
   - Signals: institutional/PMS/FII positioning
   - Alerts: ANY bulk deal for a portfolio stock (rare + high signal)

3. Block deals  (NSE archives/content/equities/block.csv)
   - Scheduled window trades at fixed prices
   - Alerts: same as bulk deals

Data fetches:
  - FII/DII: NSE /api/fiidiiTradeReact (no session needed)
  - Bulk/Block: NSE archives CSV (no session needed)
  - All persisted locally for dashboard charting

Schedule: 6:45 PM IST daily (after FII/DII final data stabilises)
  - Provisional FII/DII: ~5:30 PM IST
  - Final FII/DII: ~8:30 PM IST
  - 6:45 PM gives us provisional numbers; we re-run at 8:55 PM via
    the scheduler with `force=False` to get the final numbers.

Usage:
    python flows_alert.py --run-once          # check + send
    python flows_alert.py --dry-run           # print, don't send
    python flows_alert.py --start-scheduler   # daemon mode
    python flows_alert.py --test-render       # sample Telegram output
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from http.cookiejar import CookieJar

import urllib.error
import urllib.request

# Re-use portfolio ticker info
from portfolio_impact import PORTFOLIO_EXPOSURE  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_FILE = PROJECT_ROOT / "flows_alert.log"
SEEN_FILE = PROJECT_ROOT / "flows_alert_seen.json"        # dedup
HISTORY_FILE = PROJECT_ROOT / "fii_dii_history.json"      # daily FII/DII archive
DEALS_HISTORY_FILE = PROJECT_ROOT / "bulk_block_history.json"  # daily bulk/block archive
LOG_FILE_HISTORY = PROJECT_ROOT / "flows_alert_log.json"  # alert send history

# Thresholds
FII_DII_LARGE_FLOW_CR = 5000.0   # alert if |net| > ₹5,000 cr

# Endpoints
NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_BULK_DEALS_URL = (
    "https://archives.nseindia.com/content/equities/bulk.csv"
)
NSE_BLOCK_DEALS_URL = (
    "https://archives.nseindia.com/content/equities/block.csv"
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/csv,*/*",
    "Accept-Language": "en-IN,en;q=0.9",
}
HTTP_TIMEOUT = 20

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("flows_alert")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FiiDiiRow:
    """One row of FII or DII daily activity."""
    category: str           # "FII/FPI" or "DII"
    date: str               # NSE format: "25-Jun-2026"
    buy_value_cr: float
    sell_value_cr: float
    net_value_cr: float


@dataclass
class DealRow:
    """One bulk or block deal."""
    deal_type: str          # "bulk" or "block"
    date: str               # NSE format
    symbol: str
    security_name: str
    client_name: str
    side: str               # "BUY" or "SELL"
    quantity: int
    price: float
    remarks: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def is_dry_run() -> bool:
    flag = _env("FLOWS_ALERT_DRY_RUN", "1")
    return flag not in ("0", "false", "False")


def _http_get(url: str, params: Optional[dict] = None,
              headers: Optional[dict] = None) -> str:
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    req = urllib.request.Request(url, headers={**HTTP_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _load_seen() -> dict[str, str]:
    if not SEEN_FILE.exists():
        return {}
    try:
        with SEEN_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("could not parse %s", SEEN_FILE)
        return {}


def _save_seen(seen: dict[str, str]) -> None:
    try:
        with SEEN_FILE.open("w", encoding="utf-8") as f:
            json.dump(seen, f, indent=2, sort_keys=True)
    except Exception:
        log.exception("could not write %s", SEEN_FILE)


def _append_history(entry: dict) -> None:
    try:
        with LOG_FILE_HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.exception("could not append to %s", LOG_FILE_HISTORY)


def _load_fii_dii_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("could not parse %s", HISTORY_FILE)
        return []


def _save_fii_dii_history(rows: list[dict]) -> None:
    try:
        with HISTORY_FILE.open("w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, sort_keys=True)
    except Exception:
        log.exception("could not write %s", HISTORY_FILE)


# ---------------------------------------------------------------------------
# FII/DII fetching
# ---------------------------------------------------------------------------

def fetch_fii_dii() -> list[FiiDiiRow]:
    """Fetch today's FII and DII activity from NSE.

    Returns a list with 2 entries (FII + DII). Empty list on failure.
    """
    try:
        body = _http_get(NSE_FII_DII_URL)
    except Exception as e:
        log.warning("FII/DII fetch failed: %s", e)
        return []

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("FII/DII returned non-JSON: head=%r", body[:80])
        return []

    out: list[FiiDiiRow] = []
    for row in data:
        try:
            out.append(FiiDiiRow(
                category=row["category"],
                date=row["date"],
                buy_value_cr=float(row["buyValue"]),
                sell_value_cr=float(row["sellValue"]),
                net_value_cr=float(row["netValue"]),
            ))
        except (KeyError, ValueError, TypeError) as e:
            log.debug("skipping malformed FII/DII row: %s (%s)", row, e)
    log.info("FII/DII fetch: %d rows", len(out))
    return out


def archive_fii_dii(rows: list[FiiDiiRow]) -> None:
    """Append today's FII/DII rows to the persistent history file.

    Dedup by (category, date) — same day + category overwrites the prior
    entry, so we naturally get the final number replacing the provisional.
    """
    history = _load_fii_dii_history()
    keys_existing = {(h["category"], h["date"]) for h in history}
    for r in rows:
        key = (r.category, r.date)
        if key in keys_existing:
            # Overwrite (provisional → final)
            for h in history:
                if (h["category"], h["date"]) == key:
                    h.update(asdict(r))
                    break
        else:
            history.append(asdict(r))
            keys_existing.add(key)
    _save_fii_dii_history(history)
    log.info("FII/DII history now: %d entries", len(history))


def _load_deals_history() -> list[dict]:
    if not DEALS_HISTORY_FILE.exists():
        return []
    try:
        with DEALS_HISTORY_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("could not parse %s", DEALS_HISTORY_FILE)
        return []


def _save_deals_history(rows: list[dict]) -> None:
    try:
        with DEALS_HISTORY_FILE.open("w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, sort_keys=True)
    except Exception:
        log.exception("could not write %s", DEALS_HISTORY_FILE)


def archive_deals(deals: list[DealRow]) -> None:
    """Append today's bulk/block deals to the persistent history file.

    Dedup by (deal_type, symbol, date, client, side) so the same deal
    isn't archived twice on re-runs.
    """
    history = _load_deals_history()
    keys_existing = {
        (h["deal_type"], h["symbol"], h["date"],
         h["client_name"], h["side"])
        for h in history
    }
    for d in deals:
        key = (d.deal_type, d.symbol, d.date, d.client_name, d.side)
        if key in keys_existing:
            continue
        history.append(asdict(d))
        keys_existing.add(key)
    _save_deals_history(history)
    log.info("deals history now: %d entries", len(history))


# ---------------------------------------------------------------------------
# Bulk / Block deals fetching
# ---------------------------------------------------------------------------

def _parse_deals_csv(body: str, deal_type: str) -> list[DealRow]:
    """Parse a bulk.csv or block.csv body into DealRow objects.

    The NSE archive format has slightly different schemas:
      bulk: Date, Symbol, Security Name, Client Name, Buy/Sell,
            Quantity Traded, Trade Price / Wght. Avg. Price, Remarks
      block: Date, Symbol, Security Name, Client Name, Buy/Sell,
             Quantity Traded, Trade Price / Wght. Avg. Price
    """
    reader = csv.DictReader(io.StringIO(body))
    out: list[DealRow] = []
    for r in reader:
        try:
            # Normalise keys (strip whitespace, lowercase for matching)
            row_lc = {k.strip().lower(): (v or "").strip()
                      for k, v in r.items() if k}
            out.append(DealRow(
                deal_type=deal_type,
                date=row_lc.get("date", ""),
                symbol=row_lc.get("symbol", "").upper(),
                security_name=row_lc.get("security name", ""),
                client_name=row_lc.get("client name", ""),
                side=row_lc.get("buy/sell", "").upper(),
                quantity=int(row_lc.get("quantity traded", "0")
                             .replace(",", "") or 0),
                price=float(row_lc.get(
                    "trade price / wght. avg. price",
                    row_lc.get("trade price", "0")
                ).replace(",", "") or 0),
                remarks=row_lc.get("remarks", ""),
            ))
        except (ValueError, KeyError) as e:
            log.debug("skipping malformed %s deal row: %s (%s)",
                      deal_type, r, e)
    return out


def fetch_bulk_deals() -> list[DealRow]:
    """Fetch today's bulk deals from NSE archives."""
    try:
        body = _http_get(NSE_BULK_DEALS_URL)
    except Exception as e:
        log.warning("bulk deals fetch failed: %s", e)
        return []
    rows = _parse_deals_csv(body, "bulk")
    log.info("bulk deals: %d rows", len(rows))
    return rows


def fetch_block_deals() -> list[DealRow]:
    """Fetch today's block deals from NSE archives."""
    try:
        body = _http_get(NSE_BLOCK_DEALS_URL)
    except Exception as e:
        log.warning("block deals fetch failed: %s", e)
        return []
    rows = _parse_deals_csv(body, "block")
    log.info("block deals: %d rows", len(rows))
    return rows


def filter_for_portfolio(deals: list[DealRow]) -> list[DealRow]:
    """Return only deals for stocks we hold."""
    portfolio_symbols = set(PORTFOLIO_EXPOSURE.keys())
    return [d for d in deals if d.symbol in portfolio_symbols]


# ---------------------------------------------------------------------------
# Alert rendering
# ---------------------------------------------------------------------------

def _fmt_cr(v: float) -> str:
    """Format a ₹cr value: positive = bullish, negative = bearish."""
    sign = "+" if v >= 0 else ""
    return f"{sign}₹{v:,.0f} cr"


def render_fii_dii_alert(
    rows: list[FiiDiiRow], threshold: float = FII_DII_LARGE_FLOW_CR,
) -> Optional[str]:
    """Render a Telegram alert if any FII/DII flow exceeds threshold.

    Returns None if no row exceeds the threshold (nothing to alert).
    """
    fii = next((r for r in rows if r.category == "FII/FPI"), None)
    dii = next((r for r in rows if r.category == "DII"), None)
    if not fii or not dii:
        return None

    fii_alert = abs(fii.net_value_cr) >= threshold
    dii_alert = abs(dii.net_value_cr) >= threshold

    if not (fii_alert or dii_alert):
        log.info("FII/DII flows within threshold (FII=%+.0f, DII=%+.0f, "
                 "threshold=%.0f) — no alert",
                 fii.net_value_cr, dii.net_value_cr, threshold)
        return None

    lines: list[str] = []
    lines.append("🌊 *FII / DII activity — large flows*")
    lines.append("")
    if fii_alert:
        direction = "BOUGHT" if fii.net_value_cr > 0 else "SOLD"
        verb = "🟢" if fii.net_value_cr > 0 else "🔴"
        lines.append(
            f"{verb} *FII/FPI* {direction} {_fmt_cr(fii.net_value_cr)} net "
            f"(buy ₹{fii.buy_value_cr:,.0f} cr / "
            f"sell ₹{fii.sell_value_cr:,.0f} cr)"
        )
    if dii_alert:
        direction = "BOUGHT" if dii.net_value_cr > 0 else "SOLD"
        verb = "🟢" if dii.net_value_cr > 0 else "🔴"
        lines.append(
            f"{verb} *DII* {direction} {_fmt_cr(dii.net_value_cr)} net "
            f"(buy ₹{dii.buy_value_cr:,.0f} cr / "
            f"sell ₹{dii.sell_value_cr:,.0f} cr)"
        )

    lines.append("")
    lines.append(
        f"📊 *What this means for your portfolio:*"
    )
    if fii_alert and fii.net_value_cr < 0:
        lines.append(
            "   • FII selling pressure is a headwind for large-caps "
            "(RELIANCE, ITC, BANKBARODA, JIOFIN) and any stock with "
            "high FII ownership."
        )
    if dii_alert and dii.net_value_cr > 0:
        lines.append(
            "   • DII buying is offsetting — typically supportive for "
            "mid/small caps (KNRCON, IRCON, BALRAMCHIN, NTPCGREEN) "
            "where DII ownership is rising."
        )
    lines.append(
        f"   • Threshold for alerts: ₹{threshold:,.0f} cr in either "
        f"direction"
    )
    lines.append(f"📅 {fii.date} \\#FII #DII #Flows")
    return "\n".join(lines)


def render_deal_alert(deal: DealRow) -> str:
    """Render a Telegram alert for a single bulk/block deal on a
    portfolio stock."""
    # Compute deal value (₹ cr)
    value_cr = (deal.quantity * deal.price) / 1e7
    deal_kind = deal.deal_type.upper()

    if deal.side == "BUY":
        emoji = "🟢"
        action = "bought"
    else:
        emoji = "🔴"
        action = "sold"

    # Find company name from portfolio
    info = PORTFOLIO_EXPOSURE.get(deal.symbol, {})
    company_name = info.get("name", deal.security_name or deal.symbol)

    lines: list[str] = []
    lines.append(f"{emoji} *{deal_kind} DEAL — {deal.symbol}*")
    lines.append("")
    lines.append(
        f"📊 *{company_name}* — {deal.client_name} "
        f"{action} *{deal.quantity:,} shares* at ₹{deal.price:,.2f}"
    )
    lines.append(f"💰 *Deal value:* ₹{value_cr:,.2f} cr")
    lines.append(f"📅 {deal.date}")
    lines.append("")
    lines.append(
        "📌 *What this means:*"
    )
    if deal.side == "BUY":
        lines.append(
            f"   • {deal.client_name} is accumulating {deal.symbol} — "
            "could signal a PMS/FII/HNI taking a meaningful position."
        )
        lines.append(
            "   • Watch for follow-on buying; bulk deals often precede "
            "further accumulation."
        )
    else:
        lines.append(
            f"   • {deal.client_name} is reducing its {deal.symbol} "
            "position — could be an exit, profit-booking, or rebalancing."
        )
        lines.append(
            "   • Selling pressure from a known holder often weighs "
            "on the stock for 1-3 weeks."
        )
    if deal.remarks:
        lines.append(f"   • Note: {deal.remarks}")
    lines.append(f"\\#{deal.symbol} #{deal_kind}Deal #SmartMoney")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> dict:
    if is_dry_run():
        log.info("[DRY-RUN] would send %d chars:\n%s", len(text), text)
        return {"sent": False, "mode": "dry_run", "chars": len(text)}

    bot_token = (_env("NEWS_TELEGRAM_BOT_TOKEN")
                 or _env("FLOWS_TELEGRAM_BOT_TOKEN"))
    chat_id = (_env("NEWS_TELEGRAM_CHAT_ID")
               or _env("FLOWS_TELEGRAM_CHAT_ID"))
    if not bot_token or not chat_id:
        log.warning("no Telegram credentials — skipping")
        return {"sent": False, "mode": "no_credentials", "chars": len(text)}

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            log.info("Telegram OK: %d chars", len(text))
            return {"sent": True, "mode": "telegram", "chars": len(text)}
    except Exception as e:
        log.exception("Telegram send failed")
        return {"sent": False, "mode": "error", "error": str(e),
                "chars": len(text)}


# ---------------------------------------------------------------------------
# Dedup keys
# ---------------------------------------------------------------------------

def _fii_dii_key(date_str: str) -> str:
    """FII/DII alerts fire once per day."""
    return f"fii_dii|{date_str}"


def _deal_key(deal: DealRow) -> str:
    return (f"{deal.deal_type}|{deal.symbol}|{deal.date}|"
            f"{deal.client_name}|{deal.side}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_once(today: Optional[datetime] = None,
             force_send: bool = False) -> dict:
    """One-shot: fetch flows + deals, render alerts, send.

    Always archives FII/DII (so the dashboard chart accumulates data
    even on no-alert days).
    """
    today = today or datetime.now(IST)
    date_str = today.strftime("%d-%b-%Y")

    sent_count = 0
    skipped_count = 0
    errors: list[str] = []

    # ---- 1. FII/DII ----
    fii_dii_rows = fetch_fii_dii()
    if fii_dii_rows:
        archive_fii_dii(fii_dii_rows)

    seen = _load_seen()
    fii_dii_key = _fii_dii_key(date_str)
    if not force_send and fii_dii_key in seen:
        log.info("FII/DII already alerted for %s — skipping", date_str)
        skipped_count += 1
    elif fii_dii_rows:
        alert_text = render_fii_dii_alert(fii_dii_rows)
        if alert_text:
            result = send_telegram(alert_text)
            seen[fii_dii_key] = today.strftime("%Y-%m-%d")
            sent_count += 1
            _append_history({
                "ts": today.isoformat(timespec="seconds"),
                "type": "fii_dii",
                "date": date_str,
                "sent": result.get("sent", False),
                "mode": result.get("mode"),
            })

    # ---- 2. Bulk deals ----
    try:
        bulk = fetch_bulk_deals()
        archive_deals(bulk)
        bulk = filter_for_portfolio(bulk)
    except Exception as e:
        log.exception("bulk deals pipeline failed")
        errors.append(f"bulk: {e}")
        bulk = []

    # ---- 3. Block deals ----
    try:
        block = fetch_block_deals()
        archive_deals(block)
        block = filter_for_portfolio(block)
    except Exception as e:
        log.exception("block deals pipeline failed")
        errors.append(f"block: {e}")
        block = []

    portfolio_deals = bulk + block
    log.info("portfolio-matched deals: %d bulk + %d block",
             len(bulk), len(block))

    for deal in portfolio_deals:
        key = _deal_key(deal)
        if not force_send and key in seen:
            skipped_count += 1
            continue
        text = render_deal_alert(deal)
        result = send_telegram(text)
        seen[key] = today.strftime("%Y-%m-%d")
        sent_count += 1
        _append_history({
            "ts": today.isoformat(timespec="seconds"),
            "type": f"{deal.deal_type}_deal",
            "ticker": deal.symbol,
            "client": deal.client_name,
            "side": deal.side,
            "quantity": deal.quantity,
            "price": deal.price,
            "sent": result.get("sent", False),
            "mode": result.get("mode"),
        })

    _save_seen(seen)
    return {
        "ran_at": today.isoformat(timespec="seconds"),
        "fii_dii_rows": len(fii_dii_rows),
        "bulk_deals": len(bulk),
        "block_deals": len(block),
        "sent": sent_count,
        "skipped": skipped_count,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Daemon scheduler
# ---------------------------------------------------------------------------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _next_run_ist(hour: int, minute: int) -> datetime:
    now = datetime.now(IST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _scheduler_loop(stop_event: threading.Event,
                    hour: int, minute: int) -> None:
    while not stop_event.is_set():
        target = _next_run_ist(hour, minute)
        wait_s = (target - datetime.now(IST)).total_seconds()
        log.info("flows_alert scheduler: next run at %02d:%02d IST "
                 "(in %.0fs)", hour, minute, wait_s)
        if stop_event.wait(timeout=wait_s):
            return
        try:
            run_once()
        except Exception as e:
            log.exception("scheduled run failed: %s", e)


def start_daily_scheduler(hour: int = 18, minute: int = 45) -> threading.Event:
    """Start background daemon. Default: 6:45 PM IST."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            log.info("flows_alert scheduler already running")
            return threading.Event()
        _scheduler_started = True
        stop_event = threading.Event()
        t = threading.Thread(
            target=_scheduler_loop, args=(stop_event, hour, minute),
            name="flows_alert_scheduler", daemon=True,
        )
        t.start()
        log.info("flows_alert scheduler started (runs at %02d:%02d IST)",
                 hour, minute)
        return stop_event


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="FII/DII + bulk/block deal alerts")
    p.add_argument("--run-once", action="store_true",
                   help="Run check and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="Force dry-run mode")
    p.add_argument("--start-scheduler", action="store_true",
                   help="Start background daemon (runs forever)")
    p.add_argument("--test-render", action="store_true",
                   help="Print sample alerts for all three types")
    p.add_argument("--force", action="store_true",
                   help="Re-send even if already in seen map")
    args = p.parse_args()

    if args.dry_run:
        os.environ["FLOWS_ALERT_DRY_RUN"] = "1"

    if args.test_render:
        # Show samples for each alert type
        sample_fii_dii = [
            FiiDiiRow(category="FII/FPI", date="25-Jun-2026",
                      buy_value_cr=18988.03, sell_value_cr=18604.27,
                      net_value_cr=383.76),
            FiiDiiRow(category="DII", date="25-Jun-2026",
                      buy_value_cr=24844.03, sell_value_cr=19096.28,
                      net_value_cr=5747.75),
        ]
        text = render_fii_dii_alert(sample_fii_dii)
        print("=== FII/DII ALERT (within threshold) ===")
        print(text or "(no alert — flows below threshold)")
        print()

        sample_fii_dii2 = [
            FiiDiiRow(category="FII/FPI", date="25-Jun-2026",
                      buy_value_cr=12000, sell_value_cr=22000,
                      net_value_cr=-10000),
            FiiDiiRow(category="DII", date="25-Jun-2026",
                      buy_value_cr=25000, sell_value_cr=18000,
                      net_value_cr=7000),
        ]
        text2 = render_fii_dii_alert(sample_fii_dii2)
        print("=== FII/DII ALERT (large flows) ===")
        print(text2)
        print()

        sample_deal = DealRow(
            deal_type="bulk", date="25-Jun-2026", symbol="ITC",
            security_name="ITC Limited",
            client_name="SBI MUTUAL FUND",
            side="BUY", quantity=2500000, price=425.50,
        )
        text3 = render_deal_alert(sample_deal)
        print("=== BULK DEAL ALERT (BUY) ===")
        print(text3)
        print()

        sample_deal2 = DealRow(
            deal_type="block", date="25-Jun-2026", symbol="RELIANCE",
            security_name="Reliance Industries Limited",
            client_name="NORTHERN TRUST",
            side="SELL", quantity=500000, price=2890.00,
        )
        text4 = render_deal_alert(sample_deal2)
        print("=== BLOCK DEAL ALERT (SELL) ===")
        print(text4)
        return

    if args.start_scheduler:
        stop = start_daily_scheduler()
        try:
            while not stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("interrupted")
            stop.set()
        return

    result = run_once(force_send=args.force)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()