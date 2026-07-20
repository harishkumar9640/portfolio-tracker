"""
pipeline.purge_ircon
-------------------
One-shot utility to remove IRCON from all on-disk caches and snapshots.

Why: when IRCON was sold on 2026-07-02, the CODE was updated to remove
IRCON from TICKER_MAP, sector_mechanisms, etc. — but the JSON caches
(disk snapshots of what the live fetchers returned previously) still
contain IRCON. The webapp reads from these caches, so the user saw
IRCON on the MF Holdings Trend and Shareholding Pattern pages even
though the code was correct.

This script strips IRCON from every data file under data/ that contains
it. It is safe to run multiple times; once IRCON is gone, the script
becomes a no-op.

Usage:
  python -m pipeline.purge_ircon
  python -m pipeline.purge_ircon --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data"


def _strip_from_obj(obj):
    """Recursively remove 'IRCON' keys and IRCON references from a nested dict/list.

    IRCON is removed when:
      - The key is exactly 'IRCON' (or starts with 'IRCON_' or 'IRCON-' to catch
        variants like 'IRCON_INTL', 'IRCON-EQ')
      - The string value equals 'IRCON' (exact, not substring)
    """
    def _is_ircon_key(k: str) -> bool:
        if k == "IRCON":
            return True
        # Match variants like 'IRCON-EQ' (broker symbol), 'IRCON_INTL',
        # 'IRCON_27-May-2025' (file name), but NOT 'IRONCON' (different word)
        if k.startswith("IRCON_") or k.startswith("IRCON-"):
            return True
        return False

    def _is_ircon_value(v) -> bool:
        if not isinstance(v, str):
            return False
        if v == "IRCON" or v == "IRCON-EQ":
            return True
        if v.startswith("IRCON_") or v.startswith("IRCON-"):
            return True
        return False

    if isinstance(obj, dict):
        return {k: _strip_from_obj(v) for k, v in obj.items()
                if not _is_ircon_key(k) and not _is_ircon_value(v)}
    elif isinstance(obj, list):
        return [_strip_from_obj(v) for v in obj
                if not _is_ircon_value(v)]
    else:
        return obj


def _file_has_ircon(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return False
    return "IRCON" in text


def _safe_rel(path: Path) -> str:
    """Return path relative to PROJECT, or str(path) if outside."""
    try:
        return str(path.relative_to(PROJECT))
    except ValueError:
        return str(path)


def _process_json(path: Path, dry_run: bool) -> bool:
    """Process a JSON file. Returns True if changed."""
    if not _file_has_ircon(path):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    new_data = _strip_from_obj(data)
    if new_data == data:
        return False
    if not dry_run:
        path.write_text(json.dumps(new_data, indent=2, default=str))
    print(f"  {'[DRY] ' if dry_run else ''}stripped IRCON from {_safe_rel(path)}")
    return True


def _process_text(path: Path, dry_run: bool) -> bool:
    """Process a text/log file: just remove lines containing 'IRCON'."""
    if not _file_has_ircon(path):
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    new_lines = [ln for ln in text.splitlines(keepends=True) if "IRCON" not in ln]
    if "".join(new_lines) == text:
        return False
    if not dry_run:
        path.write_text("".join(new_lines))
    print(f"  {'[DRY] ' if dry_run else ''}stripped IRCON lines from {_safe_rel(path)}")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="Purge IRCON from on-disk caches")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change, don't write anything")
    args = p.parse_args()

    if not DATA.exists():
        print(f"data/ not found at {DATA}")
        return 1

    # Find every file under data/ that contains IRCON
    candidates = [p for p in DATA.rglob("*") if p.is_file() and _file_has_ircon(p)]

    # PROTECTED PATHS — files that must NEVER be purged because they
    # are historical records (tax filings, audit logs, alert log subjects)
    # rather than current state. A purge of these would destroy the user's
    # tax history or event audit trail.
    def _is_protected(p: Path) -> bool:
        """Decide if a file must never be touched by the purge.

        PROTECTED paths:
          - data/tax_pnl/*.xlsx — Angel One tax statements, legal records.
            IRCON's P&L in 2024-25 is part of the user's tax history and
            must stay even after the position is sold.
        """
        try:
            rel = str(p.relative_to(PROJECT))
        except ValueError:
            # Outside project — don't touch (e.g. test files in /tmp).
            return True
        # Match the data/tax_pnl/ segment anywhere in the path (not just at start,
        # so the protection also covers test fixtures placed under a project subdir).
        if "data/tax_pnl" in rel and p.suffix == ".xlsx":
            return True
        return False

    protected = [p for p in candidates if _is_protected(p)]
    if protected:
        print("⚠ Refusing to touch the following protected paths (historical records):")
        for p in protected:
            print(f"  - {_safe_rel(p)}")
        candidates = [p for p in candidates if p not in protected]

    if not candidates:
        print("✓ No purgeable IRCON references found (all were in protected paths or none exist).")
        return 0

    # Process JSON files (data caches) by stripping IRCON
    # Process text files (logs) by removing lines containing IRCON
    # BUT: keep historical email subjects in alert logs intact — those are
    # records of past events, not current state.
    json_files = [p for p in candidates if p.suffix == ".json" and "log.json" not in p.name]
    other_files = [p for p in candidates if p.suffix != ".json"]

    print(f"Found {len(json_files)} JSON files + {len(other_files)} other files containing IRCON:")
    changed = 0
    for p in json_files:
        if _process_json(p, dry_run=args.dry_run):
            changed += 1
    for p in other_files:
        if _process_text(p, dry_run=args.dry_run):
            changed += 1

    print()
    if changed == 0:
        print("No structural changes (only line-removals in logs, or no diff).")
    elif args.dry_run:
        print(f"[DRY-RUN] Would change {changed} file(s). Re-run without --dry-run to apply.")
    else:
        print(f"✓ Purged IRCON from {changed} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
