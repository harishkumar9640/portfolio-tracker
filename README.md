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

---

## Table of contents

1. [What you get](#what-you-get)
2. [Quick start](#quick-start)
3. [Daily usage](#daily-usage)
4. [The 7 pages](#the-7-pages)
5. [Project layout](#project-layout)
6. [Configuration](#configuration)
7. [Running automatically every day](#running-automatically-every-day)
8. [Tax & P&L workflow](#tax--pl-workflow)
9. [Tests](#tests)
10. [Limitations & known issues](#limitations--known-issues)
11. [Security — what's safe to publish](#security--whats-safe-to-publish)
12. [Troubleshooting](#troubleshooting)

---

## What you get

| Surface | What it does |
|---|---|
| **Web dashboard** | 7 pages: Portfolio, Flows, Con-calls, Tax & P&L, Fair Value, History, Settings. Mobile-friendly, dark-mode aware, real-time data with manual refresh. |
| **Background alerts** | News digest (Telegram at 8:55 AM), earnings/board meetings, con-call summaries, FII/DII flows + bulk/block deals, MF-holdings changes, shareholding-pattern changes, portfolio-impact news. |
| **Tax P&L parser** | Reads your Angel One "Tax PNL" xlsx files and renders a multi-year P&L dashboard with realised + unrealised gains, charges, and a per-trade verdict. |
| **Fair-value checker** | Graham number + DCF + PE-relative value for any NSE/BSE stock, with screener.in fundamentals. |

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
1. Open the dashboard once a day.
2. Glance at the **Portfolio** page (your total P&L + day change + world
   indices for context).
3. Skim the **Flows** page (what smart money did today).
4. Read any **Telegram alerts** that arrived (8:55 AM news digest;
   mid-day portfolio-impact stories; 4:30 PM MF-holdings diff email).

---

## The 7 pages

### `/portfolio` — the main dashboard

Four KPI tiles (Total / Equity / Mutual Funds / SGBs) + a horizontal
bar chart of day change vs 8 world indices + the MF Holdings Trend
section (which mutual funds bought/sold your stocks) + the Shareholding
Pattern table (promoter / FII / DII / banks / insurance / public for
each of your 8 tickers) + the SGB breakdown (per-bond price & day %).

**Refresh button** rebuilds the snapshot from scratch (5–10s on a cold
cache). The button shows a live "Refreshing… Ns" countdown while
building.

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

**Driven by Angel One "Tax PNL" xlsx files** — see
[Tax & P&L workflow](#tax--pl-workflow) below.

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
├── requirements.txt             ← Python dependencies
├── pyproject.toml               ← tool config (pytest etc.)
├── .env.example                 ← safe template; copy to .env
├── .gitignore
├── MEMORY.md                    ← session-to-session AI handoff
│
├── webapp/                      ← FastAPI dashboard (UI + JSON API)
│   ├── server.py                ← all routes + startup tasks
│   ├── data.py                  ← snapshot builders + caches
│   ├── tax_dashboard.py         ← Tax & P&L routes + parser
│   ├── templates/               ← Jinja2 HTML (7 pages)
│   └── static/                  ← CSS + JS
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
│   ├── parallel.py              ← ThreadPool helpers
│   └── logging_setup.py         ← daily-rotating log files
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
│   ├── charts/                  ← generated PNGs + HTML
│   ├── logs/                    ← daily app logs (one folder per day)
│   ├── runs/                    ← ad-hoc run outputs
│   ├── scripts/                 ← pre-publish-check.sh
│   └── tax_pnl/                 ← drop your Angel One "Tax PNL" xlsx here
│
├── mfs.json                     ← your mutual fund holdings (gitignored)
├── sgbs.json                    ← your SGB holdings (gitignored)
├── my_tickers.txt               ← your 8 equity tickers (gitignored)
└── secrets.local.json           ← local API overrides (gitignored)
```

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
| `NEWS_DISABLED=1` | server | opt-out of news scheduler |
| `MF_ALERT_DISABLED=1` | server | opt-out of MF alert scheduler |
| `SHP_ALERT_DISABLED=1` | server | opt-out of shareholding scheduler |
| `PORTFOLIO_IMPACT_DISABLED=1` | server | opt-out of impact scanner |
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
| 08:55 AM | news digest | Telegram message |
| 08:55 AM | earnings calendar scan | local cache |
| 11:00 AM | MF holdings diff | email if changes |
| 11:05 AM | shareholding pattern diff | email if changes |
| 16:30 PM | MF holdings (re-check) | email if changes |
| 18:45 PM | FII/DII + bulk/block deals | local cache + (optional) email |
| 19:00 PM | con-call filings | local cache + (optional) Telegram |
| every 30 min (market hours) | portfolio-impact news scan | Telegram alert per story |

The **missed-window guard** in `news_alert._scheduler_loop` means that
if your Mac was asleep past 8:55 AM, the next 8:55 AM digest will be
**skipped** (not re-sent at wake-up) to avoid duplicates.

For a more production-grade setup, run the unified scheduler in its
own process:

```bash
python -m pipeline.scheduler              # foreground daemon
python -m pipeline.scheduler --show-schedule   # print the schedule
```

---

## Tax & P&L workflow

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

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The suite is ~980 tests, runs in ~5 min, all offline (uses local
fixtures for Angel One, mfapi.in, screener.in, NSE).

The browser E2E tests (`test_fairvalue_e2e_browser.py`,
`test_intraday_e2e_browser.py`) need a Playwright install:

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests/test_fairvalue_e2e_browser.py
```

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

---

## License

Personal use only. The Angel One and NSE APIs are subject to their
respective terms of service — be a good citizen with rate limits.
