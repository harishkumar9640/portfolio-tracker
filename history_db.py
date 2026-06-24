"""
history_db.py
-------------
SQLite-backed time-series store for portfolio data.

Why SQLite:
  - One file, no server, no install. Ships with Python stdlib.
  - Indexed, transactional, atomic writes — safe under concurrent scripts.
  - Easy to query (e.g. "give me my portfolio value for the last 30 days").
  - Trivially exportable to CSV via the sqlite3 CLI.

Replaces:
  - data/sgb_price_history.json (unbounded, manual dedupe, full rewrites)

Tables:
  - sgb_price(isin, date, price) — daily LTP per SGB ISIN
  - portfolio_snapshot(date, kind, value, prev_value, pct) — equity/MF/SGB/total
    recorded each run, so we can plot your portfolio over time.

Usage:
    from history_db import HistoryDB
    db = HistoryDB()
    db.record_sgb_price("IN0020230184", "2026-06-24", 15511.42)
    rows = db.sgb_history("IN0020230184", days=30)
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Iterable

from logging_setup import get_logger

log = get_logger("history")

PROJECT = Path(__file__).resolve().parent
DB_FILE = PROJECT / "data" / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sgb_price (
    isin     TEXT    NOT NULL,
    date     TEXT    NOT NULL,    -- ISO YYYY-MM-DD
    price    REAL    NOT NULL,
    source   TEXT,                -- mintbyte, manual, ibja, nse
    PRIMARY KEY (isin, date)
);
CREATE INDEX IF NOT EXISTS idx_sgb_date ON sgb_price(date);

CREATE TABLE IF NOT EXISTS portfolio_snapshot (
    date        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,   -- 'equity' | 'mf' | 'sgb' | 'total'
    value       REAL    NOT NULL,
    prev_value  REAL    NOT NULL,
    pct         REAL    NOT NULL,
    PRIMARY KEY (date, kind)
);
CREATE INDEX IF NOT EXISTS idx_ps_date ON portfolio_snapshot(date);

CREATE TABLE IF NOT EXISTS run_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at       TEXT    NOT NULL,   -- ISO timestamp
    script       TEXT    NOT NULL,
    status       TEXT    NOT NULL,   -- 'ok' | 'partial' | 'fail'
    note         TEXT
);
"""


class HistoryDB:
    """Thin SQLite wrapper with serialised writes (sqlite3 is process-safe
    but we'd rather avoid the busy-handler dance)."""

    _lock = threading.Lock()

    def __init__(self, path: Path | str = DB_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets multiple threads share a connection.
        # We still serialise writes via self._lock.
        self._conn = sqlite3.connect(
            str(self.path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            isolation_level=None,   # autocommit; we manage txns explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # ---------- low-level ----------
    @contextmanager
    def _tx(self):
        with self._lock, self._conn:
            yield self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- SGB prices ----------
    def record_sgb_price(
        self, isin: str, date: str, price: float, source: str = ""
    ) -> None:
        """Insert or replace one SGB price point. `date` is YYYY-MM-DD."""
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO sgb_price(isin, date, price, source) "
                "VALUES (?, ?, ?, ?)",
                (isin.upper(), date, float(price), source),
            )

    def record_sgb_prices(self, items: Iterable[tuple[str, str, float, str]]) -> int:
        """Bulk version. Each tuple is (isin, date, price, source).
        Returns the number of rows inserted/replaced."""
        rows = [(i.upper(), d, float(p), s) for i, d, p, s in items]
        if not rows:
            return 0
        with self._tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO sgb_price(isin, date, price, source) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def sgb_history(self, isin: str, days: int | None = None) -> list[dict]:
        """Return [{date, price}] for one ISIN, newest last."""
        isin = isin.upper()
        with self._tx() as c:
            if days is None:
                cur = c.execute(
                    "SELECT date, price, source FROM sgb_price "
                    "WHERE isin=? ORDER BY date ASC",
                    (isin,),
                )
            else:
                cur = c.execute(
                    "SELECT date, price, source FROM sgb_price "
                    "WHERE isin=? AND date >= date('now', ?) "
                    "ORDER BY date ASC",
                    (isin, f"-{int(days)} days"),
                )
            return [dict(r) for r in cur.fetchall()]

    def sgb_prev_price(self, isin: str, before: str | None = None) -> tuple[float | None, str | None]:
        """Most recent SGB price strictly before `before` (defaults to today).
        Returns (price, date) or (None, None)."""
        isin = isin.upper()
        if before is None:
            before = _date.today().isoformat()
        with self._tx() as c:
            cur = c.execute(
                "SELECT date, price FROM sgb_price "
                "WHERE isin=? AND date<? ORDER BY date DESC LIMIT 1",
                (isin, before),
            )
            row = cur.fetchone()
            return (float(row["price"]), row["date"]) if row else (None, None)

    # ---------- Portfolio snapshots ----------
    def record_snapshot(
        self,
        date: str,
        kind: str,
        value: float,
        prev_value: float,
        pct: float,
    ) -> None:
        """Record one (date, kind) row. Overwrites if the pair exists."""
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO portfolio_snapshot"
                "(date, kind, value, prev_value, pct) VALUES (?, ?, ?, ?, ?)",
                (date, kind, float(value), float(prev_value), float(pct)),
            )

    def portfolio_history(self, kind: str = "total", days: int | None = None) -> list[dict]:
        """Return [{date, value, prev_value, pct, kind}] for one kind, oldest first."""
        with self._tx() as c:
            if days is None:
                cur = c.execute(
                    "SELECT date, value, prev_value, pct FROM portfolio_snapshot "
                    "WHERE kind=? ORDER BY date ASC",
                    (kind,),
                )
            else:
                cur = c.execute(
                    "SELECT date, value, prev_value, pct FROM portfolio_snapshot "
                    "WHERE kind=? AND date >= date('now', ?) ORDER BY date ASC",
                    (kind, f"-{int(days)} days"),
                )
            out = []
            for r in cur.fetchall():
                d = dict(r)
                d["kind"] = kind
                out.append(d)
            return out

    # ---------- Run log ----------
    def record_run(self, script: str, status: str, note: str = "") -> None:
        from datetime import datetime
        with self._tx() as c:
            c.execute(
                "INSERT INTO run_log(ran_at, script, status, note) VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"), script, status, note),
            )

    def last_run(self, script: str) -> dict | None:
        with self._tx() as c:
            cur = c.execute(
                "SELECT ran_at, status, note FROM run_log WHERE script=? "
                "ORDER BY id DESC LIMIT 1",
                (script,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    # ---------- Maintenance ----------
    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")

    def __repr__(self) -> str:
        return f"<HistoryDB path={self.path}>"


# ---------- One-shot migration from the JSON cache ----------
def migrate_legacy_json(json_path: Path | str | None = None) -> int:
    """
    If ``data/sgb_price_history.json`` exists, import it into the SQLite DB
    and return the number of rows imported. Safe to call repeatedly.
    """
    src = Path(json_path) if json_path else PROJECT / "data" / "sgb_price_history.json"
    if not src.exists():
        return 0
    import json as _json
    raw = _json.loads(src.read_text())
    items: list[tuple[str, str, float, str]] = []
    for isin, dates in raw.items():
        for d, p in dates.items():
            items.append((isin, d, float(p), "migrated"))
    if not items:
        return 0
    db = HistoryDB()
    n = db.record_sgb_prices(items)
    log.info("migrated %d SGB price rows from %s", n, src.name)
    # Rename the legacy file so we don't migrate twice.
    backup = src.with_suffix(".json.migrated")
    if not backup.exists():
        src.rename(backup)
        log.info("renamed legacy cache to %s", backup.name)
    return n


@dataclass
class _T:
    isin: str
    days: int


# Tiny CLI: `python3 history_db.py migrate`
if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    db = HistoryDB()
    if cmd == "migrate":
        n = migrate_legacy_json()
        print(f"migrated {n} rows")
    elif cmd == "stats":
        with db._tx() as c:
            sgb = c.execute("SELECT COUNT(*) AS n FROM sgb_price").fetchone()["n"]
            ps = c.execute("SELECT COUNT(*) AS n FROM portfolio_snapshot").fetchone()["n"]
            rl = c.execute("SELECT COUNT(*) AS n FROM run_log").fetchone()["n"]
        print(f"sgb_price:        {sgb} rows")
        print(f"portfolio_snap:   {ps} rows")
        print(f"run_log:          {rl} rows")
        print(f"db file:          {db.path}")
    else:
        print(f"unknown command: {cmd}\nusage: history_db.py [migrate|stats]")