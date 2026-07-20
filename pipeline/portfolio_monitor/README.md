# Portfolio Monitor

Three scripts that monitor your equity portfolio and email you on signals.

## Quick start

```bash
# One-time: capture the 100-day reference prices for the 3 mid-caps
python -m pipeline.portfolio_monitor.calendar --init

# One-time: capture the rebalance baseline
python -m pipeline.portfolio_monitor.rebalance_diagnostic --init-baseline

# Daily / weekly / monthly — see the "Cron" section
```

## Scripts

### 1. `calendar` — 100-day review (BALRAMCHIN, KNRCON, UNOMINDA)

- Captures current prices as a 100-day reference (`--init`)
- On day 100 (and ±7 days around it), evaluates each position:
  - **TRIM** if down > 15% from ref
  - **BOOK PARTIAL PROFIT** if up > 20% from ref
  - **HOLD** otherwise
- Sends HTML+plain email to `PM_ALERT_TO`

```bash
python -m pipeline.portfolio_monitor.calendar --status      # see current state
python -m pipeline.portfolio_monitor.calendar --force      # send email now
python -m pipeline.portfolio_monitor.calendar --reset      # re-init baseline
```

State file: `data/calendar_100day_state.json`

### 2. `concentration_check` — weekly position + Gold:Silver alert

- Checks top-1, top-2, top-3 weights against thresholds
- Default thresholds: 20% / 35% / 50% (configurable via `PM_CONC_TOP*_MAX` env vars)
- Fetches live Gold:Silver ratio (GC=F / SI=F) and signals:
  - **BUY SILVER** if ratio > 90
  - **TAKE PROFIT ON SILVER** if ratio < 60
  - **HOLD** otherwise
- Sends email only on breach

```bash
python -m pipeline.portfolio_monitor.concentration_check --status
python -m pipeline.portfolio_monitor.concentration_check --force
```

### 3. `rebalance_diagnostic` — monthly portfolio drift

- Compares current sector mix, top-2 concentration, large-cap share,
  weighted P/E / P/B / ROE / div yield against a saved baseline
- Sends email on first run (so you have a baseline) and when drift exceeds
  thresholds (sector ±5%, top-2 ±3%, large-cap ±5%)
- Fetches fundamentals from yfinance with a 7-day cache

```bash
python -m pipeline.portfolio_monitor.rebalance_diagnostic --init-baseline
python -m pipeline.portfolio_monitor.rebalance_diagnostic --status
python -m pipeline.portfolio_monitor.rebalance_diagnostic --force
```

State file: `data/rebalance_baseline.json`
Fundamentals cache: `data/fundamentals_cache.json`

### 4. `run_all` — cron-friendly runner

```bash
python -m pipeline.portfolio_monitor.run_all --review    # 100-day review
python -m pipeline.portfolio_monitor.run_all --weekly    # concentration
python -m pipeline.portfolio_monitor.run_all --monthly   # rebalance
python -m pipeline.portfolio_monitor.run_all --all       # all three
```

## Cron setup (recommended)

```cron
# 100-day calendar review — run daily, but it only sends in the ±7-day window
0 9 * * *  cd /Users/hkc21/portfolio-tracker && /path/to/.venv/bin/python -m pipeline.portfolio_monitor.run_all --review

# Weekly concentration check — Mondays 10am IST
0 10 * * 1 cd /Users/hkc21/portfolio-tracker && /path/to/.venv/bin/python -m pipeline.portfolio_monitor.run_all --weekly

# Monthly rebalance diagnostic — 1st of month, 9am IST
0 9 1 * *  cd /Users/hkc21/portfolio-tracker && /path/to/.venv/bin/python -m pipeline.portfolio_monitor.run_all --monthly
```

## Email config

Reuses the existing MF alert SMTP setup. Add to `.env`:

```bash
PM_ALERT_SMTP_HOST=smtp.mail.yahoo.com
PM_ALERT_SMTP_PORT=587
PM_ALERT_SMTP_USER=you@example.com
PM_ALERT_SMTP_PASS=app-password
PM_ALERT_FROM=you@example.com        # optional, defaults to SMTP_USER
PM_ALERT_TO=you@example.com          # optional, defaults to SMTP_USER
PM_ALERT_DRY_RUN=1                   # set 0 to actually send
```

Or reuse the existing MF alert vars (the emailer falls back to `MF_ALERT_*` if `PM_ALERT_*` is missing).

## Thresholds (override via env)

| Var | Default | Used by |
|---|---|---|
| `PM_CONC_TOP1_MAX` | 20.0 | concentration_check |
| `PM_CONC_TOP2_MAX` | 35.0 | concentration_check |
| `PM_CONC_TOP3_MAX` | 50.0 | concentration_check |
| `PM_CONC_GSILVER_RATIO_HIGH` | 90.0 | concentration_check |
| `PM_CONC_GSILVER_RATIO_LOW` | 60.0 | concentration_check |

(Calendar trim threshold = 15% hard-coded in `calendar.py`. Rebalance
sector/conc drift thresholds = 5%/3% hard-coded in `rebalance_diagnostic.py`.
Edit the file if you want to change them.)

## Data sources

- **Holdings & LTP**: Angel One SmartAPI (broker) → yfinance fallback → static list
- **Fundamentals (P/E, ROE, etc.)**: yfinance, 7-day cached
- **Gold/Silver ratio**: yfinance GC=F / SI=F

If broker login fails, the scripts fall back to yfinance. If yfinance fails,
they use the static list (which has stale LTP — only "broker" source is real).
The email subject line always shows the data source so you know.

## State files

- `data/portfolio_snapshot.json` — latest broker snapshot (auto-written on every fetch)
- `data/calendar_100day_state.json` — 100-day reference prices
- `data/rebalance_baseline.json` — sector mix / concentration baseline
- `data/fundamentals_cache.json` — yfinance fundamentals cache (7-day TTL)

All gitignored automatically (under `data/`).
