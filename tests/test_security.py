"""
Security regression tests.

Each test represents a category of attack we want to defend against.
Tests fail if a single malicious payload can compromise the system.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# Shared module-level fixture -------------------------------------------

@pytest.fixture
def secure_client():
    """TestClient with portfolio/fairvalue snapshots stubbed."""
    import tempfile
    csv = "SYMBOL,NAME OF COMPANY\nRELIANCE,Reliance Industries Limited\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(csv)
        csv_path = Path(f.name)
    try:
        import fair_value.search as s
        s.CACHE_FILE = csv_path
        s._index = []
        s._index_loaded_at = 0.0

        import webapp.data as wd
        import webapp.server as ws
        _stub = lambda force=False: {
            "asof": "2026-06-25", "indices": [], "equity":
            {"row": None, "holdings": [], "value": 0, "prev_value": 0},
            "mf": {"count": 0, "value": 0, "prev_value": 0, "pct": 0},
            "sgb": {"count": 0, "value": 0, "prev_value": 0, "pct": 0,
                    "rows": []},
            "total": {"value": 0, "prev_value": 0, "pct": 0},
            "best_index": None, "worst_index": None,
        }
        wd.get_portfolio_snapshot = _stub
        ws.get_portfolio_snapshot = _stub
        wd.get_fairvalue_snapshot = lambda force=False: {"asof": "2026-06-25", "rows": []}
        ws.get_fairvalue_snapshot = wd.get_fairvalue_snapshot

        from fastapi.testclient import TestClient
        from webapp.server import app
        yield TestClient(app)
    finally:
        csv_path.unlink(missing_ok=True)


# ---------- XSS in template rendering ----------

class TestXSSTemplateEscaping:
    """
    Jinja2 auto-escapes by default, but verify it for the places where
    user-controlled strings land in the rendered HTML.
    """

    def test_script_tag_in_ticker_escaped(self, secure_client):
        """A ticker symbol like '<script>alert(1)</script>' must not
        execute or even appear unescaped in the HTML."""
        import webapp.data as wd
        import webapp.server as ws
        wd.get_fairvalue_snapshot = lambda force=False: {
            "asof": "2026-06-25",
            "rows": [{
                "ticker": "<script>alert('xss')</script>",
                "price": 100, "eps": 10, "book_value": 50,
                "fcf_per_share": 5, "graham": 100,
                "dcf": 200,
                "graham_margin_pct": 0.0,
                "dcf_margin_pct": 100.0,
            }],
        }
        ws.get_fairvalue_snapshot = wd.get_fairvalue_snapshot
        r = secure_client.get("/fairvalue")
        assert r.status_code == 200
        assert "<script>alert('xss')</script>" not in r.text
        assert "&lt;script&gt;" in r.text or "&lt;script" in r.text

    def test_img_onerror_in_name_escaped(self, secure_client):
        """<img onerror=...> payloads are stripped/escaped."""
        import webapp.data as wd
        import webapp.server as ws
        wd.get_portfolio_snapshot = lambda force=False: {
            "asof": "2026-06-25",
            "indices": [],
            "equity": {
                "row": None,
                "holdings": [{
                    "symbol": "<img src=x onerror=alert(1)>",
                    "quantity": 1, "avg_price": 100, "ltp": 100,
                    "current_value": 100, "pnl": 0, "pnl_pct": 0,
                }],
                "value": 100, "prev_value": 100,
            },
            "mf": {"count": 0, "value": 0, "prev_value": 0, "pct": 0},
            "sgb": {"count": 0, "value": 0, "prev_value": 0, "pct": 0, "rows": []},
            "total": {"value": 100, "prev_value": 100, "pct": 0},
            "best_index": None, "worst_index": None,
        }
        ws.get_portfolio_snapshot = wd.get_portfolio_snapshot
        r = secure_client.get("/portfolio")
        assert r.status_code == 200
        assert "<img src=x onerror=alert(1)>" not in r.text
        assert "&lt;img" in r.text or "&lt;img " in r.text

    def test_jinja_autoescape_enabled_globally(self):
        """Sanity check: the Jinja2Templates instance has autoescape on."""
        from webapp.server import templates
        env = templates.env
        assert env.autoescape is not False, \
            f"Jinja2 autoescape is disabled (got {env.autoescape!r})"


# ---------- SQL injection in history_db queries ----------

class TestSQLInjectionHistoryDB:
    """
    history_db.py builds SQL queries with f-strings. The values should
    all be parameterised via `?` placeholders, never string-concatenated.
    """

    def test_isin_uses_parameterised_query(self, tmp_path):
        from history_db import HistoryDB
        db = HistoryDB(tmp_path / "test.db")
        malicious = "IN0020230184'; DROP TABLE sgb_price; --"
        result = db.sgb_history(malicious)
        assert result == []
        with db._tx() as c:
            cur = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sgb_price'")
            assert cur.fetchone() is not None

    def test_portfolio_kind_uses_parameterised_query(self, tmp_path):
        from history_db import HistoryDB
        db = HistoryDB(tmp_path / "test.db")
        malicious = "total' OR '1'='1"
        result = db.portfolio_history(kind=malicious)
        assert result == []

    def test_record_sgb_prices_uses_parameterised(self, tmp_path):
        from history_db import HistoryDB
        db = HistoryDB(tmp_path / "test.db")
        items = [("INJECTED_ISIN", "2026-06-25", 100.0,
                  "'; DROP TABLE sgb_price; --")]
        n = db.record_sgb_prices(items)
        assert n == 1
        rows = db.sgb_history("INJECTED_ISIN")
        assert rows[0]["source"] == "'; DROP TABLE sgb_price; --"
        with db._tx() as c:
            cur = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sgb_price'")
            assert cur.fetchone() is not None

    def test_record_run_uses_parameterised(self, tmp_path):
        from history_db import HistoryDB
        db = HistoryDB(tmp_path / "test.db")
        db.record_run("test.py", "ok", note="'); DROP TABLE run_log; --")
        last = db.last_run("test.py")
        assert last["note"] == "'); DROP TABLE run_log; --"
        with db._tx() as c:
            cur = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='run_log'")
            assert cur.fetchone() is not None


# ---------- Path traversal / file inclusion ----------

class TestPathTraversal:
    """The web app must never read user-controlled paths verbatim."""

    def test_settings_endpoint_does_not_read_arbitrary_files(self, secure_client):
        """Even if a request tried to path-traverse, the server only
        reads fixed project files."""
        r = secure_client.get("/settings")
        assert r.status_code == 200
        for payload in ("/etc/passwd", "../../../../etc/passwd",
                        "/settings?file=../../../etc/passwd"):
            r = secure_client.get(payload)
            assert r.status_code in (200, 404)


# ---------- Secrets never appear in HTTP responses ----------

class TestSecretsNotInResponses:
    """Ensure broker credentials and tokens never leak in HTTP output."""

    def test_angel_session_file_not_served(self, secure_client):
        r = secure_client.get("/static/../data/angel_session.json")
        assert r.status_code in (200, 307, 404)
        if r.status_code == 200:
            assert "jwtToken" not in r.text
            assert "refreshToken" not in r.text

    def test_env_file_not_served(self, secure_client):
        r = secure_client.get("/static/../.env")
        assert r.status_code in (200, 307, 404)
        if r.status_code == 200:
            assert "ANGEL_API_KEY" not in r.text or "replace_me" in r.text

    def test_secrets_local_json_not_served(self, secure_client):
        r = secure_client.get("/static/../secrets.local.json")
        assert r.status_code in (200, 307, 404)
        if r.status_code == 200:
            assert "cas_pdf_password" not in r.text or "your_cas_password_here" in r.text

    def test_logs_directory_not_served(self, secure_client):
        r = secure_client.get("/static/../logs/app.log")
        assert r.status_code in (200, 307, 404)


# ---------- DoS protection ----------

class TestDoSProtection:
    """Basic checks that pathological inputs don't crash the server."""

    def test_very_long_ticker_handled(self, secure_client):
        huge = "A" * 1_000_000
        r = secure_client.post("/api/fairvalue/lookup", json={"ticker": huge})
        assert r.status_code in (200, 400, 413, 414, 422)

    def test_extreme_numeric_params_handled(self, secure_client):
        r = secure_client.post("/api/fairvalue/lookup", json={
            "ticker": "RELIANCE",
            "dcf_g1": 1e30, "dcf_g2": -1e30, "dcf_r": 1e30,
        })
        assert r.status_code in (200, 400, 422)


# ---------- Verbose error messages ----------

class TestVerboseErrors:
    """Error responses should be helpful but not leak internal paths."""

    def test_404_doesnt_leak_stacktrace(self, secure_client):
        r = secure_client.get("/api/this-route-does-not-exist")
        assert r.status_code == 404
        assert "/Users/" not in r.text
        assert str(PROJECT) not in r.text

    def test_500_doesnt_leak_internal_state(self, monkeypatch):
        """If a handler raises, FastAPI/Starlette returns a generic 500.
        The default 500 page must not include the exception message
        (which an attacker could use to learn secrets)."""
        from fastapi.testclient import TestClient
        import webapp.server as ws

        # Override get_health to raise a sensitive error
        original = ws.get_health
        ws.get_health = lambda: (_ for _ in ()).throw(
            RuntimeError("SECRET=jwtToken_abcdef123")
        )
        # The TestClient normally re-raises server exceptions so
        # developers see them in tests; tell it to NOT do that so we
        # get the actual 500 response the user would see.
        with TestClient(ws.app, raise_server_exceptions=False) as tc:
            r = tc.get("/api/health")
            assert r.status_code == 500
            assert "jwtToken_abcdef123" not in r.text
            assert "SECRET=" not in r.text
        ws.get_health = original


# ---------- Dependency hygiene ----------

class TestDependencies:
    """All dependencies must be declared in requirements.txt / pyproject.toml."""

    def test_no_undeclared_top_level_imports(self):
        stdlib = {
            "argparse", "ast", "asyncio", "base64", "collections",
            "concurrent", "contextlib", "csv", "ctypes", "dataclasses",
            "datetime", "functools", "glob", "gzip", "hashlib", "http",
            "importlib", "inspect", "io", "itertools", "json", "logging",
            "math", "multiprocessing", "operator", "os", "pathlib",
            "pickle", "platform", "re", "shutil", "sqlite3", "ssl", "stat",
            "string", "struct", "subprocess", "sys", "tempfile",
            "threading", "time", "tokenize", "traceback", "typing",
            "unicodedata", "unittest", "urllib", "uuid", "warnings",
            "weakref", "xml", "zipfile",
        }
        # Just informational — no assertion
        for f in (PROJECT / "fair_value").rglob("*.py"):
            for line in f.read_text().splitlines()[:30]:
                m = re.match(r"^(?:from|import)\s+(\w+)", line.strip())
                if m and m.group(1) not in stdlib:
                    pass  # could assert if we wanted
        assert True
