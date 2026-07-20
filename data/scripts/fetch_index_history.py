"""
Fetch historical daily close data for Nifty Midcap 150 and Nifty Smallcap 250
from the NSE Market Activity archives (MA*.csv).

NSE publishes one CSV per trading day containing OHLC for all ~140 NSE indices:
  https://nsearchives.nseindia.com/archives/equities/mkt/MA{ddmmyy}.csv

The Nifty 50 column is already in data/cache/indices_cache.csv. We add the
Midcap 150 and Smallcap 250 series as separate CSV files in
data/cache/indices/ so the existing pipeline.index_data loader picks them up
without any code change.

Output files:
  data/cache/indices/nifty_midcap_150.csv   (Date,Close)
  data/cache/indices/nifty_smallcap_250.csv (Date,Close)

Re-runnable: skips dates that already have a row (incremental).
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parents[2]  # data/scripts/ -> data/ -> repo
sys.path.insert(0, str(PROJECT))

INDICES_DIR = PROJECT / "data" / "cache" / "indices"
NIFTY50_CSV = PROJECT / "data" / "cache" / "indices_cache.csv"
INDICES_DIR.mkdir(parents=True, exist_ok=True)

# Market Activity MA.csv columns (NSE)
MA_URL = "https://nsearchives.nseindia.com/archives/equities/mkt/MA{ddmmyy}.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0.0.0 Safari/537.36"}

# Each index: (NSE label in MA csv, output filename)
INDICES = {
    "nifty_midcap_150": ("NIFTY MIDCAP 150", "nifty_midcap_150.csv"),
    "nifty_smallcap_250": ("NIFTY SMLCAP 250", "nifty_smallcap_250.csv"),
}


def _parse_existing(path: Path) -> set[date]:
    """Return the set of dates already present in the output CSV."""
    if not path.exists():
        return set()
    out: set[date] = set()
    with path.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                d = datetime.strptime(row["Date"], "%Y-%m-%d").date()
                out.add(d)
            except (ValueError, KeyError):
                continue
    return out


def _append_rows(path: Path, rows: list[tuple[date, float]]) -> int:
    """Append (date, close) rows to the CSV, in sorted order, de-duped.
    Returns the number of rows added (0 if nothing new)."""
    if not rows:
        return 0
    existing = _parse_existing(path)
    new_rows = sorted([(d, c) for d, c in rows if d not in existing])
    if not new_rows:
        return 0
    is_new = not path.exists()
    with path.open("a") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(["Date", "Close"])
        for d, c in new_rows:
            writer.writerow([d.isoformat(), f"{c:.2f}"])
    return len(new_rows)


def _fetch_ma_csv(d: date) -> str | None:
    """Fetch the MA{ddmmyy}.csv for a given trading day. Returns the raw
    CSV text, or None if the day is a holiday / data missing."""
    ddmmyy = d.strftime("%d%m%y")
    url = MA_URL.format(ddmmyy=ddmmyy)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        text = r.text
        # MA csv's first few lines are a summary; the index OHLC block
        # starts at the line ",INDEX,PREVIOUS CLOSE,OPEN,...". Validate.
        if "INDEX,PREVIOUS CLOSE" not in text and "INDEX,PREVIOUS" not in text:
            return None
        return text
    except Exception:
        return None


def _extract_closes(text: str) -> dict[str, float]:
    """From an MA*.csv text body, return {nse_label: close} for all indices."""
    out: dict[str, float] = {}
    # Find the start of the index OHLC block
    block_started = False
    for line in text.split("\n"):
        if "INDEX" in line and "PREVIOUS CLOSE" in line:
            block_started = True
            continue
        if not block_started:
            continue
        if not line.strip():
            continue
        # Row format: ,INDEX,PREVIOUS CLOSE,OPEN,HIGH,LOW,CLOSE,GAIN/LOSS
        # Sometimes leading column is empty
        parts = [p.strip() for p in line.split(",")]
        # Find the index name and close. Index name is the first non-empty
        # cell after the leading empty cell. Close is the 5th number.
        if len(parts) < 7:
            continue
        try:
            close = float(parts[6].replace(",", ""))
        except (ValueError, IndexError):
            continue
        # The index name is in parts[1] usually
        name = parts[1] if parts[0] == "" else parts[0]
        out[name] = close
    return out


def fetch_range(
    start: date,
    end: date,
    log: callable = print,
) -> dict[str, int]:
    """Fetch all 3 indices for the date range [start, end], append to the
    appropriate CSVs. Returns {index_name: rows_added}."""
    # First read the existing date coverage for each output file so we can
    # skip dates that are already present.
    coverage = {name: _parse_existing(INDICES_DIR / fn) for name, (_, fn) in INDICES.items()}

    # Determine all trading days to try. NSE Mon-Fri except holidays; we
    # don't know holidays so we just iterate weekdays and skip 404s.
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += timedelta(days=1)

    log(f"fetching {len(days)} candidate days ({start} → {end})")
    rows_by_index: dict[str, list[tuple[date, float]]] = {k: [] for k in INDICES}
    fetched = 0
    failed = 0
    for d in days:
        # Skip if all 3 indices already have this date
        if all(d in coverage[k] for k in INDICES):
            continue
        text = _fetch_ma_csv(d)
        if not text:
            failed += 1
            continue
        closes = _extract_closes(text)
        fetched += 1
        for index_name, (label, _fn) in INDICES.items():
            if d in coverage[index_name]:
                continue
            if label in closes:
                rows_by_index[index_name].append((d, closes[label]))
        if fetched % 50 == 0:
            log(f"  fetched={fetched}, failed={failed}, "
                f"added so far: " + ", ".join(
                    f"{k}={len(v)}" for k, v in rows_by_index.items()))
        time.sleep(0.25)

    log(f"fetched={fetched}, failed={failed}")
    added: dict[str, int] = {}
    for index_name, rows in rows_by_index.items():
        fn = INDICES[index_name][1]
        n = _append_rows(INDICES_DIR / fn, rows)
        added[index_name] = n
        log(f"  {index_name}: +{n} new rows -> {INDICES_DIR / fn}")
    return added


def main():
    # Determine date range: from 1 Jan 2022 (covers all 5 xlsx files
    # which start at FY 2022-23) up to today.
    start = date(2022, 1, 1)
    end = date.today()
    added = fetch_range(start, end, log=print)
    print(json.dumps({"added": added, "asof": end.isoformat()}, indent=2))


if __name__ == "__main__":
    main()
