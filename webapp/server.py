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
  GET  /fairvalue           -> fair-value table (screener.in data)
  GET  /history             -> historical portfolio snapshots (Plotly embed)
  GET  /settings            -> manage tickers / mfs.json / sgbs.json
  GET  /api/health          -> JSON: last-run info, snapshot count
  GET  /api/portfolio       -> JSON: portfolio snapshot
  GET  /api/fairvalue       -> JSON: fair-value rows
  GET  /api/refresh         -> trigger background refresh (returns 202)
  POST /api/refresh         -> same, but POST
"""
from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from logging_setup import get_logger
from webapp import TEMPLATES_DIR, STATIC_DIR
from webapp.data import (
    get_portfolio_snapshot,
    get_fairvalue_snapshot,
    get_health,
    get_holdings_summary,
    start_background_refresh,
)

log = get_logger("webapp")

app = FastAPI(
    title="Portfolio Tracker",
    description="Personal finance dashboard: equity + MF + SGB vs world indices, plus fair-value analysis.",
    version="0.2.0",
)

# Templates and static
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _ctx(**extra) -> dict:
    """Default context for every rendered template. ``request`` is
    auto-injected by Starlette's Jinja2Templates when context contains it."""
    base = {
        "active_nav": None,
        "page_title": "Portfolio Tracker",
    }
    base.update(extra)
    return base


# ---------- Pages ----------
@app.get("/", response_class=RedirectResponse)
def root() -> str:
    return "/portfolio"


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request) -> HTMLResponse:
    snap = get_portfolio_snapshot()
    return templates.TemplateResponse(
        request,
        "portfolio.html",
        _ctx(
            active_nav="portfolio",
            page_title="Portfolio — Today",
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
            chart_url=f"/static/../charts/portfolio_compare_3mo.html" if chart_exists else None,
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


@app.get("/api/fairvalue")
def api_fairvalue() -> dict:
    return get_fairvalue_snapshot()


@app.get("/api/refresh")
@app.post("/api/refresh")
def api_refresh(kind: str = "all") -> JSONResponse:
    """Trigger a background re-fetch. Returns 202 immediately."""
    if kind in ("portfolio", "all"):
        start_background_refresh("portfolio")
    if kind in ("fairvalue", "all"):
        start_background_refresh("fairvalue")
    return JSONResponse(
        {"status": "queued", "kinds": [k for k in ("portfolio", "fairvalue")
                                       if kind in (k, "all")]},
        status_code=202,
    )


# ---------- CLI ----------
PROJECT_CHARTS_DIR = Path(__file__).resolve().parent.parent / "charts"


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(description="Run the Portfolio Tracker web dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload on source changes (dev mode).")
    args = parser.parse_args()

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