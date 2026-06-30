"""
Tests for scripts/pre-publish-check.sh.

We don't actually run the bash script in tests (it's a bash script, not
Python). Instead, we assert the script exists, is executable, and
contains the checks it should.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


SCRIPT = PROJECT / "data" / "scripts" / "pre-publish-check.sh"


def test_script_exists():
    assert SCRIPT.exists(), f"{SCRIPT} is missing"


def test_script_is_executable():
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "script must be executable by owner"


def test_script_exits_zero_on_clean_repo():
    """Run the script and verify exit code is 0."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"script failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Should explicitly print "Safe to push" or warnings
    assert "Safe to push" in result.stdout or "warnings" in result.stdout


def test_script_prints_expected_sections():
    """Spot-check the section headers in the script."""
    text = SCRIPT.read_text()
    for section in [
        "Tracked files containing real secrets",
        ".gitignore protection",
        "Git history clean",
        "Example files are placeholder-only",
        "No accidentally-committed large files",
    ]:
        assert section in text, f"missing section: {section}"