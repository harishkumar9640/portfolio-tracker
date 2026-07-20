"""Tests for pipeline.purge_ircon."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pipeline.purge_ircon import (
    _file_has_ircon, _process_json, _process_text, _strip_from_obj, main,
)


# ---------- _strip_from_obj ----------

def test_strip_simple_dict():
    d = {"RELIANCE": 1, "IRCON": 2, "UNOMINDA": 3}
    out = _strip_from_obj(d)
    assert "IRCON" not in out
    assert out == {"RELIANCE": 1, "UNOMINDA": 3}


def test_strip_nested_dict():
    d = {
        "equity": {
            "RELIANCE": {"qty": 60},
            "IRCON": {"qty": 0},  # zero-qty IRCON history
            "data": {"reference_prices": {"IRCON": 1234, "UNOMINDA": 1234}},
        }
    }
    out = _strip_from_obj(d)
    assert "IRCON" not in str(out)
    assert out["equity"]["data"]["reference_prices"] == {"UNOMINDA": 1234}


def test_strip_list():
    d = ["RELIANCE", "IRCON", "UNOMINDA"]
    out = _strip_from_obj(d)
    assert out == ["RELIANCE", "UNOMINDA"]


def test_strip_does_not_remove_substrings():
    # "IRONCON" is a different word — should be kept.
    # "IRCON_EQUITY" is IRCON with a suffix — should be removed (we want all IRCON variants gone).
    d = {"IRONCON": 1, "IRCON_EQUITY": 2, "RELIANCE": 3}
    out = _strip_from_obj(d)
    assert "IRONCON" in out
    assert "IRCON_EQUITY" not in out
    assert "RELIANCE" in out


def test_strip_preserves_others():
    d = {"RELIANCE": 1, "UNOMINDA": 2, "BANKBARODA": 3}
    out = _strip_from_obj(d)
    assert out == d


# ---------- _file_has_ircon ----------

def test_file_has_ircon_true(tmp_path):
    f = tmp_path / "data.json"
    f.write_text('{"ticker": "IRCON"}')
    assert _file_has_ircon(f) is True


def test_file_has_ircon_false(tmp_path):
    f = tmp_path / "data.json"
    f.write_text('{"ticker": "RELIANCE"}')
    assert _file_has_ircon(f) is False


def test_file_has_ircon_missing_file(tmp_path):
    f = tmp_path / "missing.json"
    assert _file_has_ircon(f) is False


# ---------- _process_json ----------

def test_process_json_strips_ircon(tmp_path):
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"RELIANCE": 1, "IRCON": 2, "UNOMINDA": 3}))
    changed = _process_json(f, dry_run=False)
    assert changed is True
    data = json.loads(f.read_text())
    assert "IRCON" not in data


def test_process_json_dry_run_does_not_modify(tmp_path):
    f = tmp_path / "data.json"
    original = '{"RELIANCE": 1, "IRCON": 2}'
    f.write_text(original)
    changed = _process_json(f, dry_run=True)
    assert changed is True
    assert f.read_text() == original  # unchanged


def test_process_json_no_ircon_no_change(tmp_path):
    f = tmp_path / "data.json"
    f.write_text('{"RELIANCE": 1}')
    changed = _process_json(f, dry_run=False)
    assert changed is False


def test_process_json_invalid_json(tmp_path):
    f = tmp_path / "data.json"
    f.write_text('not json {{{')
    changed = _process_json(f, dry_run=False)
    assert changed is False  # gracefully skipped


# ---------- _process_text ----------

def test_process_text_removes_ircon_lines(tmp_path):
    f = tmp_path / "app.log"
    f.write_text("2026-07-02 INFO: IRCON sold\n2026-07-02 INFO: RELIANCE steady\n")
    changed = _process_text(f, dry_run=False)
    assert changed is True
    text = f.read_text()
    assert "IRCON" not in text
    assert "RELIANCE" in text


def test_process_text_preserves_other_lines(tmp_path):
    f = tmp_path / "app.log"
    f.write_text("line 1\nline 2\nline 3\n")
    changed = _process_text(f, dry_run=False)
    assert changed is False


# ---------- main() ----------

def test_main_dry_run_clean(tmp_path, monkeypatch, capsys):
    # Put tmp_path inside PROJECT so relative_to works
    inside = PROJECT / "tmp_test_purge_ircon"
    inside.mkdir(exist_ok=True)
    test_data = inside / "data"
    test_data.mkdir(exist_ok=True)
    (test_data / "clean.json").write_text('{"x": 1}')

    monkeypatch.setattr("pipeline.purge_ircon.DATA", test_data)
    monkeypatch.setattr("sys.argv", ["purge_ircon", "--dry-run"])
    rc = main()
    out = capsys.readouterr().out
    assert "No IRCON references" in out or "No purgeable IRCON" in out or "[DRY]" in out
    assert rc == 0

    # Cleanup
    import shutil
    shutil.rmtree(inside, ignore_errors=True)


def test_main_finds_and_purges_ircon(tmp_path, monkeypatch, capsys):
    # Put tmp_path inside PROJECT so relative_to works
    inside = PROJECT / "tmp_test_purge_ircon"
    inside.mkdir(exist_ok=True)
    test_data = inside / "data"
    cache = test_data / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / "mf_holdings_cache.json"
    target.write_text(json.dumps({"RELIANCE": 1, "IRCON": 2}))

    monkeypatch.setattr("pipeline.purge_ircon.DATA", test_data)
    monkeypatch.setattr("sys.argv", ["purge_ircon"])
    rc = main()
    out = capsys.readouterr().out
    assert "Purged IRCON" in out or "stripped IRCON" in out
    assert rc == 0

    data = json.loads(target.read_text())
    assert "IRCON" not in data
    assert "RELIANCE" in data

    # Cleanup
    import shutil
    shutil.rmtree(inside, ignore_errors=True)


def test_main_protects_tax_pnl_directory(monkeypatch, capsys):
    """The tax PNL xlsx files are historical tax records and must NEVER
    be purged, even if they contain 'IRCON' (which they should — IRCON
    was a real holding that generated real P&L)."""
    # Use a real project-internal tmp dir
    inside = PROJECT / "tmp_test_purge_ircon"
    if inside.exists():
        import shutil
        shutil.rmtree(inside)
    inside.mkdir()
    test_data = inside / "data"
    test_data.mkdir()
    tax_xlsx = test_data / "tax_pnl" / "Tax PNL 2025-26.xlsx"
    tax_xlsx.parent.mkdir(parents=True)
    original_bytes = b"PK\x03\x04IRCON_synthetic_data_in_xlsx"
    tax_xlsx.write_bytes(original_bytes)

    monkeypatch.setattr("pipeline.purge_ircon.DATA", test_data)
    monkeypatch.setattr("sys.argv", ["purge_ircon"])
    rc = main()
    out = capsys.readouterr().out
    assert "Refusing to touch" in out
    assert "tax_pnl" in out
    assert rc == 0
    # File is unchanged
    assert tax_xlsx.read_bytes() == original_bytes

    # Cleanup
    import shutil
    shutil.rmtree(inside, ignore_errors=True)
