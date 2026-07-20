"""Tests for the scheduler catch-up logic in pipeline.scheduler_utils."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pipeline.scheduler_utils import (
    IST,
    next_run_ist,
    start_with_catch_up,
)


class TestNextRunIst:
    def test_target_in_future_today(self):
        # Pretend it's 7:00 AM IST — target 8:55 AM should be today
        now = datetime(2026, 7, 6, 7, 0, 0, tzinfo=IST)
        with patch("pipeline.scheduler_utils.datetime") as mock_dt:
            mock_dt.now.return_value = now.astimezone(timezone.utc)
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            target = next_run_ist(8, 55)
        # Target is today at 8:55 IST = 03:25 UTC
        assert target.day == 6
        assert target.hour == 3 and target.minute == 25

    def test_target_passed_today(self):
        # Pretend it's 10:00 AM IST — target 8:55 AM should be tomorrow
        now = datetime(2026, 7, 6, 10, 0, 0, tzinfo=IST)
        with patch("pipeline.scheduler_utils.datetime") as mock_dt:
            mock_dt.now.return_value = now.astimezone(timezone.utc)
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            target = next_run_ist(8, 55)
        # Target is tomorrow at 8:55 IST
        assert target.day == 7
        assert target.hour == 3 and target.minute == 25


class TestStartWithCatchUp:
    """Verify that start_with_catch_up runs immediately if target passed."""

    def test_catch_up_fires_when_target_passed_4h_ago(self):
        # Pretend it's 12:55 PM IST — target 8:55 AM was 4h ago
        now_ist = datetime(2026, 7, 6, 12, 55, 0, tzinfo=IST)
        run_count = [0]

        def run_fn():
            run_count[0] += 1

        with patch("pipeline.scheduler_utils.datetime") as mock_dt:
            mock_dt.now.return_value = now_ist.astimezone(timezone.utc)
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            stop = start_with_catch_up(
                "test", 8, 55, run_fn,
                catch_up_window_secs=(60, 8 * 3600),
                thread_name="test-scheduler",
            )
        # Give daemon thread a moment to start
        time.sleep(0.1)
        stop.set()
        # run_fn was called once (the catch-up)
        assert run_count[0] == 1

    def test_no_catch_up_when_target_in_future(self):
        # Pretend it's 7:00 AM IST — target 8:55 AM is in the future
        now_ist = datetime(2026, 7, 6, 7, 0, 0, tzinfo=IST)
        run_count = [0]

        def run_fn():
            run_count[0] += 1

        with patch("pipeline.scheduler_utils.datetime") as mock_dt:
            mock_dt.now.return_value = now_ist.astimezone(timezone.utc)
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            stop = start_with_catch_up(
                "test", 8, 55, run_fn,
                catch_up_window_secs=(60, 8 * 3600),
                thread_name="test-scheduler",
            )
        time.sleep(0.1)
        stop.set()
        # run_fn was NOT called (catch-up window didn't trigger)
        assert run_count[0] == 0

    def test_no_catch_up_when_missed_more_than_8h(self):
        # Pretend it's 11:00 PM IST — target 8:55 AM was 14h ago (>8h cutoff)
        now_ist = datetime(2026, 7, 6, 23, 0, 0, tzinfo=IST)
        run_count = [0]

        def run_fn():
            run_count[0] += 1

        with patch("pipeline.scheduler_utils.datetime") as mock_dt:
            mock_dt.now.return_value = now_ist.astimezone(timezone.utc)
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            stop = start_with_catch_up(
                "test", 8, 55, run_fn,
                catch_up_window_secs=(60, 8 * 3600),
                thread_name="test-scheduler",
            )
        time.sleep(0.1)
        stop.set()
        # run_fn was NOT called (out of catch-up window)
        assert run_count[0] == 0

    def test_no_catch_up_within_1_minute_window(self):
        # Pretend it's 8:55:30 AM IST — target 8:55 AM was 30s ago (<1 min)
        now_ist = datetime(2026, 7, 6, 8, 55, 30, tzinfo=IST)
        run_count = [0]

        def run_fn():
            run_count[0] += 1

        with patch("pipeline.scheduler_utils.datetime") as mock_dt:
            mock_dt.now.return_value = now_ist.astimezone(timezone.utc)
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            stop = start_with_catch_up(
                "test", 8, 55, run_fn,
                catch_up_window_secs=(60, 8 * 3600),
                thread_name="test-scheduler",
            )
        time.sleep(0.1)
        stop.set()
        # run_fn was NOT called (too soon, would race with main scheduler)
        assert run_count[0] == 0
