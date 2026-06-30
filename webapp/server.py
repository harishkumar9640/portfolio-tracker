"""
webapp.server
-------------
FastAPI entry point. Run with:

    python3 -m webapp.server
    # or with explicit host/port:
    python3 -m webapp.server --host 0.0.0.0 --port 8000

Then open http://localhost:8000

Routes (all responsive HTML, no SPA / JS framework):
  GET  /                    -> redirect to /portfolio
  GET  /portfolio           -> today's portfolio + indices + holdings
  GET  /flows               -> FII/DII flows + bulk/block deals
  GET  /fairvalue           -> fair-value table (screener.in data)
  GET  /history             -> historical portfolio snapshots (Plotly embed)
  GET  /settings            -> manage tickers / mfs.json / sgbs.json
  GET  /api/health          -> JSON: last-run info, snapshot count
  GET  /api/portfolio       -> JSON: portfolio snapshot
  GET  /api/flows           -> JSON: FII/DII + deals
  GET  /api/fairvalue       -> JSON: fair-value rows
  GET  /api/refresh         -> trigger background refresh (returns 202)
  POST /api/refresh         -> same, but POST
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pipeline.logging_setup import get_logger
from webapp import TEMPLATES_DIR, STATIC_DIR
from pathlib import Path
PROJECT_CHARTS_DIR = Path(__file__).resolve().parent.parent / "data" / "charts"
from webapp.data import (
    get_portfolio_snapshot,
    get_fairvalue_snapshot,
    get_mf_holdings_snapshot,
    get_health,
    get_holdings_summary,
    get_flows_snapshot,
    get_concalls_snapshot,
    start_background_refresh,
)
from webapp.tax_dashboard import router as tax_router

log = get_logger("webapp")

app = FastAPI(
    title="Portfolio Tracker",
    description="Personal finance dashboard: equity + MF + SGB vs world indices, plus fair-value analysis.",
    version="0.2.0",
)
app.include_router(tax_router)

# Templates and static
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# Serve generated charts (PNG + HTML overlays) under /charts so the
# history iframe can embed them. The chart files are written by
# portfolio_html.py and equity_compare.py into PROJECT_CHARTS_DIR.
app.mount("/charts", StaticFiles(directory=str(PROJECT_CHARTS_DIR)), name="charts")


def _ctx(**extra) -> dict:
    """Default context for every rendered template. ``request`` is
    auto-injected by Starlette's Jinja2Templates when context contains it."""
    base = {
        "active_nav": None,
        "page_title": "Portfolio Tracker",
    }
    base.update(extra)
    return base


# ---------- Shareholding helper for portfolio page ----------
# We don't want /portfolio to make Trendlyne network calls (it's already
# slow enough on cold cache). Instead we read from the persisted
# data/shareholding_prev.json — which is updated daily by the shareholding
# alert pipeline. If the file is missing or empty, we return an empty dict
# and the UI shows a "No data" placeholder.
def _get_shareholding_for_portfolio() -> dict:
    from pipeline.shareholding_alert import PREV_FILE
    if not PREV_FILE.exists():
        return {"asof": None, "tickers": {}, "row_count": 0}
    try:
        data = json.loads(PREV_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"asof": None, "tickers": {}, "row_count": 0}
    out = {}
    latest_quarter = None
    for tkr, payload in data.items():
        quarters = payload.get("quarters") or []
        if not quarters:
            continue
        latest = quarters[0]
        if not latest_quarter or latest.get("quarter") > latest_quarter:
            latest_quarter = latest.get("quarter")
        out[tkr] = {
            "name": payload.get("name", tkr),
            "url": payload.get("url"),
            "quarter": latest.get("quarter"),
            "promoter": latest.get("promoter", 0.0),
            "promoter_pledged": latest.get("promoter_pledged", 0.0),
            "fii": latest.get("fii", 0.0),
            "dii": latest.get("dii", 0.0),
            "mutual_funds": latest.get("mutual_funds", 0.0),
            "banks": latest.get("banks", 0.0),
            "insurance": latest.get("insurance", 0.0),
            "public": latest.get("public", 0.0),
            "others": latest.get("others", 0.0),
        }
    return {"asof": latest_quarter, "tickers": out, "row_count": len(out)}


# ---------- Pages ----------
@app.get("/", response_class=RedirectResponse)
def root() -> str:
    return "/portfolio"


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request) -> HTMLResponse:
    """Render the portfolio page. The snapshot is fetched synchronously
    here (cached for 60s in webapp.data). On a cold cache the first
    request takes ~5s while the build runs; the browser shows a
    stopwatch loader via #page-loading during that time.
    """
    snap = get_portfolio_snapshot()
    mf_holdings = get_mf_holdings_snapshot()
    shareholding = _get_shareholding_for_portfolio()
    return templates.TemplateResponse(
        request,
        "portfolio.html",
        _ctx(
            active_nav="portfolio",
            page_title="Portfolio — Today",
            snapshot=snap,
            mf_holdings=mf_holdings,
            shareholding=shareholding,
        ),
    )


@app.get("/flows", response_class=HTMLResponse)
def flows_page(request: Request) -> HTMLResponse:
    """Render the FII/DII + bulk/block-deals page. Reads from the local
    history files written by flows_alert.run_once(); no network calls."""
    snap = get_flows_snapshot()
    return templates.TemplateResponse(
        request,
        "flows.html",
        _ctx(
            active_nav="flows",
            page_title="FII / DII & Smart Money",
            snapshot=snap,
        ),
    )


@app.get("/concalls", response_class=HTMLResponse)
def concalls_page(request: Request, ticker: Optional[str] = None
                  ) -> HTMLResponse:
    """Render the con-call summaries page from local cache; no network.

    Optional ?ticker= filter (e.g. ?ticker=ITC) restricts to one stock.
    """
    snap = get_concalls_snapshot(filter_ticker=ticker)
    return templates.TemplateResponse(
        request,
        "concalls.html",
        _ctx(
            active_nav="concalls",
            page_title="Con-call summaries",
            snapshot=snap,
        ),
    )


@app.get("/fairvalue", response_class=HTMLResponse)
def fairvalue_page(request: Request) -> HTMLResponse:
    snap = get_fairvalue_snapshot()
    return templates.TemplateResponse(
        request,
        "fairvalue.html",
        _ctx(
            active_nav="fairvalue",
            page_title="Fair Value",
            snapshot=snap,
        ),
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> HTMLResponse:
    """Embed the existing Plotly HTML chart (or show a placeholder)."""
    chart_path = PROJECT_CHARTS_DIR / "portfolio_compare_3mo.html"
    chart_exists = chart_path.exists()
    return templates.TemplateResponse(
        request,
        "history.html",
        _ctx(
            active_nav="history",
            page_title="Portfolio History",
            chart_url=f"/charts/portfolio_compare_3mo.html" if chart_exists else None,
            chart_exists=chart_exists,
        ),
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            active_nav="settings",
            page_title="Settings",
            holdings=get_holdings_summary(),
        ),
    )


# ---------- JSON API ----------
@app.get("/api/health")
def api_health() -> dict:
    return get_health()


@app.get("/api/portfolio")
def api_portfolio() -> dict:
    return get_portfolio_snapshot()


@app.get("/api/flows")
def api_flows() -> dict:
    """JSON: FII/DII history + bulk/block deals for charting/automation."""
    return get_flows_snapshot()


@app.get("/api/concalls")
def api_concalls(ticker: Optional[str] = None) -> dict:
    """JSON: cached con-call summaries."""
    return get_concalls_snapshot(filter_ticker=ticker)


@app.post("/api/concalls/run")
def api_concalls_run() -> dict:
    """Trigger an on-demand con-call scan (runs in background thread)."""
    def _worker():
        try:
            import pipeline.concalls as concalls
            concalls.run_once(days_back=7, force_send=False)
        except Exception as e:
            log.error("concalls on-demand run failed: %s", e)
    t = threading.Thread(target=_worker, daemon=True,
                         name="concalls-ondemand")
    t.start()
    return {"status": "queued", "kind": "concalls"}


@app.get("/api/intraday")
def api_intraday(interval: str = "5m") -> dict:
    """
    JSON snapshot of today's intraday data for the user's full portfolio
    (equity + MF + SGB) vs the 8 world indices.

    interval: one of '1m', '5m', '15m'. Default '5m'.
    """
    if interval not in ("1m", "5m", "15m"):
        return {"error": f"unsupported interval {interval!r}; use 1m, 5m, or 15m"}
    try:
        from pipeline.intraday import build_intraday_snapshot
        return build_intraday_snapshot(interval)  # type: ignore[arg-type]
    except Exception as e:
        log.exception("intraday snapshot failed for %s", interval)
        return {"error": str(e), "interval": interval}


@app.get("/api/fairvalue")
def api_fairvalue() -> dict:
    return get_fairvalue_snapshot()


@app.get("/api/mf_holdings")
def api_mf_holdings() -> dict:
    """
    Monthly mutual fund holdings trend for the user's 8 equity tickers.

    Returns:
        {
            "asof": "2026-06-26",
            "row_count": 8,
            "rows": [
                {
                    "ticker": "ITC",
                    "name": "ITC",
                    "asof": "May 2026",
                    "total_mfs_holding": 458,
                    "mfs_bought": 167,
                    "mfs_sold": 120,
                    "net_change_shares": -13181340,
                    "net_change_label": "-13,181,340",
                    "total_shares_held": 2085365191,
                    "top_buyer": {...},
                    "top_seller": {...},
                    "top_buyers": [...],
                    "top_sellers": [...],
                    "url": "https://trendlyne.com/...",
                    "fetched_at": "2026-06-26T09:00:00"
                },
                ...
            ]
        }

    The data is sorted by |net_change| descending (biggest movers first).
    """
    return get_mf_holdings_snapshot()


@app.post("/api/mf_alert/run")
def api_mf_alert_run(payload: Optional[dict] = None) -> dict:
    """
    Manually trigger one MF-holdings alert check. Same logic the daily
    scheduler runs at 16:30 IST, but on demand. Fetches a fresh snapshot,
    diffs against the persisted "previous" snapshot, and emails if
    anything changed.

    Body (JSON, optional):
        {
            "force_email": true    # send email even if no changes
        }

    Returns:
        {
            "ran_at":              ISO timestamp,
            "snapshot_ok":         bool,
            "stocks_with_changes": int,
            "tickers_changed":     [...],
            "email":               {sent, mode, ...}
        }
    """
    from pipeline.mf_holdings_alert import run_once
    force_email = bool((payload or {}).get("force_email", False))
    return run_once(force_email=force_email)


@app.get("/api/mf_alert/log")
def api_mf_alert_log() -> dict:
    """
    Return the last 30 runs of the MF alert (most recent first).
    Useful for the user to verify the scheduler is working.
    """
    from pathlib import Path
    from pipeline.mf_holdings_alert import ALERT_LOG_FILE
    if not ALERT_LOG_FILE.exists():
        return {"runs": []}
    try:
        runs = json.loads(ALERT_LOG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        runs = []
    # Most recent first
    return {"runs": list(reversed(runs[-30:]))}


@app.post("/api/news/run")
def api_news_run(payload: Optional[dict] = None) -> dict:
    """
    Manually trigger one news-digest run. Same logic the daily
    scheduler runs at 8:55 AM IST, but on demand.

    Body (JSON, optional):
        {
            "force_send": true    # send even if no significant news
        }
    """
    from pipeline.news_alert import run_once as news_run_once
    force_send = bool((payload or {}).get("force_send", False))
    return news_run_once(force_send=force_send)


@app.get("/api/news/log")
def api_news_log() -> dict:
    """Return the last 30 news-digest runs (most recent first)."""
    from pipeline.news_alert import LOG_FILE
    if not LOG_FILE.exists():
        return {"runs": []}
    try:
        runs = json.loads(LOG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        runs = []
    return {"runs": list(reversed(runs[-30:]))}


@app.post("/api/shareholding/run")
def api_shareholding_run(payload: Optional[dict] = None) -> dict:
    """
    Manually trigger one shareholding-pattern alert check.

    Body (JSON, optional):
        {
            "force_email": true    # send email even if no changes
        }
    """
    from pipeline.shareholding_alert import run_once as shp_run_once
    force_email = bool((payload or {}).get("force_email", False))
    return shp_run_once(force_email=force_email)


@app.get("/api/shareholding/log")
def api_shareholding_log() -> dict:
    """Return the last 30 shareholding-alert runs (most recent first)."""
    from pipeline.shareholding_alert import LOG_FILE
    if not LOG_FILE.exists():
        return {"runs": []}
    try:
        runs = json.loads(LOG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        runs = []
    return {"runs": list(reversed(runs[-30:]))}


@app.get("/api/shareholding/snapshot")
def api_shareholding_snapshot() -> dict:
    """
    Fetch the latest shareholding data for all 8 tickers without
    persisting or comparing. Useful for the UI / on-demand display.
    """
    from pipeline.shareholding_alert import fetch_all
    try:
        snap = fetch_all(parallel=True)
    except Exception as e:
        return {"error": f"fetch failed: {e}", "tickers": {}}
    # Serialise
    out = {}
    for tkr, ts in snap.items():
        out[tkr] = {
            "ticker": ts.ticker,
            "name": ts.name,
            "url": ts.url,
            "fetched_at": ts.fetched_at,
            "quarters": [q.to_dict() for q in ts.quarters],
        }
    return {"tickers": out}


@app.post("/api/portfolio_impact/scan")
def api_portfolio_impact_scan(payload: Optional[dict] = None) -> dict:
    """
    Manually trigger one portfolio-impact scan. Fetches latest news,
    cross-references against your 8 holdings, and sends Telegram alerts
    for any story that affects one of your stocks.

    Body (JSON, optional):
        {
            "dry_run": true    # log the alerts instead of sending
        }
    """
    from pipeline.portfolio_impact import scan_once
    dry_run = bool((payload or {}).get("dry_run", False))
    return scan_once(send=not dry_run)


@app.get("/api/portfolio_impact/log")
def api_portfolio_impact_log() -> dict:
    """Return the last 50 portfolio-impact alerts (most recent first)."""
    from pipeline.portfolio_impact import IMPACT_LOG_FILE
    if not IMPACT_LOG_FILE.exists():
        return {"alerts": []}
    try:
        log_data = json.loads(IMPACT_LOG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        log_data = []
    return {"alerts": list(reversed(log_data[-50:]))}


@app.get("/api/portfolio_impact/exposure")
def api_portfolio_impact_exposure() -> dict:
    """
    Return the portfolio exposure map (ticker → sectors + risk drivers).
    Useful for the UI / debugging.
    """
    from pipeline.portfolio_impact import PORTFOLIO_EXPOSURE
    return {"exposure": PORTFOLIO_EXPOSURE}


@app.get("/api/news/preview")
def api_news_preview() -> dict:
    """
    Build a Telegram-style preview of today's digest WITHOUT sending.
    Useful for the user to verify the categories are working before
    relying on the daily cron.
    """
    from pipeline.news_alert import (
        fetch_articles, _filter_fresh, _categorise_and_dedup,
        _load_seen, _save_seen, render_telegram,
    )
    from datetime import datetime, timezone, timedelta
    try:
        articles = fetch_articles()
    except Exception as e:
        return {"error": f"fetch failed: {e}"}
    fresh = _filter_fresh(articles)
    seen = _load_seen()
    buckets = _categorise_and_dedup(fresh, seen)
    # Don't persist seen cache for previews (we haven't actually sent)
    _save_seen(_load_seen())  # no-op write to be safe
    msg = render_telegram(buckets, date_str=datetime.now().strftime("%d %b %Y"))
    return {
        "articles_total": len(articles),
        "articles_fresh": len(fresh),
        "categories": {c: len(buckets[c]) for c in buckets},
        "message_length": len(msg) if msg else 0,
        "message_preview": msg or "(no significant news today)",
    }


@app.get("/api/fairvalue/search")
def api_fairvalue_search(q: str = "", limit: int = 10) -> dict:
    """
    Autocomplete endpoint. Matches the user's query against the in-memory
    mfapi.in master scheme list by ticker prefix or name substring.
    Returns up to ``limit`` matches, sorted by relevance.

    Empty ``q`` returns the most-popular schemes (useful for the default
    dropdown state).
    """
    from pipeline.fair_value.search import search_schemes
    return {"query": q, "results": search_schemes(q, limit=limit)}


@app.post("/api/fairvalue/lookup")
def api_fairvalue_lookup(payload: dict) -> dict:
    """
    Compute the fair value for a single ticker on demand.

    Body (JSON):
        {
            "ticker":      "RELIANCE"  (required),
            "industry_pe": 25,         (optional, enables PE-relative),
            "dcf_g1":      0.10,       (optional, default 10%),
            "dcf_g2":      0.03,       (optional, default 3%),
            "dcf_r":       0.10        (optional, default 10%)
        }

    Returns the full breakdown including the underlying fundamentals.
    """
    from pipeline.fair_value.valuation import (
        check,
    )
    from pipeline.fair_value.search import resolve_ticker

    raw_ticker = (payload.get("ticker") or "").strip()
    if not raw_ticker:
        return {"error": "ticker is required"}

    # Resolve via screener.in master list if the user typed a name / ISIN
    resolved_ticker, resolved_name = resolve_ticker(raw_ticker)

    industry_pe = payload.get("industry_pe")
    dcf_g1 = float(payload.get("dcf_g1", 0.10))
    dcf_g2 = float(payload.get("dcf_g2", 0.03))
    dcf_r = float(payload.get("dcf_r", 0.10))

    rows = check(
        [resolved_ticker],
        industry_pe=industry_pe,
        dcf_g1=dcf_g1, dcf_g2=dcf_g2, dcf_r=dcf_r,
    )
    if not rows:
        return {"error": f"could not value {raw_ticker!r}"}

    r = rows[0].to_dict()
    if r.get("error"):
        return {"error": r["error"], "ticker": resolved_ticker,
                "queried_as": raw_ticker}

    # Add margin metrics for the UI
    if r.get("price") and r.get("graham"):
        r["graham_margin_pct"] = round(
            (r["graham"] - r["price"]) / r["price"] * 100, 2
        )
    if r.get("price") and r.get("dcf"):
        r["dcf_margin_pct"] = round(
            (r["dcf"] - r["price"]) / r["price"] * 100, 2
        )
    if r.get("price") and r.get("pe_relative"):
        r["pe_margin_pct"] = round(
            (r["pe_relative"] - r["price"]) / r["price"] * 100, 2
        )

    r["queried_as"] = raw_ticker
    r["resolved_ticker"] = resolved_ticker
    r["resolved_name"] = resolved_name
    r["params"] = {
        "industry_pe": industry_pe,
        "dcf_g1": dcf_g1, "dcf_g2": dcf_g2, "dcf_r": dcf_r,
    }
    return r


@app.get("/api/refresh/status")
def api_refresh_status() -> dict:
    """Return current cache state for all snapshots.
    Used by dashboard.js to detect when a manual refresh has completed."""
    from webapp.data import (
        _portfolio_cache, _fairvalue_cache, _mf_holdings_cache,
        _portfolio_in_progress, _portfolio_in_progress_ts,
        _is_market_open, _current_cache_ttl,
    )
    import time
    now = time.time()
    return {
        "market_open": _is_market_open(),
        "cache_ttl_sec": _current_cache_ttl(),
        "now": now,
        "portfolio": {
            "in_progress": _portfolio_in_progress,
            "in_progress_for_sec": (
                now - _portfolio_in_progress_ts
                if _portfolio_in_progress else 0
            ),
            "cache_age_sec": (
                now - _portfolio_cache["ts"]
                if _portfolio_cache["ts"] else None
            ),
            # Unix timestamp of last successful rebuild — the JS uses
            # this to detect completion (asof date string is too coarse
            # when both old and new builds land on the same trading day)
            "cache_ts": _portfolio_cache["ts"] or None,
            "asof": _portfolio_cache.get("asof"),
        },
        "fairvalue": {
            "cache_age_sec": (
                now - _fairvalue_cache["ts"]
                if _fairvalue_cache["ts"] else None
            ),
        },
        "mf_holdings": {
            "cache_age_sec": (
                now - _mf_holdings_cache["ts"]
                if _mf_holdings_cache["ts"] else None
            ),
        },
    }


@app.get("/api/refresh")
@app.post("/api/refresh")
def api_refresh(kind: str = "all") -> JSONResponse:
    """Trigger a background re-fetch. Returns 202 immediately.

    For 'portfolio', the rebuild is synchronous and the response
    includes a 'status' URL to poll for completion.
    """
    valid_kinds = ("portfolio", "fairvalue", "mf_holdings",
                   "flows", "concalls")

    if kind in ("portfolio", "all"):
        # Run portfolio rebuild in a background thread so we return
        # 202 immediately, but the rebuild is properly serialized
        # via the lock in webapp.data
        start_background_refresh("portfolio")
    if kind in ("fairvalue", "all"):
        start_background_refresh("fairvalue")
    if kind in ("mf_holdings", "all"):
        start_background_refresh("mf_holdings")
    if kind in ("flows", "all"):
        start_background_refresh("flows")
    if kind in ("concalls", "all"):
        start_background_refresh("concalls")

    return JSONResponse(
        {"status": "queued",
         "kinds": [k for k in valid_kinds if kind in (k, "all")],
         "poll_url": "/api/refresh/status"},
        status_code=202,
    )


# ---------- CLI ----------


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(description="Run the Portfolio Tracker web dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload on source changes (dev mode).")
    args = parser.parse_args()

    # Pre-warm the portfolio cache in the background so the first
    # page load after server start is fast. We can't block uvicorn.run
    # for ~5 seconds (the build takes that long), but the cache will
    # be ready by the time the user actually opens their browser.
    import threading
    def _warm():
        try:
            from webapp.data import (
                get_portfolio_snapshot,
                get_fairvalue_snapshot,
                get_mf_holdings_snapshot,
            )
            get_portfolio_snapshot(force=True)
            log.info("portfolio cache pre-warmed")
            get_fairvalue_snapshot(force=True)
            log.info("fairvalue cache pre-warmed")
            get_mf_holdings_snapshot(force=True)
            log.info("mf_holdings cache pre-warmed")
        except Exception as e:
            log.warning("cache pre-warm failed (non-fatal): %s", e)
    threading.Thread(target=_warm, daemon=True, name="cache-warmer").start()

    # Start the daily MF-holdings alert scheduler (16:30 IST). The
    # scheduler is opt-out via MF_ALERT_DISABLED=1; it never blocks
    # the server start (daemon thread). On missing SMTP credentials
    # it runs in dry-run mode (logs emails instead of sending).
    if not os.environ.get("MF_ALERT_DISABLED"):
        try:
            from pipeline.mf_holdings_alert import start_daily_scheduler
            start_daily_scheduler()
        except Exception as e:
            log.warning("could not start mf_holdings_alert scheduler: %s", e)

    # Start the daily global-news alert scheduler (8:55 AM IST).
    # Same opt-out pattern. On missing Telegram bot creds the alert
    # runs in dry-run mode (logs the message body instead of sending).
    if not os.environ.get("NEWS_DISABLED"):
        try:
            from pipeline.news_alert import start_daily_scheduler as start_news_scheduler
            start_news_scheduler()
        except Exception as e:
            log.warning("could not start news_alert scheduler: %s", e)

    # Start the daily shareholding-pattern alert scheduler (16:35 IST).
    # Reuses the same SMTP creds as mf_holdings_alert.
    if not os.environ.get("SHP_ALERT_DISABLED"):
        try:
            from pipeline.shareholding_alert import start_daily_scheduler as start_shp_scheduler
            start_shp_scheduler()
        except Exception as e:
            log.warning("could not start shareholding_alert scheduler: %s", e)

    # Start the portfolio-impact scanner (every 30 min during market hours).
    # Cross-references news against your 8 holdings and sends Telegram
    # alerts when a story affects one of your stocks.
    if not os.environ.get("PORTFOLIO_IMPACT_DISABLED"):
        try:
            from pipeline.portfolio_impact import start_daily_scheduler as start_impact_scheduler
            start_impact_scheduler(interval_minutes=30)
        except Exception as e:
            log.warning("could not start portfolio_impact scheduler: %s", e)

    log.info("starting Portfolio Tracker on http://%s:%d", args.host, args.port)
    uvicorn.run(
        "webapp.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()