"""
Tests for the Plotly HTML overlay module (portfolio_html).
We don't render the HTML in tests; we just exercise the data layer.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A SQLite DB with 30 days of fake-but-plausible snapshots."""
    from history_db import HistoryDB
    db = HistoryDB(tmp_path / "test.db")
    base_e, base_m, base_s = 500_000.0, 350_000.0, 30_000.0
    for i in range(30, 0, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        # Use deterministic values (no random) so tests are stable
        e = base_e + i * 100
        m = base_m + i * 50
        s = base_s + i * 5
        db.record_snapshot(d, "equity", e, e / 1.005, 0.5)
        db.record_snapshot(d, "mf",     m, m / 1.003, 0.3)
        db.record_snapshot(d, "sgb",    s, s / 1.002, 0.2)
        db.record_snapshot(d, "total",  e + m + s, (e + m + s) / 1.003, 0.4)
    return db


class TestPortfolioValueSeries:
    def test_returns_series_with_all_kinds(self, seeded_db):
        # Patch the module-level DB_FILE so portfolio_value_series uses our test DB
        import portfolio_html
        portfolio_html.HistoryDB = lambda *a, **kw: seeded_db
        s = portfolio_html.portfolio_value_series(
            days=60, include=("equity", "mf", "sgb")
        )
        assert s is not None
        assert len(s) == 30
        assert all(v > 0 for v in s.values)

    def test_handles_missing_kind_gracefully(self, seeded_db):
        # Delete the SGB snapshots to simulate "we don't track SGBs yet"
        with seeded_db._tx() as c:
            c.execute("DELETE FROM portfolio_snapshot WHERE kind='sgb'")
        import portfolio_html
        portfolio_html.HistoryDB = lambda *a, **kw: seeded_db
        s = portfolio_html.portfolio_value_series(
            days=60, include=("equity", "mf", "sgb")
        )
        assert s is not None
        assert all(v > 0 for v in s.values)

    def test_returns_none_for_empty_db(self, tmp_path: Path):
        from history_db import HistoryDB
        empty_db = HistoryDB(tmp_path / "empty.db")
        import portfolio_html
        portfolio_html.HistoryDB = lambda *a, **kw: empty_db
        s = portfolio_html.portfolio_value_series(days=60)
        assert s is None

    def test_respects_days_window(self, seeded_db):
        import portfolio_html
        portfolio_html.HistoryDB = lambda *a, **kw: seeded_db
        s_short = portfolio_html.portfolio_value_series(days=10, include=("equity",))
        s_long = portfolio_html.portfolio_value_series(days=60, include=("equity",))
        assert s_short is not None
        assert s_long is not None
        assert len(s_long) >= len(s_short)


class TestRenderHtml:
    def test_render_html_smoke(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Render a tiny chart and confirm it writes to disk."""
        import portfolio_html
        monkeypatch.setattr(portfolio_html, "CHARTS_DIR", tmp_path)
        idx = pd.DataFrame({
            "A": [100.0, 101.0, 102.0],
            "B": [100.0, 99.0, 98.5],
        }, index=pd.to_datetime(["2026-06-20", "2026-06-21", "2026-06-22"]))
        port = pd.Series([500_000, 502_000, 504_000],
                         index=pd.to_datetime(["2026-06-20", "2026-06-21", "2026-06-22"]))
        out = portfolio_html.render_html(idx, "test", port, ("equity", "mf", "sgb"))
        assert out.exists()
        assert out.suffix == ".html"
        # Should contain plotly traces and the portfolio name
        text = out.read_text()
        assert "My Portfolio" in text
        assert "plotly" in text.lower()

    def test_render_html_no_portfolio(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import portfolio_html
        monkeypatch.setattr(portfolio_html, "CHARTS_DIR", tmp_path)
        idx = pd.DataFrame({"A": [100.0, 101.0]}, index=pd.to_datetime(["2026-06-20", "2026-06-21"]))
        out = portfolio_html.render_html(idx, "test", None, ("equity",))
        assert out.exists()
        text = out.read_text()
        # Title should hint that snapshots are missing
        assert "snapshot" in text.lower()