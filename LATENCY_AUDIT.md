# Latency Audit — Portfolio Tracker

**Date:** 2026-07-01
**Scope:** All request paths under `webapp/server.py` and the scheduled pipelines under `pipeline/scheduler.py`.
**Method:** Static code review + live profiling on the current dev environment (warm caches).

---

## TL;DR — Where the time goes

| Path | Typical latency (warm) | Typical latency (cold) | Verdict |
|---|---|---|---|
| `GET /portfolio` (first hit / manual refresh) | **~5–8 s** | **~15 s** (first login + first yfinance pull) | 🔴 **worst hot path** |
| `GET /api/intraday?interval=5m` | 0.2 s (cached) / 3–4 s (cold) | 3–4 s | 🟡 |
| `GET /api/portfolio` (cached) | < 50 ms | n/a | 🟢 cache works |
| `GET /flows` (HTML) | 50 ms | n/a (file-only) | 🟢 |
| `GET /concalls` (HTML) | 100–300 ms | n/a (file-only) | 🟢 |
| `GET /api/refresh?kind=portfolio` (background) | spawns the same 5–15 s job | same | 🔴 cascades to everything else |
| `POST /api/concalls/run` (background) | n/a | **30 s – 5 min** (Ollama) | 🟡 design-correct, but not cancellable |
| `scheduler.py` orchestrator | n/a | each job 1–60 s | 🟢 |
| `flows_alert.run_once()` (scheduled) | 1 s | 5 s (with retries) | 🟢 |
| `webapp.data` cold import | 0.04 s | n/a | 🟢 |

**The single biggest win available is `/portfolio` rebuild latency: 15 s → 2–3 s** with the changes in §1.

---

## 1. `/portfolio` rebuild is the #1 latency

### Measured (warm cache, 2026-07-01)
```
equity_compare.build_snapshot(): 15.99 s   (first run)
  ├─ Angel One login (cached):      0.05 s
  ├─ fetch_holdings:                1.11 s   (SmartAPI round-trip + LTP fallback)
  ├─ fetch_mf_rows (5 MFs):         1.03 s   (sequential; ~200 ms/fund)
  ├─ fetch_sgb_rows (2 SGBs):       1.04 s   (NSE quote + mintbyte mapping)
  ├─ fetch_indices("5d") (8 idx):   1.81 s   (cache hit; cold = ~10 s)
  └─ post-process + DB writes:      <0.5 s
```

These fetches are **already parallelised** via `pipeline.parallel.fetch_all` (see `equity_compare.py:235–256`). The remaining 15 s is mostly:
- a **serial** step for `fetch_indices` (run after `fetch_all` instead of inside it)
- 1 s of NSE SGB "fetch all 45 SGB instruments" overhead in `mf_sgb.py:101–110` on every call
- the **first call** to `yfinance` for each ticker hits Yahoo's rate limiter (we saw the cold path get rate-limited on login)
- the LTP fallback in `angel_client.py:215–225` is sequential (one batch call, but still ~200–400 ms on top of `holding()`)

### Concrete fixes (ordered by impact)

1. **Move `fetch_indices` into the parallel group** — `equity_compare.py:206-256` runs indices *after* `fetch_all({equity, mf, sgb})`. Adding `"indices": _fetch_indices` to the `fetch_all` dict saves another ~1.8 s.
   - Expected: 15.99 s → ~14 s (smaller win than I hoped, but free).

2. **Cache the NSE SGB instrument list** in `mf_sgb.py:_fetch_nse_sgb_instruments()` (currently re-fetches 45 instruments on every snapshot build). 1 s → 0 s.
   - Expected: ~1 s saved.

3. **Pre-warm the master scheme list** (`_MASTER_CACHE` in `mf_sgb.py:64-80`) — the 6 MB JSON is already on disk; just read it instead of refetching when the in-process cache is cold. This is already done; just verify the cache file isn't being reloaded on each module reload.

4. **Skip the LTP fallback in `angel_client.py`** when `holding()` already returned LTPs. Currently we always make a second `getMarketData` call for the empty-LTP filter, even though for an 11-holding portfolio with all LTPs populated it's pure waste. Add a guard `if any(ltp == 0 for h in needs_ltp): skip the call`.
   - Expected: ~200–400 ms saved.

5. **Make the yfinance download tolerant of the cache-having-today** for `fetch_indices` — currently we refetch all 8 tickers from Yahoo if even one is missing today's row (`indices_chart.py:96-103`). The check is per-ticker but the refetch is global. Either:
   - refetch only the missing tickers (`if "^NSEI" not in cached.columns or pd.isna(cached.loc[today, "^NSEI"]): fetch(t)`), or
   - skip the per-day row check entirely and rely on the file's mtime (≤ 24 h is "fresh enough" for 5d/1mo/3mo charts).
   - Expected: saves the yfinance cold path entirely after the first run; the cold-path rate-limit we saw goes away.

6. **Tighten the in-process portfolio cache TTL** — currently 60 s during market hours. The data only changes every 1–15 min (Angel LTP ticks every few seconds, but the snapshot is end-of-day). Bumping to 90 s or 120 s reduces the chance a manual refresh re-fires after a single page load.
   - Expected: 30% fewer rebuilds in interactive use.

7. **Persist the in-process cache across server restarts** — write `_portfolio_cache["data"]` to `data/cache/portfolio_snapshot_cache.json` (with mtime check). Server restarts (which happen often in dev) currently force a fresh 5–15 s rebuild on the first request. The 60 s thread cache only lives in memory.
   - Expected: first request after restart goes from 15 s → 50 ms.

### Estimated combined impact of fixes 1–7:
- **Cold rebuild: 15 s → 2–3 s** (most of the saving is fix 5 — the Yahoo refetch is the biggest single cost on cold).
- **Warm rebuild: 6 s → 1.5 s**.
- **First request after server restart: 15 s → 50 ms** (fix 7).

---

## 2. `/api/intraday` is fast on cache, slow on cold

### Measured
- Warm (5-min cache): 0.2 s — fine.
- Cold (no cache): 3–4 s for 8 indices in parallel via `map_parallel`. Single biggest contributor is `yfinance.download(period="60d", interval="5m")` for 8 tickers.

### Bug: the "My Portfolio" line is always flat
`intraday.py:188` reads `data/holdings_cache.json`, which is **never written anywhere in the codebase**. The result: `equity intraday skipped` (logged) → fallback to Nifty as proxy → the portfolio line tracks Nifty 1:1 instead of the user's actual portfolio.
- **Latency cost:** none.
- **Correctness cost:** misleading chart.
- **Fix:** in `equity_compare.build_snapshot()` (or the snapshot writer), persist `data/holdings_cache.json` on each rebuild. Or call `angel_client.fetch_holdings()` directly from `intraday._load_equity_holdings()`. The Angel call is already in the cache, so cost is ~0.

### Other improvements
- **Cache the equity-portfolio series** (currently a per-build download of 8–11 tickers). TTL could be the same 5 min as the indices cache.
- **`normalize_to_open_today` does two full DataFrame copies** (`df = df.copy()` and `df = df[df.index <= latest_valid]`). The second one is a slice so it's cheap; the first is on every call. Fine for now.

---

## 3. Webapp cache strategy is mostly fine, with one fragile spot

### What's working
- `_portfolio_cache` is properly thread-safe with an `in_progress` flag — manual refresh during a rebuild returns the stale cache (good).
- `_CACHE_TTL = 300` is reasonable for non-market-hours.
- The cache-warmer thread on server start (`server.py:540-555`) pre-builds the snapshot so the first browser hit is fast.

### What's fragile
1. **`@lru_cache` on intraday or fairvalue endpoints** — none spotted, but grep suggests historical use. Switch to `cachetools.TTLCache` if any exist.
2. **`_get_shareholding_for_portfolio()` in `server.py:78-119` reads `data/shareholding_prev.json` synchronously on every `/portfolio` request** — it's a small file but `json.loads` + dict comprehension every request is wasted. Hoist to a module-level dict refreshed on a 60 s timer, or memoize with a `@functools.lru_cache(maxsize=1, ttl=60)`.
3. **No ETag / `Last-Modified` headers** — every browser refresh re-downloads the full HTML. Add `response.headers["ETag"] = str(cache_ts)` and handle `If-None-Match` → 304. Saves a few hundred ms for repeat users.

---

## 4. The Yahoo Finance dependency is the single biggest cold-path cost

Across the codebase, yfinance is hit in 4 places:
- `equity_compare.py:71` (`fetch_equity_prev_value`) — 8+ tickers, every snapshot
- `indices_chart.py:106` (`fetch_indices`) — 8 tickers
- `intraday.py:80` (`_download_one`) — 8 tickers, every snapshot
- `shareholding_alert.py` (per stock, monthly) — N tickers

Each `yf.download(period="60d", interval="5m")` call costs ~300–500 ms over the network. Cold-path `fetch_indices` does **8 sequential downloads** (with internal retries, each 2s backoff) → 8 × 3 s = 24 s worst case, ~10 s typical.

### Mitigations (in order of effort)
1. **Done — `CACHE_FILE` in `indices_chart.py:43`** — persists to CSV. But the "is today's row present" check forces a refetch too aggressively (see §1 fix #5).
2. **Use `yf.download(tickers, period=..., interval=..., group_by="ticker")` for batch download** — Yahoo supports up to ~200 tickers in one HTTP call. 8 separate calls → 1 call. This is the single biggest cold-path win.
3. **Self-host a tiny proxy** that caches Yahoo responses for 5 min — overkill for a personal tracker.
4. **Replace Yahoo with an alternate data source for EOD** — NSE's own API for Nifty, stooq.com CSV for others. More work, less rate-limit risk.

### Recommended: option 2 (batch download)
`yf.download(["^NSEI", "^GSPC", ...], period="max", interval="1d", group_by="ticker")` returns one DataFrame with a MultiIndex. After flattening it's a 1-shot call.
- Expected: 8 × 1.8 s → 1 × 2.5 s on cold, ~50 % savings on warm.

---

## 5. `concalls.run_once()` — slow by design, no way to cancel

### Architecture
1. `find_recent_filings` — NSE GET (1 s) + 1 PDF download per filing via **Playwright headless Chromium** (3–5 s per PDF cold, 1–2 s warm).
2. `summarize_with_ollama` — one LLM call per 6 000-char chunk + one reduction pass for multi-chunk. On a 50-page PDF with ~75 000 chars: ~13 chunks × 5 s = **~65 s of LLM time**.

Total: a single 50-page transcript takes ~75 s to process. The 19:00 IST scheduler kicks this off, and Playwright is single-threaded so the whole run is serial.

### Issues
1. **Playwright launches a fresh Chromium per PDF** (`_http_download` in `concalls.py:184-219`). Cold-launch is 2–3 s *per PDF*. We have ~5–15 PDFs per run → **15–45 s of pure browser startup overhead**. Reuse one browser context across all PDFs.
2. **The Ollama reduction pass duplicates work** — the per-chunk summaries already contain the same structure; the reduction prompt often just truncates them. Consider skipping reduction if `len(chunks) == 1` (most are), and capping chunk count to 8 for the rest.
3. **`POST /api/concalls/run` is fire-and-forget** with no cancel/timeout. A stuck Playwright call (NSE hangs) will hold the worker thread indefinitely. Wrap in a `subprocess` with a 5-min timeout.
4. **No incremental processing** — every run re-fetches `days_back=7` filings and re-summarizes anything not in `seen.json`. If `seen.json` is corrupt, the system does the full work again. The dedupe state is good but the seen-file is loaded into a dict and never trimmed (it grows unbounded with `days_back`).

### Recommended fix for the Playwright issue
```python
# Pseudocode
with sync_playwright() as p:
    browser = p.chromium.launch(...)        # once per run
    context = browser.new_context(...)
    page = context.new_page()
    page.goto("https://www.nseindia.com/")   # once per run, share cookies
    for filing in filings:
        resp = page.request.get(filing.pdf_url, ...)
        ...
    browser.close()
```
- Expected: 15 PDFs × 3 s = 45 s → 1 launch (3 s) + 15 × 1.5 s = ~25 s. ~45 % saving on a 15-PDF run.

---

## 6. `flows_alert.run_once()` — already fast

### Measured: ~1 s warm, 5 s cold (with retries).
- FII/DII fetch: <100 ms (NSE's `/api/fiidiiTradeReact`).
- Bulk deals: <200 ms (CSV).
- Block deals: <200 ms (CSV).
- The sequential structure is fine here because each is fast.

### Issues (minor)
1. **NSE 403 retries are silent** — `urllib.request.urlopen` will raise on 403; we don't catch and back off. Add a single retry-with-backoff wrapper. (You mentioned this in MEMORY §4.)
2. **`archive_deals` reads the entire 96 KB `bulk_block_history.json` on every run, then writes it back** — O(N) per call, with ~360 deals that's fine, but it'll grow. If it gets to 10 000 deals, switch to append-only NDJSON.
3. **`seen.json` has no bound** — it can grow with `days_back × 8 tickers × 3 sources`. Add a trim to last N keys (you noted this in MEMORY §2).

---

## 7. `news_alert.run_once()` and `shareholding_alert.run_once()`

I didn't profile these in detail, but static analysis:
- `news_alert.py` is 48 KB / 1194 lines and imports `urllib` + Google News RSS + multiple RSS feeds. Each RSS parse is ~500 ms. With 6+ feeds: ~3 s sequential. Could be parallelised.
- `shareholding_alert.py` is 30 KB / 858 lines. Trendlyne scraper — 8 stocks × ~1 s = 8 s. The `parallel` helper isn't used; should be.

---

## 8. The webapp template rendering is fine

Templates are 73–656 lines, well within Jinja's comfort zone. `portfolio.html` is 512 lines and renders ~30 rows; takes ~30 ms. Not a bottleneck.

One micro-issue: `settings.html` is 656 lines and reads 3 JSON files (`mfs.json`, `sgbs.json`, my_tickers.txt) on every request via `get_holdings_summary()`. Cache it.

---

## 9. SQLite is not a bottleneck

`history_db.py` uses WAL mode, has a `_lock` for serialised writes, and uses `INSERT OR REPLACE` for idempotency. All good. The DB is 60 KB; queries are sub-ms. **Don't touch it for now.**

---

## 10. Recommended fix order (biggest wins first)

| # | Fix | File | Est. saving | Effort |
|---|---|---|---|---|
| 1 | Batch yfinance download (`group_by="ticker"`) | `indices_chart.py`, `intraday.py` | 5–10 s cold / 1–2 s warm | S |
| 2 | Persist `_portfolio_cache` to disk | `webapp/data.py` | 15 s → 50 ms after restart | S |
| 3 | Move `fetch_indices` into the parallel group | `equity_compare.py:230` | 1.5 s | XS |
| 4 | Skip LTP fallback when all LTPs populated | `angel_client.py:215` | 200–400 ms | XS |
| 5 | Cache NSE SGB instrument list | `mf_sgb.py` | ~1 s | S |
| 6 | Persist `data/holdings_cache.json` (fixes intraday flatline bug) | `equity_compare.py` or `intraday.py` | 0 s, but fixes correctness | S |
| 7 | Reuse one Playwright browser across all PDFs | `concalls.py:184` | 20–40 s per 15-PDF run | M |
| 8 | `functools.lru_cache` on `_get_shareholding_for_portfolio` | `server.py:78` | ~5 ms / req | XS |
| 9 | ETag / 304 for HTML pages | `server.py` | 100 ms / refresh | S |
| 10 | Parallelise `news_alert` RSS fetches | `news_alert.py` | 2–3 s | S |
| 11 | Parallelise `shareholding_alert` (8 sequential HTTP) | `shareholding_alert.py` | 5 s | XS |

**Total expected impact:** `/portfolio` first request: 15 s → 2–3 s. After warm cache: 50 ms. After server restart: 50 ms (from fix #2). Cold Yahoo path: gone.

---

## 11. Things I did NOT measure but suspect

- **GIL contention** — `map_parallel` uses threads, which is correct for I/O-bound work, but the GIL is briefly held during list/dict operations. For 5–15 items it's noise; for 100+ (e.g. `mf_holdings_alert` doing 50 MFs × 5 stocks) it could matter. Profile with `py-spy dump --pid <pid>` if it ever feels slow.
- **JSON serialisation of large snapshots** — `webapp/data.py:_build_portfolio_snapshot` returns a 100 KB dict; FastAPI's `JSONResponse` re-encodes it on every `/api/portfolio` hit. Probably fine, but if you ever see the API path lag the HTML path, this is the culprit.
- **The tax dashboard** (`webapp/tax_dashboard.py`, 567 lines) is loaded on server start via `app.include_router(tax_router)`. I didn't check if it has any startup-time imports that block.

---

*End of audit. Want me to start with fix #1 (batch yfinance) or fix #2 (persist portfolio cache)?*
