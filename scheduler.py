"""
Unified daily-scheduler orchestrator.

Runs multiple alerts at their scheduled IST times from a single daemon
process. Replaces the per-module schedulers (each module still has its
own `start_daily_scheduler()` for backwards compat / standalone use).

Schedule:
  08:55 IST — news_alert, mf_holdings_alert, earnings_alert
  16:30 IST — mf_holdings_alert
  (others as added)

Usage:
    python scheduler.py                  # run the orchestrator
    python scheduler.py --dry-run        # env var DRY_RUN=1 for all alerts

Each module's run_once() is invoked with its own try/except so one
failing alert doesn't take down the rest.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Make sibling modules importable when run as a script
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(PROJECT_ROOT / "scheduler.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scheduler")


# ---------------------------------------------------------------------------
# Schedule: list of (hour, minute, name, callable)
# ---------------------------------------------------------------------------

def _build_schedule() -> list[tuple[int, int, str, callable]]:
    """Build the (hour, minute, label, callable) schedule list.

    Imports happen inside this function so that a missing/broken
    module doesn't prevent the orchestrator from starting.
    """
    schedule: list[tuple[int, int, str, callable]] = []

    # 08:55 IST — news digest + earnings alerts (both before market open)
    try:
        import news_alert
        schedule.append(
            (8, 55, "news_alert.run_once", news_alert.run_once)
        )
    except Exception as e:
        log.warning("could not register news_alert: %s", e)

    try:
        import earnings_alert as ea
        schedule.append(
            (8, 55, "earnings_alert.run_once",
             lambda: ea.run_once(force_send=False))
        )
    except Exception as e:
        log.warning("could not register earnings_alert: %s", e)

    # 16:30 IST — mf_holdings_alert (after market close)
    try:
        import mf_holdings_alert
        schedule.append(
            (16, 30, "mf_holdings_alert.run_once",
             lambda: mf_holdings_alert.run_once(force_email=False))
        )
    except Exception as e:
        log.warning("could not register mf_holdings_alert: %s", e)

    # 18:45 IST — flows_alert (FII/DII provisional + bulk/block deals).
    # At ~20:30 IST NSE publishes final FII/DII numbers; the history
    # file is overwritten in-place so the dashboard chart shows the
    # final number, but we don't re-alert (dedup map blocks re-send).
    try:
        import flows_alert
        schedule.append(
            (18, 45, "flows_alert.run_once",
             lambda: flows_alert.run_once(force_send=False))
        )
    except Exception as e:
        log.warning("could not register flows_alert: %s", e)

    return schedule


# ---------------------------------------------------------------------------
# Orchestrator loop
# ---------------------------------------------------------------------------

def _seconds_until_next_run(now_ist: datetime,
                             schedules: list[tuple[int, int, str, callable]]
                             ) -> tuple[float, str]:
    """Return (wait_seconds, label_of_next_run).

    Picks the soonest scheduled (hour, minute) in the next 24 hours.
    """
    best_delta: timedelta | None = None
    best_label = ""

    for hour, minute, label, _fn in schedules:
        target = now_ist.replace(hour=hour, minute=minute,
                                 second=0, microsecond=0)
        delta = target - now_ist
        if delta.total_seconds() <= 0:
            # Already passed today — schedule for tomorrow
            target = target + timedelta(days=1)
            delta = target - now_ist
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_label = label

    if best_delta is None:
        # No schedule — sleep 1 hour and check again
        return 3600.0, "no-schedule"
    return best_delta.total_seconds(), best_label


def _run_one(label: str, fn: callable) -> None:
    """Run one scheduled task with error isolation."""
    log.info("running %s", label)
    t0 = time.monotonic()
    try:
        result = fn()
        dt = time.monotonic() - t0
        log.info("%s completed in %.1fs: %s",
                 label, dt, _summarise(result))
    except Exception as e:
        log.exception("%s failed: %s", label, e)


def _summarise(result: object) -> str:
    """Best-effort summary of a run_once() return value."""
    if not isinstance(result, dict):
        return str(result)[:120]
    parts = []
    for k in ("sent", "skipped", "candidates", "fetch_ok",
              "articles", "alerts", "stocks_changed"):
        if k in result:
            parts.append(f"{k}={result[k]}")
    return ", ".join(parts) if parts else json_short(result)


def json_short(obj: object, n: int = 200) -> str:
    import json
    try:
        return json.dumps(obj, default=str)[:n]
    except Exception:
        return repr(obj)[:n]


_scheduler_started = False
_scheduler_lock = threading.Lock()


def _orchestrator_loop(stop_event: threading.Event) -> None:
    """Run scheduled tasks. Sleeps until the next scheduled time, then
    fires all tasks that match the current hour/minute."""
    schedules = _build_schedule()
    log.info("scheduler started with %d tasks: %s",
             len(schedules),
             [(h, m, lbl) for h, m, lbl, _ in schedules])

    while not stop_event.is_set():
        now_ist = datetime.now(IST)
        wait_s, next_label = _seconds_until_next_run(now_ist, schedules)

        # Round up to the next minute boundary so we don't busy-loop
        sleep_chunk = 60.0
        log.info("next run: %s in %.0fs", next_label, wait_s)

        slept = 0.0
        while slept < wait_s and not stop_event.is_set():
            stop_event.wait(min(sleep_chunk, wait_s - slept))
            slept += sleep_chunk

        if stop_event.is_set():
            break

        # Fire any tasks whose (hour, minute) matches now
        now_ist = datetime.now(IST)
        for hour, minute, label, fn in schedules:
            if now_ist.hour == hour and now_ist.minute == minute:
                _run_one(label, fn)


def start_orchestrator() -> threading.Event:
    """Start the unified orchestrator daemon. Returns stop_event."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            log.info("orchestrator already running")
            return threading.Event()
        _scheduler_started = True
        stop_event = threading.Event()
        t = threading.Thread(
            target=_orchestrator_loop, args=(stop_event,),
            daemon=True, name="scheduler-orchestrator",
        )
        t.start()
        log.info("orchestrator started")
        return stop_event


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--start", action="store_true",
                   help="Start the orchestrator daemon (runs forever).")
    p.add_argument("--dry-run", action="store_true",
                   help="Set DRY_RUN=1 in env for child modules.")
    p.add_argument("--show-schedule", action="store_true",
                   help="Print the schedule and exit.")
    args = p.parse_args()

    if args.dry_run:
        os.environ.setdefault("NEWS_ALERT_DRY_RUN", "1")
        os.environ.setdefault("EARNINGS_ALERT_DRY_RUN", "1")
        os.environ.setdefault("MF_ALERT_DRY_RUN", "1")

    schedules = _build_schedule()
    if args.show_schedule:
        print("Schedule (IST):")
        for hour, minute, label, _fn in schedules:
            print(f"  {hour:02d}:{minute:02d}  {label}")
        return

    if args.start:
        stop = start_orchestrator()
        log.info("orchestrator running. Ctrl-C to stop.")
        try:
            while not stop.wait(60):
                pass
        except KeyboardInterrupt:
            log.info("interrupted")
            stop.set()
        return

    # default: print schedule
    p.print_help()


if __name__ == "__main__":
    _cli()