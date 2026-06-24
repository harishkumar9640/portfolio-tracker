"""
Tests for the Angel One session cache (file-level logic only).
We mock the SmartAPI SDK so these run offline.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a fake .env and patch load_dotenv to be a no-op so it doesn't
    touch the real .env on disk."""
    monkeypatch.setenv("ANGEL_API_KEY", "fake_key")
    monkeypatch.setenv("ANGEL_CLIENT_CODE", "F00000")
    monkeypatch.setenv("ANGEL_MPIN", "1111")
    monkeypatch.setenv("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)


@pytest.fixture
def mock_smartconnect(monkeypatch: pytest.MonkeyPatch):
    """Replace SmartApi.SmartConnect with a controllable fake."""
    fake_sdk = MagicMock()
    fake_obj = MagicMock()
    fake_obj.generateSession.return_value = {
        "status": True,
        "data": {
            "jwtToken": "jwt_v1",
            "refreshToken": "rt_v1",
            "feedToken": "ft_v1",
        },
    }
    fake_obj.generateToken.return_value = {
        "status": True,
        "data": {"jwtToken": "jwt_v2", "feedToken": "ft_v2"},
    }
    fake_obj.access_token = "jwt_v1"
    fake_obj.refresh_token = "rt_v1"
    fake_obj.feed_token = "ft_v1"
    fake_obj.userId = "F00000"
    fake_obj.holding.return_value = {"status": True, "data": []}
    fake_sdk.return_value = fake_obj
    fake_module = MagicMock(SmartConnect=lambda api_key: fake_obj)
    sys.modules["SmartApi"] = fake_module
    return fake_obj


@pytest.fixture
def tmp_session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the session cache to a temp dir."""
    # Patch PROJECT inside angel_client
    import angel_client
    monkeypatch.setattr(angel_client, "PROJECT", tmp_path)
    monkeypatch.setattr(angel_client, "ANGEL_SESSION_FILE", tmp_path / "angel_session.json")
    return tmp_path


class TestSessionCache:
    def test_first_login_writes_session(self, fake_env, mock_smartconnect, tmp_session_dir):
        from angel_client import login
        obj = login()
        # Verify the SDK was called with generateSession (fresh login)
        assert mock_smartconnect.generateSession.called
        # Verify the session file was written
        session_file = tmp_session_dir / "angel_session.json"
        assert session_file.exists()
        data = json.loads(session_file.read_text())
        assert data["jwtToken"] == "jwt_v1"
        assert data["refreshToken"] == "rt_v1"
        assert data["client_code"] == "F00000"
        assert data["logged_in_at"] > 0

    def test_second_login_uses_cache(
        self, fake_env, mock_smartconnect, tmp_session_dir, monkeypatch
    ):
        # Pre-seed a valid session file
        session_file = tmp_session_dir / "angel_session.json"
        session_file.write_text(json.dumps({
            "jwtToken": "jwt_cached",
            "refreshToken": "rt_cached",
            "feedToken": "ft_cached",
            "client_code": "F00000",
            "logged_in_at": int(time.time()),   # fresh
        }))
        # Reset mock so we can detect whether generateSession was called
        mock_smartconnect.generateSession.reset_mock()
        mock_smartconnect.generateToken.reset_mock()

        from angel_client import login
        obj = login()
        # generateSession should NOT be called on a cache hit
        assert not mock_smartconnect.generateSession.called
        # generateToken SHOULD be called to refresh the jwtToken
        assert mock_smartconnect.generateToken.called

    def test_expired_cache_falls_back_to_fresh_login(
        self, fake_env, mock_smartconnect, tmp_session_dir
    ):
        session_file = tmp_session_dir / "angel_session.json"
        session_file.write_text(json.dumps({
            "jwtToken": "jwt_old",
            "refreshToken": "rt_old",
            "feedToken": "ft_old",
            "client_code": "F00000",
            "logged_in_at": int(time.time()) - 25 * 3600,   # 25h ago
        }))

        from angel_client import login
        obj = login()
        assert mock_smartconnect.generateSession.called

    def test_broken_cache_file_falls_back(
        self, fake_env, mock_smartconnect, tmp_session_dir
    ):
        session_file = tmp_session_dir / "angel_session.json"
        session_file.write_text("not valid json {{{")

        from angel_client import login
        obj = login()
        assert mock_smartconnect.generateSession.called

    def test_generate_token_failure_falls_back(
        self, fake_env, mock_smartconnect, tmp_session_dir
    ):
        session_file = tmp_session_dir / "angel_session.json"
        session_file.write_text(json.dumps({
            "jwtToken": "jwt_cached",
            "refreshToken": "rt_cached",
            "client_code": "F00000",
            "logged_in_at": int(time.time()),
        }))
        mock_smartconnect.generateToken.side_effect = RuntimeError("Invalid Token")
        from angel_client import login
        obj = login()
        # Should have fallen back to a fresh login
        assert mock_smartconnect.generateSession.called

    def test_session_load_rejects_missing_jwt(
        self, fake_env, mock_smartconnect, tmp_session_dir
    ):
        from angel_client import _load_session, _save_session
        # Save a session without jwtToken
        _save_session({"refreshToken": "rt", "logged_in_at": int(time.time())})
        assert _load_session() is None

    def test_session_load_rejects_expired(
        self, fake_env, mock_smartconnect, tmp_session_dir
    ):
        from angel_client import _save_session, _load_session
        _save_session({
            "jwtToken": "x", "refreshToken": "y",
            "logged_in_at": int(time.time()) - 25 * 3600,
        })
        assert _load_session() is None

    def test_session_save_is_atomic(
        self, fake_env, mock_smartconnect, tmp_session_dir
    ):
        from angel_client import _save_session
        _save_session({
            "jwtToken": "x", "refreshToken": "y",
            "logged_in_at": 1234567890,
        })
        # The .tmp file should NOT linger
        leftovers = list(tmp_session_dir.glob("*.tmp"))
        assert not leftovers
        # And the real file should be valid JSON
        data = json.loads((tmp_session_dir / "angel_session.json").read_text())
        assert data["jwtToken"] == "x"