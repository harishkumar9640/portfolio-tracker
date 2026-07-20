"""
pipeline.portfolio_truth.bootstrap
-----------------------------------
Project-start hook. Run this on every fresh session to ensure the
truth file is current and consistent.

The user asked for the pattern:
  "Every time I ask the project to run, the project should start with
   the txt file that holds the info of my holdings."

This module is the AI-facing entry point. Calling bootstrap() at the
start of any project run does the following:

  1. Loads the truth file (data/portfolio_truth.json)
  2. Pulls live state from broker + mfs.json + sgbs.json + my_tickers.txt
  3. Reports drift (if any). Does NOT auto-write.
  4. Returns a structured "what I know about your portfolio" dict
     for the AI to consume in context.

Usage (programmatic):
    from pipeline.portfolio_truth.bootstrap import bootstrap
    snapshot = bootstrap()
    # snapshot["equity"]    -> {TICKER: {qty, avg_price, ...}}
    # snapshot["mutual_funds"] -> {scheme_name: {units, ...}}
    # snapshot["sgbs"]      -> {isin: {units, ...}}
    # snapshot["watchlist"] -> [TICKER, ...]
    # snapshot["drift"]     -> diff result, or None if clean
    # snapshot["asof"]      -> ISO timestamp of truth file

Usage (CLI):
    python -m pipeline.portfolio_truth.bootstrap
    python -m pipeline.portfolio_truth.bootstrap --quiet
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from pipeline import portfolio_truth

log = logging.getLogger("portfolio_truth.bootstrap")


def bootstrap(*, force_init: bool = False, quiet: bool = False) -> dict:
    """
    Project-start hook. Returns a snapshot dict of the user's portfolio,
    plus a 'drift' key showing what changed in the live state vs the
    truth file.

    The AI is expected to call this before doing anything else.

    If `force_init=True` and the truth file doesn't exist, this will
    initialise it from the live state (no prompt). Otherwise it just
    reports drift and returns the existing truth.
    """
    if not portfolio_truth.TRUTH_FILE.exists():
        if force_init:
            live = portfolio_truth.fetch_live_state()
            new_truth = portfolio_truth.merge_from_live(live, existing=None)
            portfolio_truth.save_truth(new_truth, source="bootstrap")
            if not quiet:
                print(f"✓ Bootstrapped truth file: {portfolio_truth.TRUTH_FILE}")
            return _snapshot(new_truth, drift=None)
        else:
            log.warning("truth file missing at %s; returning empty default", portfolio_truth.TRUTH_FILE)
            return {
                "asof": None,
                "source": "default",
                "equity": {},
                "mutual_funds": {},
                "sgbs": {},
                "watchlist": [],
                "drift": None,
            }

    current = portfolio_truth.load_truth()
    live = portfolio_truth.fetch_live_state()
    drift = portfolio_truth.diff_states(current, live)
    if not quiet:
        if drift["is_clean"]:
            print(f"✓ Truth file current (asof={current.get('asof')}). "
                  f"No drift.")
        else:
            print(f"⚠ Truth file drift detected. Run "
                  f"`python -m pipeline.portfolio_truth.update` to sync.")
            print(f"  drift summary:")
            for sect in ("equity", "mutual_funds", "sgbs", "watchlist"):
                d = drift[sect]
                changes = {k: v for k, v in d.items() if v}
                if changes:
                    print(f"    {sect}: {changes}")
    return _snapshot(current, drift=drift)


def _snapshot(truth: dict, drift: Optional[dict]) -> dict:
    return {
        "asof": truth.get("asof"),
        "source": truth.get("source"),
        "schema_version": truth.get("schema_version"),
        "equity": dict(truth.get("equity", {})),
        "mutual_funds": dict(truth.get("mutual_funds", {})),
        "sgbs": dict(truth.get("sgbs", {})),
        "watchlist": list(truth.get("watchlist", [])),
        "drift": drift,
    }


def print_snapshot(snap: dict) -> None:
    print(f"=== Portfolio snapshot (asof {snap['asof']}) ===")
    print(f"Source: {snap['source']}  schema_version: {snap['schema_version']}")
    print()
    print(f"Equity ({len(snap['equity'])} positions):")
    for tk, p in sorted(snap["equity"].items()):
        print(f"  {tk:<14} qty={p['qty']:>6}  avg=₹{p['avg_price']:>9.2f}  "
              f"src={p.get('source', '?')}")
    print()
    print(f"Mutual funds ({len(snap['mutual_funds'])}):")
    for name, p in sorted(snap["mutual_funds"].items()):
        print(f"  {name[:60]:<60}  units={p['units']:.2f}")
    print()
    print(f"SGBs ({len(snap['sgbs'])}):")
    for isin, p in sorted(snap["sgbs"].items()):
        print(f"  {isin}  units={p['units']}  invested/g=₹{p.get('invested_per_g', '?')}")
    print()
    print(f"Watchlist ({len(snap['watchlist'])}):")
    print(f"  {', '.join(snap['watchlist'])}")
    if snap.get("drift"):
        d = snap["drift"]
        print()
        if d.get("is_clean"):
            print("Drift: none")
        else:
            print("Drift: detected (see update.py)")


def main() -> int:
    p = argparse.ArgumentParser(description="Project-start portfolio bootstrap")
    p.add_argument("--force-init", action="store_true",
                   help="If truth file missing, create it from live state")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress output (just print JSON)")
    p.add_argument("--json", action="store_true",
                   help="Print full snapshot as JSON")
    args = p.parse_args()

    snap = bootstrap(force_init=args.force_init, quiet=args.quiet or args.json)
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    elif args.quiet:
        pass
    else:
        print_snapshot(snap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
