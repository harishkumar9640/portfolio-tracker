# Portfolio Tracker

A personal finance dashboard for Indian investors. It pulls your **equity
holdings** from Angel One, **mutual fund** NAVs from mfapi.in, **Sovereign
Gold Bond (SGB)** prices from public sources, plus the day's **FII/DII
flows** and **bulk/block deals** from NSE — then plots everything
side-by-side with 8 world stock-market indices so you can see, at a glance,
whether you beat the market today.

It also runs a small fleet of background alerts: **earnings calendar**,
**con-call summaries**, **FII/DII flows**, **MF holdings changes**,
**shareholding-pattern shifts**, **news digest** (Telegram), and
**portfolio-impact news scanner** — all scheduled by IST time of day.

The whole thing ships as a single FastAPI webapp, a folder of CLI
pipeline scripts, and a SQLite database for time-series data.

In addition to the dashboard, the project has three
**ad-hoc CLI modules** for one-off analysis:
- **Cohorts** — group holdings by market-cap tier and compare CAGR
  vs Nifty 50 / Nifty Next 50 / Nifty Midcap 150 / Nifty Smallcap 250
- **XIRR** — money-weighted return on your capital (more accurate than
  CAGR for uneven buy timing)
- **Tax P&L upload** — analyse anyone else's Tax P&L file
  (Angel One xlsx, Zerodha CSV, or any tabular file with a
  user-supplied column mapping). Ephemeral, 24h TTL.

---

## Table of contents

1. [What you get](#what-you-get)
2. [Quick start](#quick-start)
3. [Daily usage](#daily-usage)
4. [The pages](#the-pages)
5. [Project layout](#project-layout)
6. [Configuration](#configuration)
7. [Running automatically every day](#running-automatically-every-day)
8. [Tax & P&L workflow](#tax--pl-workflow)
9. [Cohorts, XIRR & ad-hoc analysis](#cohorts-xirr--ad-hoc-analysis)
10. [Tests](#tests)
11. [Limitations & known issues](#limitations--known-issues)
12. [Security — what's safe to publish](#security--whats-safe-to-publish)
13. [Troubleshooting](#troubleshooting)

---

## What you get

| Surface | What it does |
|---|---|
| **Web dashboard** | 9 pages: Dashboard, Portfolio, Flows, Con-calls, Tax & P&L, Fair Value, History, CAGR, Settings. Mobile-friendly, dark-mode aware, real-time data with manual refresh. |
| **Background alerts** | News digest (Telegram at 8:55 AM), earnings/board meetings, con-call summaries, FII/DII flows + bulk/block deals, MF-holdings changes, shareholding-pattern changes, portfolio-impact news. |
| **Tax P&L parser** | Reads your Angel One "Tax PNL" xlsx files and renders a multi-year P&L dashboard with realised + unrealised gains, charges, and a per-trade verdict. Also accepts ephemeral uploads of anyone else's Tax P&L (Angel One, Zerodha, or any tabular file with column mapping). |
| **Fair-value checker** | Graham number + DCF + PE-relative value for any NSE/BSE stock, with screener.in fundamentals. |
| **Cohorts / XIRR** | Per-tier CAGR analysis, Nifty-benchmark alpha, money-weighted XIRR. CLI + web. |
| **Gold:Silver ratio** | Live oz/oz ratio with rotation signal (BUY SILVER / HOLD / TAKE PROFIT) on the Portfolio page. |
| **Portfolio monitor** | Weekly concentration + 100-day review for the BALRAMCHIN/KNRCON/UNOMINDA midcap bets. |
| **Project truth** | Single source of truth for all positions (equity + MF + SGB + watchlist) shared across modules. |

---

## Quick start

> Tested on macOS 14+ with Python 3.11+. Linux and Windows should work but
> are not exercised regularly. The macOS-specific bits are the FII/DII
> fetcher's HTTPS handling and the Telegram Bot scheduler.

### 1. Clone and enter

```bash
git clone <your-fork-url> portfolio-tracker
cd portfolio-tracker
```

### 2. Set up Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure your Angel One credentials

```bash
cp .env.example .env
$EDITOR .env
```

You need:
- `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET`
  from <https://smartapi.angelbroking.com/>
- (optional) `NEWS_TELEGRAM_BOT_TOKEN` + `NEWS_TELEGRAM_CHAT_ID` for the
  daily 8:55 AM news digest (create a bot via [@BotFather](https://t.me/BotFather))
- (optional) `MF_ALERT_SMTP_*` for the MF-holdings + shareholding email
  alerts
- (optional) `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` for the
  portfolio-impact Telegram alerts

> **Never commit `.env` to git** — it's already in `.gitignore`.

### 4. List your mutual funds and SGBs

```bash
cp mfs.json.example mfs.json   # then edit
cp sgbs.json.example sgbs.json # then edit
```

### 5. Run the web dashboard

```bash
python -m webapp.server
# open http://127.0.0.1:8000  (redirects to /portfolio)
```

The first page load takes 5–10 seconds because the portfolio snapshot
builds from scratch (Angel One login + NSE + IBJA + Yahoo Finance in
parallel). Subsequent loads are < 1s thanks to the in-process cache
(60s during market hours, 5 min otherwise).

To expose on your LAN, bind to all interfaces:

```bash
python -m webapp.server --host 0.0.0.0 --port 8000
```

---

## Daily usage

**The webapp is the main interface.** Just open it in your browser. The
Refresh button on the Portfolio page rebuilds the snapshot on demand.

For most users, the workflow is:
1. Open the dashboard once a day. The root URL redirects to `/dashboard`
   (all-charts overview).
2. Glance at the **Portfolio** page (your total P&L + day change + world
   indices + Gold:Silver ratio + MF holdings + shareholding + SGBs).
3. Skim the **Flows** page (what smart money did today).
4. Read any **Telegram alerts** that arrived (8:55 AM news digest;
   mid-day portfolio-impact stories; 4:30 PM MF-holdings diff email).

For deeper analysis, the **CAGR** page shows per-stock time-weighted
returns vs Nifty benchmarks, and the **Tax & P&L** page has the
per-trade verdicts. The CLI tools (`pipeline.cagr`, `pipeline.cohorts`,
`pipeline.xirr`) cover the same data plus richer diagnostics.

---

## The pages

### `/dashboard` — all-charts overview (default landing)

Single-page summary built from a fresh snapshot:
- **KPI strip** (4 tiles): Total Portfolio, My Equity, XIRR (ex-ETFs),
  Total P&L
- **Day change bar chart** — your portfolio vs 8 world indices
- **3 per-tier cards** — Large-cap / Mid-cap / Small-cap CAGR + alpha
- **Combined portfolio vs Nifty 50** chart
- **Per-tier charts** (3 small) and today's intraday sparkline

Built on the same data the Portfolio page uses; cold cache takes ~18s
on first load, then sub-second.

### `/portfolio` — the main dashboard

Four KPI tiles (Total / Equity / Mutual Funds / SGBs) + a horizontal
bar chart of day change vs 8 world indices + the **Gold:Silver ratio**
card (live oz/oz + rotation signal) + the **MF Holdings Trend**
section (which mutual funds bought/sold your stocks) + the **Shareholding
Pattern** table (promoter / FII / DII / banks / insurance / public for
each of your 8 tickers) + the SGB breakdown (per-bond price & day %).

**Refresh button** rebuilds the snapshot from scratch (5–10s on a cold
cache). The button shows a live "Refreshing… Ns" countdown while
building.

### `/cagr` — per-stock CAGR vs Nifty benchmarks

- **Top KPI grid**: 6 tiles including XIRR (ex-ETFs), XIRR (incl. ETFs),
  XIRR vs TWR alpha, Total invested, Total current, XIRR definition.
- **Combined portfolio vs Nifty 50** chart at the top.
- **Per-stock CAGR table** with each stock's time-weighted return,
  alpha vs Nifty 50, and per-tier cohort grouping.
- **Why XIRR ≠ TWR** callout that explains the difference in plain
  language.

JSON endpoint: `GET /api/xirr` (with `?include_etfs=true`).

### `/flows` — FII/DII + bulk/block deals

- **FII / DII daily flows** (NSE): net buy/sell by foreign and domestic
  institutional investors, in ₹ crores.
- **Bulk deals**: transactions where a single trade exceeded 0.5% of
  the company's equity.
- **Block deals**: same but in a separate pre-market window.

The data is read from local history files written by
`pipeline.flows_alert`; the **Re-scan now** button triggers a fresh
fetch.

### `/concalls` — con-call summaries

Per-ticker filter chips + cards showing the last ~7 days of con-call
filings and investor presentations. Each card has:
- management tone (positive / neutral / cautious)
- 1-line summary
- extracted guidance line
- per-ticker top buyer / top seller of the stock

Cached locally; **Re-scan now** triggers a fresh download.

### `/tax` — Tax & P&L dashboard

A combined view of:
- Equity delivery P&L (LTCG + STCG separately) by financial year
- Intraday P&L
- F&O options + futures P&L (turnover + net)
- Dividend income
- Charges (STT, stamp duty, brokerage, GST)
- Open holdings (cost, market value, unrealised — split ST vs LT)
- A **per-trade verdict** (GREAT / GOOD / OK / BAD / POOR / TERRIBLE)
  for every closed equity trade in the imported xlsx files.

**Also accepts ephemeral uploads of anyone else's Tax P&L** via the
📤 Upload button in the header. See [Tax & P&L workflow](#tax--pl-workflow).

**Driven by your own Angel One "Tax PNL" xlsx files** in
`data/tax_pnl/` (auto-detected). The ephemeral upload route is in
addition, not a replacement.

### `/fairvalue` — fair-value checker

Search any NSE/BSE stock by ticker or company name. The lookup
returns:
- Current LTP (from screener.in)
- Graham number
- DCF value (with customisable growth + discount rate)
- PE-relative value (if you supply an industry PE)

The result is rendered in a modal with the full breakdown.

### `/history` — historical portfolio snapshot

Embeds the most recent Plotly chart of portfolio value vs Nifty 50
over 3 months. The chart is regenerated by `portfolio_html.py`; the
History page just embeds the HTML.

### `/settings` — manage everything

- **Run alerts manually** with one click: News, MF-holdings, shareholding,
  portfolio-impact.
- **Force a re-fetch** for each data source.
- **Preview today's news digest** (Telegram-style, without sending).
- **View the alert log** for the last 30 runs of each background job.
- **View your portfolio composition** (number of MFs / SGBs / tickers
  loaded from your config files).

---

## Project layout

```
portfolio-tracker/
├── README.md                    ← you are here
├── MEMORY.md                    ← session-to-session AI handoff (gitignored)
├── LATENCY_AUDIT.md             ← data-pipeline latency notes
├── HARDWARE_RANKING.md          ← VPS / UPS / 4G-failover recommendations
├── requirements.txt             ← Python dependencies
├── pyproject.toml               ← tool config (pytest etc.)
├── .env.example                 ← safe template; copy to .env
├── .gitignore
│
├── webapp/                      ← FastAPI dashboard (UI + JSON API)
│   ├── server.py                ← all routes + startup tasks
│   ├── data.py                  ← snapshot builders + caches (incl. G/S ratio)
│   ├── tax_dashboard.py         ← Tax & P&L routes + ephemeral upload
│   ├── cache.py                 ← TTL cache helpers
│   ├── templates/               ← Jinja2 HTML (9 pages)
│   │   ├── dashboard.html       ← all-charts overview
│   │   ├── portfolio.html       ← main P&L + holdings + G/S ratio
│   │   ├── cagr.html            ← per-stock CAGR + XIRR
│   │   ├── flows.html
│   │   ├── concalls.html
│   │   ├── tax.html
│   │   ├── fairvalue.html
│   │   ├── history.html
│   │   └── settings.html
│   └── static/                  ← CSS + JS
│       ├── css/app.css
│       └── js/                  ← tax_pie.js, tax.js (upload modal), etc.
│
├── pipeline/                    ← all CLI data pipelines
│   ├── angel_client.py          ← Angel One SmartAPI wrapper
│   ├── equity_compare.py        ← today's portfolio vs indices
│   ├── mf_sgb.py                ← MF NAVs + SGB prices (4 sources)
│   ├── mf_holdings.py           ← monthly MF ownership of your 8 tickers
│   ├── mf_holdings_alert.py     ← diff vs yesterday → email alert
│   ├── shareholding_alert.py    ← quarterly pattern diff → email
│   ├── concalls.py              ← con-call transcripts + summaries
│   ├── flows_alert.py           ← FII/DII + bulk/block deals
│   ├── earnings_alert.py        ← results / board meetings calendar
│   ├── news_alert.py            ← global news digest → Telegram
│   ├── portfolio_impact.py      ← cross-reference news ↔ your tickers
│   ├── portfolio_html.py        ← Plotly historical chart
│   ├── fair_value/              ← screener.in fundamentals + DCF/Graham
│   ├── sentiment.py             ← news sentiment classifier
│   ├── sector_mechanisms.py     ← sector driver descriptions
│   ├── indices_chart.py         ← world indices from Yahoo Finance
│   ├── intraday.py              ← today's intraday OHLC
│   ├── history_db.py            ← SQLite schema + migrations
│   ├── scheduler.py             ← unified daily scheduler (IST)
│   ├── scheduler_utils.py       ← shared scheduler helpers
│   ├── parallel.py              ← ThreadPool helpers
│   ├── logging_setup.py         ← daily-rotating log files
│   ├── cagr.py                  ← per-stock CAGR + XIRR (CLI)
│   ├── cohorts.py               ← per-tier cohort analysis (CLI)
│   ├── cohort_charts.py         ← cohort plot rendering
│   ├── xirr.py                  ← XIRR solver + cash-flow builder
│   ├── ledger.py                ← ledger-backed portfolio history
│   ├── marketcap.py             ← market-cap classification
│   ├── index_data.py            ← Nifty 50 / Midcap 150 / Smallcap 250 data
│   ├── purge_ircon.py           ← one-off IRCON cleanup helper
│   ├── project_start.py         ← bootstrap entry point
│   │
│   ├── tax_pnl/                 ← broker-agnostic Tax P&L parser
│   │   ├── __init__.py          ← data model + parse_files()
│   │   ├── report.py            ← template-based markdown report
│   │   ├── sessions.py          ← ephemeral session storage
│   │   └── adapters/
│   │       ├── angel_one.py     ← fuzzy Angel One xlsx adapter
│   │       ├── zerodha.py       ← Zerodha Console CSV adapter
│   │       └── generic.py       ← column-mapping adapter
│   │
│   ├── portfolio_truth/         ← single source of truth for positions
│   │   ├── __init__.py          ← data model + load/save
│   │   ├── update.py            ← CLI updater
│   │   └── bootstrap.py         ← project-start hook
│   │
│   └── portfolio_monitor/       ← weekly concentration + 100-day reviews
│       ├── __init__.py
│       ├── holdings.py          ← equity (broker → yfinance → static)
│       ├── emailer.py           ← SMTP helper
│       ├── calendar.py          ← 100-day review for midcaps
│       ├── concentration_check.py
│       └── rebalance_diagnostic.py
│
├── tests/                       ← pytest suite (~980 tests)
│
├── data/                        ← ALL runtime state (gitignored)
│   ├── cache/                   ← API response caches (safe to delete)
│   │   ├── angel_session.json
│   │   ├── indices_cache.csv
│   │   ├── intraday_cache_{1m,5m,15m}.csv
│   │   ├── mf_master_cache.json
│   │   ├── mf_holdings_cache.json
│   │   ├── nse_equity_list.csv
│   │   └── screener/            ← HTML caches by hash
│   ├── db/                      ← SQLite + persisted snapshots
│   │   ├── history.db
│   │   ├── mf_holdings_prev.json
│   │   └── sgb_price_history.json.migrated
│   ├── alerts/                  ← per-pipeline alert state
│   │   ├── news/                ← log.json + seen.json
│   │   ├── flows/               ← log.json + seen.json + run.log
│   │   │   ├── fii_dii_history.json
│   │   │   └── bulk_block_history.json
│   │   ├── concalls/            ← log.json + seen.json + run.log + cache/
│   │   ├── earnings/            ← log.json + seen.json + run.log
│   │   ├── mf_holdings/         ← log.json
│   │   ├── shareholding/        ← log.json + prev.json
│   │   ├── portfolio_impact/    ← log.json + seen.json
│   │   └── scheduler.log
│   ├── tax_pnl_uploads/         ← ephemeral upload sessions (24h TTL)
│   │   └── <session-uuid>/
│   │       ├── meta.json
│   │       ├── <uploaded files>
│   │       └── parsed.json
│   ├── charts/                  ← generated PNGs + HTML
│   ├── logs/                    ← daily app logs (one folder per day)
│   ├── runs/                    ← ad-hoc run outputs
│   ├── scripts/                 ← pre-publish-check.sh, fetch_index_history.py
│   └── tax_pnl/                 ← drop your Angel One "Tax PNL" xlsx here
│
├── mfs.json                     ← your mutual fund holdings (gitignored)
├── sgbs.json                    ← your SGB holdings (gitignored)
├── my_tickers.txt               ← your 8 equity tickers (gitignored)
└── secrets.local.json           ← local API overrides (gitignored)
```

---

## Sub-projects

Two **standalone, client-side** web apps live alongside the main
FastAPI dashboard. Each is a separate Vercel project with its own
URL, its own deployment, and its own tests. They share the
"no backend, no data leaves the browser" architecture.

### `webapp-static-tax/` — static Tax P&L viewer

A pure-frontend HTML/JS app that reads a broker Tax P&L xlsx/CSV
(Angel One, Zerodha, or any tabular file with manual column
mapping), shows the capital gains breakdown, a pie/bar chart of
where every rupee went, and downloads a self-contained HTML
report. Files never leave the browser.

- **Path on this machine:** `webapp-static-tax/`
- **Live URL:** https://tax-pnl-pied.vercel.app
- **Branch:** `feature/static-tax-pnl-poc`
- **Tests:** `tests/static/` — 233 tests via `npm test`
- **Stack:** HTML + SheetJS + Plotly, all from CDN. No backend.

### `webapp-itr-workbook/` — Personal ITR-1 / ITR-2 checker

A privacy-first ITR workbook for the ~70% of Indian taxpayers who
file ITR-1 or ITR-2 (salaried + small investor). Same architecture:
no backend, all data in `localStorage`. Computes tax under both
old and new regimes with a side-by-side comparison and a
recommendation, supports Form 16 PDF and Form 26AS JSON import.

- **Path on this machine:** `webapp-itr-workbook/`
- **Branch:** `feature/itr-workbook`
- **Tests:** 233 tests in 7 suites via `npm test` (~160ms total):
  - Suite 1: Statutory Compliance & Tax Logic (48 tests)
  - Suite 2: Schema Validation (38 tests)
  - Suite 3: API Integration & E-Filing (28 tests)
  - Suite 4: Security & Data Privacy (27 tests)
  - Suite 5: Functional Stability (25 tests)
  - Suite 6: Performance & Non-Functional Stability (17 tests)
  - Suite 7: Industry Benchmarking (24 tests)
  - Engine (26 tests)
- **Stack:** Pure JS, no build step, no npm runtime deps. ~68KB
  total source. The HTML UI is planned for v2; the engine is
  fully usable via Node REPL or as a library today.

### Why these are separate projects (not routes on the main app)

1. **Different deployment lifecycles.** The main dashboard
   depends on a Python backend and APIs. These are pure static
   files. Mixing them would slow down deploys of either.
2. **Different threat models.** The main app authenticates with
   Angel One, has rate limits, etc. The static apps run entirely
   in the browser; no auth, no server, no PII leaves the device.
3. **Different users.** The main dashboard is for you (the
   portfolio owner). The static apps are tools that can be
   shared with anyone (your CA, your family) without giving
   them access to your live portfolio.

---

## Configuration

### Environment variables (`.env`)

| Variable | Used by | Required? |
|---|---|---|
| `ANGEL_API_KEY` | `pipeline.angel_client` | **Yes** (for live data) |
| `ANGEL_CLIENT_ID` | `pipeline.angel_client` | **Yes** |
| `ANGEL_PASSWORD` (MPIN) | `pipeline.angel_client` | **Yes** |
| `ANGEL_TOTP_SECRET` | `pipeline.angel_client` | **Yes** |
| `NEWS_TELEGRAM_BOT_TOKEN` | `pipeline.news_alert` | No (logs only if missing) |
| `NEWS_TELEGRAM_CHAT_ID` | `pipeline.news_alert` | No |
| `TELEGRAM_BOT_TOKEN` | `pipeline.portfolio_impact` | No |
| `TELEGRAM_CHAT_ID` | `pipeline.portfolio_impact` | No |
| `MF_ALERT_SMTP_HOST` | `pipeline.mf_holdings_alert` | No (logs only) |
| `MF_ALERT_SMTP_PORT` | `pipeline.mf_holdings_alert` | default 587 |
| `MF_ALERT_SMTP_USER` | `pipeline.mf_holdings_alert` | No |
| `MF_ALERT_SMTP_PASSWORD` | `pipeline.mf_holdings_alert` | No |
| `MF_ALERT_SMTP_TO` | `pipeline.mf_holdings_alert` | No |
| `PM_ALERT_SMTP_*` | `pipeline.portfolio_monitor` | No (overrides MF_*) |
| `NEWS_DISABLED=1` | server | opt-out of news scheduler |
| `MF_ALERT_DISABLED=1` | server | opt-out of MF alert scheduler |
| `SHP_ALERT_DISABLED=1` | server | opt-out of shareholding scheduler |
| `PORTFOLIO_IMPACT_DISABLED=1` | server | opt-out of impact scanner |
| `PM_OVERDUE_TELEGRAM=1` | `pipeline.scheduler` | opt-in overdue-task Telegram alerts |
| `NEWS_DRY_RUN=1` | `pipeline.news_alert` | log instead of send |
| `PT_LOG_LEVEL` | `pipeline.logging_setup` | DEBUG / INFO / WARNING / ERROR |

### Config files

- **`mfs.json`** — your mutual fund holdings. See `mfs.json.example`.
- **`sgbs.json`** — your SGB holdings (ISIN + grams). See `sgbs.json.example`.
- **`my_tickers.txt`** — one ticker per line; the default 8 the dashboard focuses on.
- **`secrets.local.json`** — local API overrides; merged into env at runtime.

---

## Running automatically every day

The webapp starts the following schedulers as daemon threads when it boots:

| Time (IST) | Job | Output |
|---|---|---|
| 03:00 AM | sweep expired Tax P&L upload sessions | local cleanup |
| 08:55 AM | news digest | Telegram message |
| 08:55 AM | earnings calendar scan | local cache |
| 11:00 AM | MF holdings diff | email if changes |
| 11:05 AM | shareholding pattern diff | email if changes |
| 16:30 PM | MF holdings (re-check) | email if changes |
| 18:45 PM | FII/DII + bulk/block deals | local cache + (optional) email |
| 19:00 PM | con-call filings | local cache + (optional) Telegram |
| every 30 min (market hours) | portfolio-impact news scan | Telegram alert per story |

**Startup catch-up** (on webapp boot) looks back 12h and fires any
missed tasks within that window. The 5-minute per-task **missed-window
guard** (`RUN_GRACE_SECS` in `pipeline.scheduler`) skips tasks older
than 5 min past their target time to avoid sending stale alerts.

The watchdog logs overdue tasks every 15 min to the orchestrator
log. Telegram pings for overdue tasks are OFF by default (set
`PM_OVERDUE_TELEGRAM=1` in `.env` to enable) so a multi-day Mac
sleep doesn't spam you with "task due 18h ago" alerts that you
already know about.

For a more production-grade setup, run the unified scheduler in its
own process:

```bash
python -m pipeline.scheduler              # foreground daemon
python -m pipeline.scheduler --show-schedule   # print the schedule
```

---

## Tax & P&L workflow

### Your own files (the default)

1. Open Angel One → **Reports** → **Tax P&L** → download the
   `Tax PNL <FY>.xlsx` for the financial year you just closed.
2. Drop the file into `data/tax_pnl/` (Finder-friendly path).
3. Open the **Tax & P&L** page — it picks up the new file on the next
   refresh (TTL is 4 minutes; click the **Refresh tax** button to force).
4. The chart-toggles let you slice by financial year, side-by-side
   comparison of two FYs, and a per-trade verdict table sorted by
   P&L percentage.

The parser auto-detects CDSL/NSDL `Tax PNL` exports, splits delivery
vs intraday vs F&O, and rolls up charges (STT, stamp duty, brokerage,
GST) into a single P&L summary.

### Someone else's file (ephemeral upload)

The **📤 Upload someone's Tax P&L** button on the Tax & P&L page lets
you analyse any Angel One or Zerodha (or other) Tax P&L export
without saving it permanently. Sessions are **24h TTL** and live in
`data/tax_pnl_uploads/<uuid>/` (separate from your own data so they
can never pollute your pipeline).

- **Angel One**: drop in `Tax PNL <FY>.xlsx` — auto-detected
- **Zerodha**: drop in `Console P&L <FY>.csv` — auto-detected
- **Any other tabular file**: select files, click "My file isn't
  Angel One or Zerodha", map the columns, and submit. The mapping
  is sent as a JSON form field.

Once uploaded, the Tax & P&L dashboard re-renders against the
upload with a banner showing the session ID and expiry. A "📋 Markdown
report" button renders a template-based report (headline, year-by-year
breakdown, verdict distribution, 3-6 plain-English insights) — no LLM,
fully deterministic, copy-pasteable.

API endpoints (also available for programmatic access):
- `GET  /api/tax/upload/brokers` — supported brokers
- `POST /api/tax/upload` — create session + upload files
- `POST /api/tax/upload/{id}/mapping` — set column mapping
- `GET  /api/tax/upload/{id}/report` — markdown report
- `DELETE /api/tax/upload/{id}` — cleanup

Limits: 10 files per session, 20 MB per file, xlsx/xlsm/csv only,
xlsx magic bytes enforced, filenames sanitised against path traversal.

A daily 03:00 IST sweep (`pipeline.scheduler.tax_pnl.sweep_sessions`)
removes expired sessions.

---

## Cohorts, XIRR & ad-hoc analysis

### Cohorts (per-market-cap-tier CAGR)

Groups your holdings into Large-cap / Mid-cap / Small-cap tiers and
compares each tier's CAGR vs the appropriate Nifty benchmark:

```bash
python -m pipeline.cohorts                 # show tier breakdown + alpha
python -m pipeline.cohort_charts          # render combined vs Nifty 50 chart
```

Web UI: see the **/dashboard** page (3 per-tier cards + combined chart)
and the **/cagr** page (per-stock CAGR table).

### XIRR (money-weighted return)

CAGR treats all your capital as if it were invested from day 1, which
overstates or understates your actual return when buy timing is uneven.
XIRR accounts for when each rupee actually went in:

```bash
python -m pipeline.xirr                    # print XIRR (ex-ETFs)
python -m pipeline.xirr --include-etfs     # include GOLDBEES/SILVERBEES/etc.
```

Web UI: KPI tile at the top of **/cagr** with both XIRR numbers and
the "Why XIRR ≠ TWR" callout. JSON endpoint: `GET /api/xirr`.

### Project truth

`pipeline.portfolio_truth` is the **single source of truth** for all
your positions (equity + MF + SGB + watchlist). All other modules
(portfolio snapshot, monitor, cohorts, XIRR) read from this file
rather than broker responses directly. This guarantees consistency
across modules and gives the portfolio-monitor module a stable
baseline to detect drift.

```bash
python -m pipeline.project_start           # bootstrap on launch
python -m pipeline.portfolio_truth.update  # refresh from broker
```

The truth file lives at `data/portfolio_truth.json` (gitignored).

### Portfolio monitor (weekly checks)

`pipeline.portfolio_monitor` runs weekly concentration + drift
diagnostics and a 100-day review for the BALRAMCHIN / KNRCON / UNOMINDA
midcap bets:

```bash
python -m pipeline.portfolio_monitor.run_all   # full check, all jobs
```

Email alerts use the same SMTP creds as the MF-holdings alerts
(`MF_ALERT_SMTP_*`, with `PM_ALERT_SMTP_*` as an override).

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The suite is ~1,000 tests, runs in ~5 min, all offline (uses local
fixtures for Angel One, mfapi.in, screener.in, NSE).

The browser E2E tests (`test_fairvalue_e2e_browser.py`,
`test_intraday_e2e_browser.py`) need a Playwright install:

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests/test_fairvalue_e2e_browser.py
```

**Recent test coverage highlights:**
- `tests/test_tax_pnl_upload.py` (28 tests) — broker adapters, ephemeral
  sessions, upload validation, Generic column mapping
- `tests/test_gold_silver.py` (13 tests) — signal logic, yfinance
  mock, cache behaviour
- `tests/test_xirr.py`, `tests/test_cagr.py`, `tests/test_cohorts.py`,
  `tests/test_cohort_charts.py` — return analysis
- `tests/test_portfolio_truth.py` — single-source-of-truth file

---

## Limitations & known issues

- **Angel One session expires every trading day.** The auto-relogin
  in `angel_client.login()` handles this transparently; you'll see
  "Invalid Token" warnings in the logs the first time each day.
- **NSE rate-limits aggressively.** Some flows fetcher pages return
  403 if hit too often. Back off and try again in 5 minutes.
- **Tax PNL parser is heuristic.** It works on the standard Angel One
  xlsx export but may miss edge cases (auction trades, buyback
  proceeds, etc.). Open an issue with a redacted sample if you hit
  one.
- **The portfolio-impact scanner fires every 30 min during market
  hours.** If you have lots of holdings, you may get a flurry of
  Telegram messages. Adjust `interval_minutes` in `webapp.server.main()`.
- **`bulk_block_history.json` grows unbounded.** Trim manually if it
  gets above 5 MB.
- **Multi-day Mac sleep loses scheduled alerts.** The orchestrator's
  startup catch-up looks back only 12h, and the per-task missed-window
  guard is 5 min. Tasks older than that (e.g. last Saturday's 8:55 AM
  news digest if you wake up this Saturday) are dropped. To catch up
  manually after a long sleep, force-run the pipeline:
  ```bash
  python -m pipeline.news_alert --force
  python -m pipeline.flows_alert --force
  python -m pipeline.concalls --once
  python -m pipeline.earnings_alert --once
  ```
- **Tax P&L ephemeral uploads are limited to 10 files × 20 MB per
  session, 24h TTL.** xlsx/xlsm/csv only, with magic-byte validation
  and filename sanitisation. Sessions live in
  `data/tax_pnl_uploads/` and are auto-swept at 03:00 IST.
- **Zerodha adapter is built from documented column names** (not a
  real sample file). If your Console P&L export has different
  columns, use the Generic column-mapping upload instead.

---

## Security — what's safe to publish

- ✅ `mfs.json.example`, `sgbs.json.example`, `my_tickers.txt.example` —
  templates, no personal data.
- ✅ All code in `pipeline/`, `webapp/`, `tests/`.
- ❌ `.env`, `secrets.local.json` — **never commit**.
- ❌ `mfs.json`, `sgbs.json`, `my_tickers.txt` — contain your actual
  holdings.
- ❌ `data/` — runtime state, may contain downloaded PDFs from
  con-call transcripts with company logos (low risk but unnecessary).
- ❌ `MEMORY.md` — AI session memory; treat as private notes.

The `data/scripts/pre-publish-check.sh` script verifies the above
before any commit.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'pipeline'` | `cd` to the project root before running. |
| `Angel One login failed: Invalid Token` | Re-generate TOTP secret in `.env`; auto-relogin tries once. |
| `Portfolio value shows ₹0` | Check `mfs.json` and `my_tickers.txt` exist; check `.env` credentials. |
| `News digest never arrives` | Verify `NEWS_TELEGRAM_BOT_TOKEN` and `NEWS_TELEGRAM_CHAT_ID`; test with `curl https://api.telegram.org/bot$TOKEN/getMe`. |
| `concall page shows "No summaries yet"` | Run `python -m pipeline.concalls --once` to force a fetch. |
| `Tax & P&L page shows old data` | Click **Refresh tax** (forces a 4-min cache bypass). |
| `Port 8000 already in use` | `python -m webapp.server --port 8123`. |
| Mac went to sleep and the news digest arrived twice | Already fixed in `news_alert._scheduler_loop` (missed-window guard). Update to latest. |
| Mac went to sleep for a week and no alerts fired | Expected — the 12h catch-up + 5-min grace window deliberately skip stale alerts to avoid spamming. Force-run the pipelines manually. |
| Gold:Silver ratio card shows "unavailable" | yfinance is down. Card will recover on the next page load (60s TTL). |
| Tax P&L upload returns "no files accepted" | Check the file extension (xlsx/xlsm/csv only), size (≤20 MB), and that xlsx files start with the PK\x03\x04 magic bytes. |
| `/cagr` page returns 500 | Run `python -m pipeline.cagr` from the CLI to see the traceback. Common cause: missing index history data (`data/scripts/fetch_index_history.py`). |
| Chart on /dashboard is stale | Append `?v=<unix_timestamp>` to chart URLs (already done in template). Or hard-refresh the browser. |

---

## License

Personal use only. The Angel One and NSE APIs are subject to their
respective terms of service — be a good citizen with rate limits.
