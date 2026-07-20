"""
pipeline.portfolio_monitor.run_all
----------------------------------
One-shot runner that fires all three portfolio-monitor scripts
according to their preferred cadence. Designed for cron:

  # Weekly concentration check (Mondays 10am IST)
  0 10 * * 1  /path/to/.venv/bin/python -m pipeline.portfolio_monitor.run_all --weekly

  # Monthly diagnostic (1st of month, 9am IST)
  0 9 1 * *  /path/to/.venv/bin/python -m pipeline.portfolio_monitor.run_all --monthly

  # 100-day review (only fires in the ±7-day window, so just run daily)
  0 9 * * *  /path/to/.venv/bin/python -m pipeline.portfolio_monitor.run_all --review

Or run them all manually:
  python -m pipeline.portfolio_monitor.run_all --all

The scripts themselves decide whether to actually send email (the calendar
only sends in the review window or on action; concentration only sends
on breach; rebalance only sends on drift or first of month).
"""
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

from pipeline.logging_setup import get_logger

log = get_logger("portfolio_monitor.run_all")

PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "review": "pipeline/portfolio_monitor/calendar.py",
    "weekly": "pipeline/portfolio_monitor/concentration_check.py",
    "monthly": "pipeline/portfolio_monitor/rebalance_diagnostic.py",
}


def _run(script_rel: str, extra_args: list[str]) -> int:
    """Run a single monitor script as if invoked from CLI."""
    script_path = PROJECT / script_rel
    sys.argv = [str(script_path)] + extra_args
    try:
        runpy.run_path(str(script_path), run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    except Exception as e:
        log.exception("script %s failed: %s", script_path, e)
        return 1


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--review", action="store_true",
                   help="Run 100-day calendar review (BALRAMCHIN/KNRCON/UNOMINDA)")
    g.add_argument("--weekly", action="store_true",
                   help="Run weekly concentration check")
    g.add_argument("--monthly", action="store_true",
                   help="Run monthly rebalance diagnostic")
    g.add_argument("--all", action="store_true",
                   help="Run all three (manual debug)")
    p.add_argument("--force", action="store_true",
                   help="Force each script to send its email even without triggers")
    args = p.parse_args()

    results = {}
    if args.review or args.all:
        log.info("=" * 60 + "\nRunning 100-day calendar review\n" + "=" * 60)
        results["review"] = _run(SCRIPTS["review"],
                                  ["--force"] if args.force else [])
    if args.weekly or args.all:
        log.info("=" * 60 + "\nRunning weekly concentration check\n" + "=" * 60)
        results["weekly"] = _run(SCRIPTS["weekly"],
                                  ["--force"] if args.force else [])
    if args.monthly or args.all:
        log.info("=" * 60 + "\nRunning monthly rebalance diagnostic\n" + "=" * 60)
        results["monthly"] = _run(SCRIPTS["monthly"],
                                   ["--force"] if args.force else [])

    log.info("Run summary: %s", results)
    return 0 if all(v == 0 for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

