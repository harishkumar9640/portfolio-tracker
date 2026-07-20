"""
pipeline.scheduler_utils
-----------------------
Shared helpers for the daily-scheduler daemon threads in this project.

Each alert module (news_alert, mf_holdings_alert, shareholding_alert,
flows_alert, earnings_alert) has its own ``start_daily_scheduler()``
function that registers a daemon thread running at a fixed IST time.
This module provides:

- ``next_run_ist(hour, minute)`` — compute the next UTC-naive timestamp
  when the target IST hour:minute will be hit.
- ``run_with_catch_up(name, target_hour, target_minute, run_fn)`` —
  start a daemon thread that, on first call, IMMEDIATELY runs
  ``run_fn`` if today's target time has already passed (between 1 min
  and 8 hours ago). This handles the common case: the webapp restarts
  at, say, 8:01 AM, and the 8:55 AM news digest would otherwise be
  silently skipped until tomorrow.

The catch-up window is intentionally bounded (1 min to 8 h) so that:
- We don't fire a 4 AM digest the moment the user restarts the webapp
  at 2 PM (8 h cutoff).
- We don't double-send if the scheduler thread "raced" the wall clock
  and is only 30 s late (1 min cutoff).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("scheduler_utils")

IST = timezone(timedelta(hours=5, minutes=30))


def next_run_ist(hour: int, minute: int) -> datetime:
    """Return the next UTC-naive timestamp at which IST hour:minute will hit.

    If today's target time is in the future, returns today at that time.
    If today's target time has passed, returns tomorrow at that time.
    """
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)
    target_today_ist = now_ist.replace(hour=hour, minute=minute,
                                        second=0, microsecond=0)
    if target_today_ist > now_ist:
        target_ist = target_today_ist
    else:
        target_ist = target_today_ist + timedelta(days=1)
    return target_ist.astimezone(timezone.utc).replace(tzinfo=None)


def start_with_catch_up(
    name: str,
    target_hour: int,
    target_minute: int,
    run_fn,
    catch_up_window_secs: tuple[int, int] = (60, 8 * 3600),
    thread_name: str | None = None,
) -> threading.Event:
    """Start a daemon thread for a daily job with catch-up on startup.

    Args:
        name: human-readable name (used in logs).
        target_hour, target_minute: the IST time of day to run.
        run_fn: callable that performs the actual job.
        catch_up_window_secs: (min, max) seconds since target time
            for which catch-up will fire. Default: 1 min to 8 h.
        thread_name: optional thread name.

    Returns:
        threading.Event that, when set, stops the scheduler.
    """
    stop_event = threading.Event()

    # Catch-up check: if today's target time has passed (within the
    # window), run once immediately on this same thread (before
    # spawning the daemon).
    target = next_run_ist(target_hour, target_minute)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    missed_by = (now - target).total_seconds()
    # Note: `target` is the NEXT run, so `now > target` means we missed
    # today's run only if the next run is tomorrow. I.e. only when
    # the target time has passed TODAY.
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    target_today_ist = now_ist.replace(hour=target_hour, minute=target_minute,
                                        second=0, microsecond=0)
    missed_today_secs = (now_ist - target_today_ist).total_seconds()
    if catch_up_window_secs[0] <= missed_today_secs <= catch_up_window_secs[1]:
        log.info(
            "%s: target %02d:%02d IST was %.0fs ago \u2014 "
            "running once immediately to catch up",
            name, target_hour, target_minute, missed_today_secs,
        )
        try:
            run_fn()
        except Exception as e:
            log.exception("%s catch-up run failed: %s", name, e)
    else:
        log.debug(
            "%s: target was %+.0fs ago (window %ds..%ds); no catch-up",
            name, missed_today_secs,
            catch_up_window_secs[0], catch_up_window_secs[1],
        )

    # Spawn the daemon for the recurring schedule.
    t = threading.Thread(
        target=_daemon_loop,
        args=(name, target_hour, target_minute, run_fn, stop_event),
        daemon=True,
        name=thread_name or f"{name}-scheduler",
    )
    t.start()
    log.info("%s scheduler started", name)
    return stop_event


def _daemon_loop(name, target_hour, target_minute, run_fn, stop_event):
    """Background loop: wait until next target time, then run_fn."""
    MAX_MISSED_SECS = 5 * 60  # 5 min
    while not stop_event.is_set():
        target = next_run_ist(target_hour, target_minute)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        wait_secs = (target - now).total_seconds()
        target_ist = target.replace(tzinfo=timezone.utc).astimezone(IST)
        log.info(
            "%s scheduler: next run at %s IST (in %.0fs)",
            name, target_ist.strftime("%Y-%m-%d %H:%M:%S"), wait_secs,
        )
        while wait_secs > 0 and not stop_event.is_set():
            chunk = min(60, wait_secs)
            stop_event.wait(chunk)
            wait_secs -= chunk
        if stop_event.is_set():
            break

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        missed_by = (now - target).total_seconds()
        if missed_by > MAX_MISSED_SECS:
            log.warning(
                "%s scheduler: missed target by %.0fs (>%ds) \u2014 "
                "skipping today's run to avoid duplicate send. "
                "Next attempt at tomorrow's %02d:%02d IST.",
                name, missed_by, MAX_MISSED_SECS, target_hour, target_minute,
            )
            # Sleep until tomorrow's window
            tomorrow_target = target + timedelta(days=1)
            while not stop_event.is_set():
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                remaining = (tomorrow_target - now).total_seconds()
                if remaining <= 0:
                    break
                stop_event.wait(min(60, remaining))
            continue

        try:
            run_fn()
        except Exception as e:
            log.exception("%s scheduled run failed: %s", name, e)
