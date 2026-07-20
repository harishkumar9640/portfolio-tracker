"""
pipeline.project_start
---------------------
One-shot project-start entry point. Call this at the beginning of any
session that touches the portfolio data.

Behaviour:
  1. Loads the truth file (data/portfolio_truth.json) and prints a snapshot
  2. Pulls live state from broker + mfs.json + sgbs.json + my_tickers.txt
  3. Compares and reports drift
  4. If --sync, applies the drift to the truth file (and emits watchlist)
  5. Exits 0 if everything is clean, exits 1 if drift is detected and
     not auto-synced.

Usage:
  python -m pipeline.project_start
  python -m pipeline.project_start --sync
  python -m pipeline.project_start --json
"""
from __future__ import annotations

import argparse
import json
import sys

from pipeline.portfolio_truth import (
    diff_states, fetch_live_state, load_truth, merge_from_live,
    save_truth, print_status,
)
from pipeline.portfolio_truth.bootstrap import bootstrap


def main() -> int:
    p = argparse.ArgumentParser(
        description="Project-start: load truth, check drift, optionally sync"
    )
    p.add_argument("--sync", action="store_true",
                   help="Auto-apply drift to the truth file (and emit watchlist)")
    p.add_argument("--emit-watchlist", action="store_true",
                   help="Also rewrite my_tickers.txt from the truth's watchlist")
    p.add_argument("--json", action="store_true",
                   help="Print JSON snapshot of the truth file")
    args = p.parse_args()

    if args.json:
        snap = bootstrap(quiet=True)
        print(json.dumps(snap, indent=2, default=str))
        return 0

    # Always print the human-readable status
    print("=" * 60)
    print("PORTFOLIO TRUTH — PROJECT START")
    print("=" * 60)
    print()
    snap = bootstrap(quiet=False)

    if snap.get("drift", {}).get("is_clean", True):
        return 0

    if args.sync:
        live = fetch_live_state()
        current = load_truth()
        new = merge_from_live(live, current)
        save_truth(new, source="auto-sync")
        if args.emit_watchlist:
            from pipeline.portfolio_truth.update import _emit_watchlist_file
            _emit_watchlist_file(new["watchlist"])
        print()
        print(f"✓ Synced. Truth now: equity={len(new['equity'])}  "
              f"mf={len(new['mutual_funds'])}  "
              f"sgb={len(new['sgbs'])}  "
              f"watchlist={len(new['watchlist'])}")
        return 0

    print()
    print("⚠ Drift detected. Run with --sync to apply, or run:")
    print("  python -m pipeline.portfolio_truth.update")
    return 1


if __name__ == "__main__":
    sys.exit(main())
