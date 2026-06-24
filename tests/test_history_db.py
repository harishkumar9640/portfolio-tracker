"""
Unit tests for the SQLite-backed HistoryDB.
Uses a tmp DB so production data is never touched.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from history_db import HistoryDB, migrate_legacy_json  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path: Path) -> HistoryDB:
    """Fresh DB for each test."""
    return HistoryDB(tmp_path / "test.db")


class TestHistoryDB:
    def test_schema_initialised(self, tmp_db: HistoryDB):
        with tmp_db._tx() as c:
            tables = {
                r["name"]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"sgb_price", "portfolio_snapshot", "run_log"} <= tables

    def test_record_and_query_sgb(self, tmp_db: HistoryDB):
        tmp_db.record_sgb_price("IN0020230184", "2026-06-22", 15500.0, "test")
        tmp_db.record_sgb_price("IN0020230184", "2026-06-23", 15511.0, "test")
        hist = tmp_db.sgb_history("IN0020230184")
        assert [r["date"] for r in hist] == ["2026-06-22", "2026-06-23"]
        assert hist[-1]["price"] == 15511.0

    def test_record_upserts(self, tmp_db: HistoryDB):
        tmp_db.record_sgb_price("IN0020230184", "2026-06-22", 15500.0, "v1")
        tmp_db.record_sgb_price("IN0020230184", "2026-06-22", 15510.0, "v2")
        rows = tmp_db.sgb_history("IN0020230184")
        assert len(rows) == 1
        assert rows[0]["price"] == 15510.0
        assert rows[0]["source"] == "v2"

    def test_bulk_record(self, tmp_db: HistoryDB):
        n = tmp_db.record_sgb_prices([
            ("IN0020230184", "2026-06-22", 15500.0, "x"),
            ("IN0020230184", "2026-06-23", 15511.0, "x"),
            ("IN0020230069", "2026-06-22", 15198.0, "x"),
        ])
        assert n == 3
        assert len(tmp_db.sgb_history("IN0020230184")) == 2
        assert len(tmp_db.sgb_history("IN0020230069")) == 1

    def test_sgb_prev_price(self, tmp_db: HistoryDB):
        tmp_db.record_sgb_price("IN0020230184", "2026-06-22", 15500.0)
        tmp_db.record_sgb_price("IN0020230184", "2026-06-23", 15511.0)
        # Most recent strictly before 2026-06-23
        price, d = tmp_db.sgb_prev_price("IN0020230184", before="2026-06-23")
        assert price == 15500.0
        assert d == "2026-06-22"

    def test_sgb_prev_price_no_data(self, tmp_db: HistoryDB):
        price, d = tmp_db.sgb_prev_price("IN0029999999", before="2026-06-23")
        assert price is None and d is None

    def test_sgb_history_days_filter(self, tmp_db: HistoryDB):
        today = date.today()
        for i in range(40):
            d = (today - timedelta(days=i)).isoformat()
            tmp_db.record_sgb_price("IN0020230184", d, 15000.0 + i)
        rows = tmp_db.sgb_history("IN0020230184", days=10)
        assert len(rows) <= 11   # today + last 10 days

    def test_portfolio_snapshot(self, tmp_db: HistoryDB):
        tmp_db.record_snapshot("2026-06-22", "total", 1_000_000, 990_000, 1.01)
        tmp_db.record_snapshot("2026-06-23", "total", 1_010_000, 1_000_000, 1.00)
        hist = tmp_db.portfolio_history("total")
        assert len(hist) == 2
        assert hist[1]["value"] == 1_010_000
        # Upsert
        tmp_db.record_snapshot("2026-06-23", "total", 1_020_000, 1_000_000, 2.00)
        hist = tmp_db.portfolio_history("total")
        assert len(hist) == 2
        assert hist[1]["value"] == 1_020_000

    def test_run_log(self, tmp_db: HistoryDB):
        tmp_db.record_run("equity_compare.py", "ok", "all good")
        last = tmp_db.last_run("equity_compare.py")
        assert last is not None
        assert last["status"] == "ok"
        assert "all good" in last["note"]
        assert tmp_db.last_run("never_ran.py") is None

    def test_last_run_returns_most_recent(self, tmp_db: HistoryDB):
        tmp_db.record_run("x.py", "ok", "first")
        tmp_db.record_run("x.py", "fail", "second")
        last = tmp_db.last_run("x.py")
        assert last["status"] == "fail"

    def test_last_run_unknown_script(self, tmp_db: HistoryDB):
        assert tmp_db.last_run("never.py") is None

    def test_thread_safety(self, tmp_db: HistoryDB):
        import threading
        errors: list[Exception] = []

        def worker(i: int):
            try:
                tmp_db.record_sgb_price(f"IN00000{i:04d}", "2026-06-23", 100.0 + i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(tmp_db.sgb_history("IN000000000")) == 1


class TestLegacyMigration:
    def test_migrate_from_json(self, tmp_path: Path, tmp_db: HistoryDB):
        legacy = tmp_path / "legacy.json"
        legacy.write_text(
            '{"IN0020230184": {"2026-06-22": 15500.0, "2026-06-23": 15511.0},'
            ' "IN0020230069": {"2026-06-22": 15198.0}}'
        )
        # monkey-patch the module-level path
        import history_db
        original_path = history_db.PROJECT
        history_db.PROJECT = tmp_path
        try:
            n = history_db.migrate_legacy_json(legacy)
        finally:
            history_db.PROJECT = original_path
        assert n == 3