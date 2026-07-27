"""
Tests for pipeline.news_alert.py — global news digest + Telegram delivery.

Covers:
  - _parse_rss: handles RSS 2.0 + Atom 1.0
  - _parse_pubdate: handles RFC 822, ISO 8601, naive formats
  - _classify: maps articles to the 7 categories by keyword matching
  - _gdacs_to_articles: converts GDACS GeoJSON to Article objects
  - _filter_fresh: drops articles older than MAX_AGE_HOURS
  - _categorise_and_dedup: buckets articles, dedups via seen cache
  - render_telegram: returns None when no articles; renders categories;
    markdown-escapes special characters
  - is_dry_run: True when bot token missing; False when set
  - _env: reads .env via dotenv fallback
  - run_once: persists seen cache; respects dry-run mode
  - _next_run_ist: math (today 8:55 if before, else tomorrow)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pipeline.news_alert as na  # noqa: E402
from pipeline.news_alert import (  # noqa: E402
    Article,
    CATEGORY_KEYWORDS,
    CATEGORY_DISPLAY,
    IST,
    LOG_FILE,
    SEEN_FILE,
    _parse_rss,
    _parse_pubdate,
    _classify,
    _gdacs_to_articles,
    _filter_fresh,
    _categorise_and_dedup,
    render_telegram,
    is_dry_run,
    _next_run_ist,
    _load_seen,
    _save_seen,
    run_once,
    NEWS_FEEDS,
)


# ---------- Fixtures ----------

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Federal Reserve cuts interest rates by 25 basis points</title>
      <link>https://example.com/fed-cuts</link>
      <pubDate>Sat, 27 Jun 2026 08:25:43 GMT</pubDate>
      <description>The Fed lowered its benchmark rate citing softer inflation.</description>
    </item>
    <item>
      <title>Powell signals more rate hikes ahead</title>
      <link>https://example.com/powell</link>
      <pubDate>Sat, 27 Jun 2026 09:00:00 GMT</pubDate>
      <description>Fed chair warned markets about further tightening.</description>
    </item>
    <item>
      <title>Giraffe found safe in Texas after weeks missing</title>
      <link>https://example.com/giraffe</link>
      <pubDate>Sat, 27 Jun 2026 10:00:00 GMT</pubDate>
      <description>Light human interest story, not relevant to user alerts.</description>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom Feed</title>
  <entry>
    <title>WHO declares monkeypox global health emergency</title>
    <link href="https://example.com/mpox"/>
    <published>2026-06-26T12:00:00Z</published>
    <summary>The World Health Organization escalated mpox to emergency status.</summary>
  </entry>
</feed>
"""


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(na, "LOG_FILE", tmp_path / "news_alert_log.json")
    monkeypatch.setattr(na, "SEEN_FILE", tmp_path / "news_alert_seen.json")
    return tmp_path


@pytest.fixture
def clean_news_env(monkeypatch):
    """Strip NEWS_* env vars and force dry-run."""
    for k in ("NEWS_DRY_RUN", "NEWS_TELEGRAM_BOT_TOKEN",
              "NEWS_TELEGRAM_CHAT_ID", "NEWS_QUIET_HOURS"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv(k, "")


# ---------- RSS parsing ----------

class TestParseRss:
    def test_parses_rss_2_items(self):
        articles = _parse_rss(RSS_SAMPLE, "Test")
        assert len(articles) == 3
        # First article
        assert "Federal Reserve" in articles[0].title
        assert articles[0].url == "https://example.com/fed-cuts"
        assert articles[0].source == "Test"
        assert articles[0].published is not None
        # Third article (giraffe)
        assert "Giraffe" in articles[2].title

    def test_parses_atom_feed(self):
        articles = _parse_rss(ATOM_SAMPLE, "TestAtom")
        assert len(articles) == 1
        assert "monkeypox" in articles[0].title
        assert articles[0].url == "https://example.com/mpox"
        assert articles[0].source == "TestAtom"

    def test_returns_empty_for_garbage(self):
        assert _parse_rss("not xml at all", "Broken") == []

    def test_skips_items_without_title(self):
        xml = """<rss><channel><item><link>https://x</link></item>
                          <item><title>OK</title><link>https://y</link></item></channel></rss>"""
        articles = _parse_rss(xml, "Test")
        assert len(articles) == 1
        assert articles[0].title == "OK"


# ---------- Pubdate parsing ----------

class TestParsePubdate:
    def test_rfc_822_with_gmt(self):
        dt = _parse_pubdate("Sat, 27 Jun 2026 08:25:43 GMT")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 6 and dt.day == 27

    def test_iso_8601_z(self):
        dt = _parse_pubdate("2026-06-26T12:00:00Z")
        assert dt is not None
        assert dt.year == 2026

    def test_returns_none_for_garbage(self):
        assert _parse_pubdate("not a date") is None
        assert _parse_pubdate("") is None

    def test_returns_naive_utc(self):
        dt = _parse_pubdate("Sat, 27 Jun 2026 08:25:43 GMT")
        assert dt.tzinfo is None  # naive UTC


# ---------- Classification ----------

class TestClassify:
    def test_interest_rate_keyword_match(self):
        a = Article(title="Fed cuts rates", url="", source="x", published=None)
        assert _classify(a) == "interest_rate"

    def test_geopolitical_war_keyword_match(self):
        a = Article(title="Israel strikes Gaza",
                    url="", source="x", published=None,
                    description="military strikes")
        assert _classify(a) == "geopolitical"

    def test_pandemic_keyword_match(self):
        a = Article(title="WHO declares outbreak emergency",
                    url="", source="x", published=None)
        assert _classify(a) == "pandemic"

    def test_exchange_rate_keyword_match(self):
        a = Article(title="Rupee hits record low vs dollar",
                    url="", source="x", published=None)
        assert _classify(a) == "exchange_rate"

    def test_purchasing_power_keyword_match(self):
        a = Article(title="CPI inflation data shows price pressures rising",
                    url="", source="x", published=None)
        assert _classify(a) == "purchasing_power"

    def test_market_risk_keyword_match(self):
        a = Article(title="S&P 500 plunges 5% in broad market sell-off",
                    url="", source="x", published=None)
        assert _classify(a) == "market_risk"

    def test_economic_keyword_match(self):
        a = Article(title="GDP growth slows amid unemployment rise",
                    url="", source="x", published=None)
        assert _classify(a) == "economic"

    def test_default_risk_keyword_match(self):
        a = Article(title="Major airline files for bankruptcy",
                    url="", source="x", published=None)
        assert _classify(a) == "default_risk"

    def test_liquidity_risk_keyword_match(self):
        a = Article(title="Money market fund faces redemption pressure",
                    url="", source="x", published=None)
        assert _classify(a) == "liquidity_risk"

    def test_financial_risk_keyword_match(self):
        a = Article(title="Credit rating downgrade by Moody's on debt",
                    url="", source="x", published=None)
        assert _classify(a) == "financial_risk"

    def test_business_risk_keyword_match(self):
        a = Article(title="Tech firm issues profit warning after earnings miss",
                    url="", source="x", published=None)
        assert _classify(a) == "business_risk"

    def test_management_risk_keyword_match(self):
        a = Article(title="CEO resigns after accounting scandal",
                    url="", source="x", published=None)
        assert _classify(a) == "management_risk"

    def test_giraffe_returns_none(self):
        """Light human-interest story should be skipped (None category)."""
        a = Article(title="Giraffe found safe after weeks",
                    url="", source="x", published=None,
                    description="Light human interest story.")
        assert _classify(a) is None

    def test_pinned_category_honoured(self):
        """If the feed has a pinned category, we use it directly
        (no keyword matching needed)."""
        a = Article(title="Random business news",
                    url="", source="BBC Business", published=None)
        assert _classify(a) == "economic"

    def test_overlapping_article_only_counted_once(self):
        """An article matching multiple categories should be assigned to
        the FIRST matching one (priority order matters)."""
        # 'inflation' + 'rate cut' could match both purchasing_power and
        # interest_rate. We expect interest_rate to win (higher priority).
        a = Article(
            title="Fed signals rate cut as inflation eases",
            url="", source="x", published=None,
        )
        assert _classify(a) == "interest_rate"

    def test_case_insensitive(self):
        a = Article(title="FED CUTS RATES", url="", source="x", published=None)
        assert _classify(a) == "interest_rate"


# ---------- Filter fresh ----------

class TestFilterFresh:
    def test_drops_old_articles(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old = Article(title="x", url="", source="x",
                      published=now - timedelta(hours=na.MAX_AGE_HOURS + 5))
        new = Article(title="x", url="", source="x",
                      published=now - timedelta(hours=2))
        kept = _filter_fresh([old, new])
        assert len(kept) == 1
        assert kept[0].published == new.published

    def test_keeps_undated_articles(self):
        """Some feeds omit pubDate (evergreen content) — keep them."""
        undated = Article(title="x", url="", source="x", published=None)
        kept = _filter_fresh([undated])
        assert len(kept) == 1


# ---------- Portfolio filter (2026-07-27) ----------

class TestFilterForPortfolio:
    """
    The news digest is filtered through portfolio_impact._find_affected_tickers
    so that only articles scoring >= 4 against the user's holdings reach
    the user. FIFA-finals-style stories must be dropped.
    """

    def test_unrelated_article_is_dropped(self):
        """FIFA finals / cat rescue / etc. score 0 and are dropped."""
        article = Article(title="FIFA World Cup final highlights",
                         url="https://sport/fifa-final",
                         source="BBC", published=datetime.now())
        kept, dropped = na._filter_for_portfolio([article])
        assert dropped == 1
        assert kept == []

    def test_reliance_industries_article_is_kept(self):
        article = Article(
            title="Reliance Industries announces record quarterly profit",
            url="https://biz/reliance-q1",
            source="MoneyControl", published=datetime.now(),
        )
        kept, dropped = na._filter_for_portfolio([article])
        assert dropped == 0
        assert len(kept) == 1

    def test_unominda_article_is_kept(self):
        article = Article(
            title="Uno Minda auto component exports surge 30% YoY",
            url="https://biz/unominda",
            source="MoneyControl", published=datetime.now(),
        )
        kept, dropped = na._filter_for_portfolio([article])
        assert dropped == 0
        assert len(kept) == 1

    def test_gold_price_article_kept_for_goldbees(self):
        article = Article(
            title="Gold price hits fresh record on US Fed pivot",
            url="https://biz/gold-record",
            source="Bloomberg", published=datetime.now(),
        )
        kept, dropped = na._filter_for_portfolio([article])
        assert dropped == 0
        assert len(kept) == 1

    def test_disabled_when_env_var_zero(self):
        """NEWS_PORTFOLIO_ONLY=0 disables the filter (legacy behavior)."""
        article = Article(title="FIFA World Cup final highlights",
                         url="https://sport/fifa", source="BBC",
                         published=datetime.now())
        with patch.dict(os.environ, {"NEWS_PORTFOLIO_ONLY": "0"}):
            kept, dropped = na._filter_for_portfolio([article])
        assert dropped == 0
        assert len(kept) == 1

    def test_unqualified_reliance_idiom_is_dropped(self):
        """English idiom 'self-reliance' must not pass the filter."""
        article = Article(
            title="Economic self-reliance key to India's rise in new global order",
            url="https://opinion/gurumurthy", source="The Hindu",
            published=datetime.now(),
        )
        kept, dropped = na._filter_for_portfolio([article])
        assert dropped == 1
        assert kept == []

    def test_drop_count_includes_unrelated_articles(self):
        relevant = Article(title="Reliance Industries Q1 profit",
                          url="https://a", source="x",
                          published=datetime.now())
        irrelevant = Article(title="Local cricket match highlights",
                            url="https://b", source="x",
                            published=datetime.now())
        kept, dropped = na._filter_for_portfolio([relevant, irrelevant])
        assert dropped == 1
        assert len(kept) == 1


# ---------- Categorise and dedup ----------

class TestCategoriseAndDedup:
    def test_buckets_articles_into_categories(self):
        articles = [
            Article(title="Fed cuts rates", url="https://a", source="x",
                   published=datetime.now()),
            Article(title="WHO declares outbreak", url="https://b", source="x",
                   published=datetime.now()),
            Article(title="Giraffe found", url="https://c", source="x",
                   published=datetime.now()),  # should be skipped
        ]
        buckets = _categorise_and_dedup(articles, seen={})
        assert len(buckets["interest_rate"]) == 1
        assert len(buckets["pandemic"]) == 1
        assert len(buckets["geopolitical"]) == 0  # giraffe filtered out
        assert len(buckets["default_risk"]) == 0

    def test_dedups_against_seen_cache(self):
        a = Article(title="Fed cuts rates", url="https://fed",
                    source="x", published=datetime.now())
        buckets = _categorise_and_dedup([a], seen={"https://fed": "2026-01-01"})
        assert len(buckets["interest_rate"]) == 0

    def test_caps_at_max_per_category(self):
        articles = [
            Article(title=f"Fed rate decision #{i}", url=f"https://f{i}",
                    source="x", published=datetime.now())
            for i in range(20)
        ]
        buckets = _categorise_and_dedup(articles, seen={})
        assert len(buckets["interest_rate"]) == na.MAX_PER_CATEGORY

    def test_overlapping_article_assigned_to_first_match(self):
        """An article that matches both interest_rate and purchasing_power
        should land in interest_rate (higher priority)."""
        a = Article(
            title="Fed signals rate cut as inflation eases",
            url="https://x", source="x", published=datetime.now(),
        )
        buckets = _categorise_and_dedup([a], seen={})
        assert len(buckets["interest_rate"]) == 1
        assert len(buckets["purchasing_power"]) == 0  # not duplicated


# ---------- Render telegram ----------

class TestRenderTelegram:
    def test_returns_none_when_no_articles_and_not_forced(self):
        buckets = {c: [] for c in CATEGORY_DISPLAY}
        assert render_telegram(buckets, date_str="27 Jun 2026") is None

    def test_returns_no_news_message_when_forced_with_no_articles(self):
        buckets = {c: [] for c in CATEGORY_DISPLAY}
        msg = render_telegram(buckets, date_str="27 Jun 2026", force=True)
        assert msg is not None
        assert "No significant alerts" in msg

    def test_renders_all_populated_categories(self):
        buckets = {c: [] for c in CATEGORY_DISPLAY}
        buckets["geopolitical"].append(Article(
            title="Israel strikes Gaza", url="https://war1",
            source="x", published=datetime.now(), category="geopolitical",
        ))
        buckets["interest_rate"].append(Article(
            title="Fed cuts rates", url="https://fed1",
            source="x", published=datetime.now(), category="interest_rate",
        ))
        msg = render_telegram(buckets, date_str="27 Jun 2026")
        assert msg is not None
        assert "Israel strikes Gaza" in msg
        assert "Fed cuts rates" in msg
        assert "Geopolitical" in msg or "War" in msg
        assert "Interest Rate" in msg

    def test_titles_are_escaped_for_plain_text(self):
        """Article titles containing _, *, [, ] must not break Telegram's
        plain-text parser. We use plain text (no parse_mode) but we still
        escape stray `_` chars to avoid unintended italic rendering.
        Note: Telegram renders _word_ as italic even in plain text."""
        buckets = {c: [] for c in CATEGORY_DISPLAY}
        buckets["interest_rate"].append(Article(
            title="RBI_repo_rate_hiked_to_[6.5%]_today",
            url="https://rbi", source="x", published=datetime.now(),
            category="interest_rate",
        ))
        msg = render_telegram(buckets, date_str="27 Jun 2026")
        assert msg is not None
        # Underscores in the title should be escaped (so they don't render
        # as italic spans in Telegram's plain text)
        assert "RBI\\_repo\\_rate" in msg
        # The URL must still be present
        assert "https://rbi" in msg

    def test_message_includes_date_and_sources(self):
        buckets = {c: [] for c in CATEGORY_DISPLAY}
        buckets["geopolitical"].append(Article(
            title="Test war", url="https://w", source="x",
            published=datetime.now(), category="geopolitical",
        ))
        msg = render_telegram(buckets, date_str="27 Jun 2026")
        assert "27 Jun 2026" in msg
        assert "Sources" in msg


# ---------- is_dry_run ----------

class TestIsDryRun:
    def test_dry_run_when_no_creds(self, clean_news_env):
        assert is_dry_run() is True

    def test_dry_run_when_explicit_flag(self, monkeypatch, clean_news_env):
        monkeypatch.setenv("NEWS_DRY_RUN", "1")
        assert is_dry_run() is True

    def test_not_dry_run_when_creds_present(self, monkeypatch, clean_news_env):
        monkeypatch.setenv("NEWS_TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("NEWS_TELEGRAM_CHAT_ID", "456")
        assert is_dry_run() is False


# ---------- _next_run_ist ----------

class TestNextRunIst:
    def test_returns_today_if_before_855(self, monkeypatch):
        # Fake now: 2026-06-27 06:00 IST
        class FakeDT:
            @classmethod
            def now(cls, tz=None):
                if tz is IST:
                    return datetime(2026, 6, 27, 6, 0, tzinfo=IST)
                return datetime(2026, 6, 27, 0, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(na, "datetime", FakeDT)
        nxt = _next_run_ist()
        # 08:55 IST = 03:25 UTC
        assert nxt.hour == 3 and nxt.minute == 25
        assert nxt.day == 27

    def test_returns_tomorrow_if_after_855(self, monkeypatch):
        class FakeDT:
            @classmethod
            def now(cls, tz=None):
                if tz is IST:
                    return datetime(2026, 6, 27, 18, 0, tzinfo=IST)
                return datetime(2026, 6, 27, 12, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(na, "datetime", FakeDT)
        nxt = _next_run_ist()
        assert nxt.day == 28


# ---------- _scheduler_loop: missed-window guard ----------

class TestShouldSkipMissedRun:
    """Pure unit tests for the missed-window decision. This is the
    logic that prevented the duplicate 9:09 AM Telegram send."""

    def test_skip_when_missed_by_10_minutes(self):
        """Mac woke up 10 min after 8:55 target → skip."""
        target = datetime(2026, 6, 27, 3, 25, 0)   # 03:25 UTC = 8:55 IST
        now    = datetime(2026, 6, 27, 3, 35, 0)   # 10 min later
        skip, missed = na._should_skip_missed_run(target, now)
        assert skip is True
        assert missed == 600.0

    def test_run_when_missed_by_only_2_minutes(self):
        """Mac woke up 2 min after target → still run (within grace)."""
        target = datetime(2026, 6, 27, 3, 25, 0)
        now    = datetime(2026, 6, 27, 3, 27, 0)
        skip, missed = na._should_skip_missed_run(target, now)
        assert skip is False
        assert missed == 120.0

    def test_run_when_exactly_on_time(self):
        """Edge case: now == target → no miss, run."""
        target = datetime(2026, 6, 27, 3, 25, 0)
        now    = datetime(2026, 6, 27, 3, 25, 0)
        skip, missed = na._should_skip_missed_run(target, now)
        assert skip is False
        assert missed == 0.0

    def test_run_when_at_grace_boundary(self):
        """Exactly at 5-minute grace → do NOT skip (uses strict >)."""
        target = datetime(2026, 6, 27, 3, 25, 0)
        now    = datetime(2026, 6, 27, 3, 30, 0)
        skip, missed = na._should_skip_missed_run(target, now)
        assert skip is False   # 300s == grace, not strictly greater

    def test_run_when_target_is_in_future(self):
        """Scheduler polled early, target hasn't been reached yet."""
        target = datetime(2026, 6, 27, 3, 25, 0)
        now    = datetime(2026, 6, 27, 3, 20, 0)   # 5 min before
        skip, missed = na._should_skip_missed_run(target, now)
        assert skip is False
        assert missed == -300.0

    def test_custom_grace_period(self):
        """Callers can pass a tighter grace (e.g. for tests)."""
        target = datetime(2026, 6, 27, 3, 25, 0)
        now    = datetime(2026, 6, 27, 3, 25, 30)   # 30s late
        # With 60s grace: 30s < 60s → run (don't skip).
        # With 10s grace: 30s > 10s → skip.
        skip_60, _ = na._should_skip_missed_run(target, now, grace_secs=60)
        skip_10, _ = na._should_skip_missed_run(target, now, grace_secs=10)
        assert skip_60 is False
        assert skip_10 is True


class TestSchedulerLoopIntegration:
    """Light integration test: drive the pipeline.scheduler loop with a fake
    clock + fast Event.wait, verifying both the happy path and the
    missed-window skip path."""

    def _patched_loop(self, monkeypatch, fake_now_utc):
        """Replace datetime.now + Event.wait with controllable fakes.
        Returns the run-call recorder and a stop() helper."""
        from pipeline.news_alert import _scheduler_loop
        runs = []
        monkeypatch.setattr(na, "run_once",
                            lambda *a, **kw: runs.append(1))
        # Make Event.wait return immediately so loops don't block
        orig_wait = threading.Event.wait
        monkeypatch.setattr(
            threading.Event, "wait",
            lambda self, timeout=None: orig_wait(self, 0.001),
        )
        # Force datetime.now() to return our fixed value
        class FakeDT:
            @classmethod
            def now(cls, tz=None):
                if tz is timezone.utc:
                    return fake_now_utc.replace(tzinfo=timezone.utc)
                if tz is IST:
                    utc = fake_now_utc.replace(tzinfo=timezone.utc)
                    return utc.astimezone(IST).replace(tzinfo=None)
                return fake_now_utc
        monkeypatch.setattr(na, "datetime", FakeDT)
        return _scheduler_loop, runs

    def test_runs_normally_when_well_within_window(self, monkeypatch,
                                                   clean_news_env):
        """now = 8:50 IST (5 min before target) → should run at least once."""
        fake_now = datetime(2026, 6, 27, 3, 20, 0)   # 8:50 IST
        loop, runs = self._patched_loop(monkeypatch, fake_now)
        stop = threading.Event()
        t = threading.Thread(target=loop, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.5)
        stop.set()
        t.join(timeout=3)
        assert len(runs) >= 1, f"expected at least 1 run, got {len(runs)}"

    def test_skips_when_target_missed_by_10_minutes(self, monkeypatch,
                                                    clean_news_env):
        """now = 9:05 IST (10 min after target) → should skip run_once."""
        fake_now = datetime(2026, 6, 27, 3, 35, 0)   # 9:05 IST
        # Patch _next_run_ist to return today's 8:55 IST (= 03:25 UTC),
        # because the fake clock is past it and the real helper would
        # otherwise push to tomorrow.
        monkeypatch.setattr(
            na, "_next_run_ist",
            lambda *a, **kw: datetime(2026, 6, 27, 3, 25, 0),
        )
        loop, runs = self._patched_loop(monkeypatch, fake_now)
        stop = threading.Event()
        t = threading.Thread(target=loop, args=(stop,), daemon=True)
        t.start()
        # Give the loop time to: compute wait_secs (negative), exit inner
        # loop, check missed_by=600s, log warning, enter "sleep until
        # tomorrow" loop.
        time.sleep(0.5)
        stop.set()
        t.join(timeout=3)
        assert len(runs) == 0, \
            f"expected 0 runs (missed-window), got {len(runs)}"

    def test_log_uses_ist_label_correctly(self, monkeypatch, clean_news_env):
        """The 'next run at ... IST' log line must show IST, not UTC.

        Regression test: the previous version logged a UTC-naive
        timestamp but labelled it 'IST', which was misleading by 5h30.
        """
        from pipeline.news_alert import _scheduler_loop
        captured = []
        class FakeLog:
            def info(self, fmt, *args):
                captured.append(fmt % args)
            def warning(self, *a, **kw): pass
            def exception(self, *a, **kw): pass
        monkeypatch.setattr(na, "log", FakeLog())
        # Fast Event.wait
        orig_wait = threading.Event.wait
        monkeypatch.setattr(
            threading.Event, "wait",
            lambda self, timeout=None: orig_wait(self, 0.001),
        )
        # 6:00 AM IST = 00:30 UTC. Target is today 8:55 IST = 03:25 UTC.
        # fake_now at 06:00 IST → wait_secs ≈ 9900s (we don't care).
        # Fake 'now' = 9:30 IST → past target → missed-window path.
        # We want to capture the FIRST log line (the 'next run at' one).
        fake_now = datetime(2026, 6, 27, 3, 30, 0)   # 9:00 IST
        # Patch _next_run_ist to return today's target so the inner
        # loop computes a small wait_secs and the log line fires.
        monkeypatch.setattr(
            na, "_next_run_ist",
            lambda *a, **kw: datetime(2026, 6, 27, 3, 25, 0),
        )
        # Fake datetime.now
        class FakeDT:
            @classmethod
            def now(cls, tz=None):
                if tz is timezone.utc:
                    return fake_now.replace(tzinfo=timezone.utc)
                if tz is IST:
                    return fake_now.replace(tzinfo=timezone.utc) \
                                     .astimezone(IST).replace(tzinfo=None)
                return fake_now
        monkeypatch.setattr(na, "datetime", FakeDT)
        # Make run_once set stop so the loop exits after first run
        stop = threading.Event()
        def fake_run_once():
            stop.set()
        monkeypatch.setattr(na, "run_once", fake_run_once)
        t = threading.Thread(
            target=_scheduler_loop, args=(stop,), daemon=True
        )
        t.start()
        t.join(timeout=3)
        # Expect log line with "08:55:00 IST"
        assert any("08:55:00" in line and "IST" in line for line in captured), \
            f"expected '08:55:00 IST' in log lines, got: {captured[:3]}"
        # And NOT the misleading UTC value labelled IST
        assert not any("03:25:00 IST" in line for line in captured), \
            f"unexpected '03:25:00 IST' (UTC mis-labelled): {captured[:3]}"


# ---------- run_once ----------

class TestRunOnce:
    def test_run_once_dry_run_returns_no_send(self, tmp_data_dir, clean_news_env):
        """When all categories are empty and force=False, returns
        sent=False with reason='no significant news'."""
        # Mock fetch_articles to return only human-interest stories
        with patch.object(na, "fetch_articles", return_value=[
            Article(title="Local cat rescued from tree", url="https://cat",
                    source="x", published=datetime.now()),
        ]):
            result = run_once()
        assert result["fetch_ok"] is True
        assert result["articles_total"] == 1
        assert result["articles_kept"] == 0
        assert result["telegram"]["sent"] is False
        assert "no significant news" in result["telegram"]["reason"]

    def test_run_once_force_sends_even_when_empty(self, tmp_data_dir, clean_news_env):
        with patch.object(na, "fetch_articles", return_value=[]):
            result = run_once(force_send=True)
        assert result["articles_total"] == 0
        assert result["telegram"]["mode"] == "dry_run"

    def test_run_once_persists_seen_cache(self, tmp_data_dir, clean_news_env):
        """Articles we send should appear in SEEN_FILE so we don't
        re-send them tomorrow."""
        with patch.object(na, "fetch_articles", return_value=[
            Article(title="Fed cuts rates", url="https://fed-unique",
                    source="x", published=datetime.now()),
        ]):
            run_once()
        assert na.SEEN_FILE.exists()
        seen = json.loads(na.SEEN_FILE.read_text())
        assert "https://fed-unique" in seen

    def test_run_once_skips_already_seen(self, tmp_data_dir, clean_news_env):
        """Articles we've already alerted on should not be sent again."""
        # Pre-populate seen cache
        seen = {"https://fed-already-seen": "2026-06-26T08:00:00"}
        na._save_seen(seen)
        with patch.object(na, "fetch_articles", return_value=[
            Article(title="Fed cuts rates", url="https://fed-already-seen",
                    source="x", published=datetime.now()),
        ]):
            result = run_once()
        assert result["articles_kept"] == 0


# ---------- Edge cases ----------

class TestEdgeCases:
    def test_rss_with_no_items(self):
        xml = """<rss><channel><title>Empty</title></channel></rss>"""
        assert _parse_rss(xml, "empty") == []

    def test_seen_cache_round_trip(self, tmp_data_dir):
        na._save_seen({"https://a": "2026-06-27T08:00:00"})
        loaded = _load_seen()
        assert "https://a" in loaded

    def test_seen_cache_expires_old_entries(self, tmp_data_dir):
        """Entries older than 7 days should be auto-expired on load."""
        eight_days_ago = (datetime.now() - timedelta(days=8)).isoformat()
        na._save_seen({
            "https://old": eight_days_ago,
            "https://recent": datetime.now().isoformat(),
        })
        loaded = _load_seen()
        assert "https://old" not in loaded
        assert "https://recent" in loaded

    def test_dedup_against_empty_seen(self):
        """No prior cache means all fresh articles pass through."""
        articles = [
            Article(title="Fed cuts rates", url="https://a", source="x",
                   published=datetime.now()),
            Article(title="WHO outbreak", url="https://b", source="x",
                   published=datetime.now()),
        ]
        buckets = _categorise_and_dedup(articles, seen={})
        assert len(buckets["interest_rate"]) == 1
        assert len(buckets["pandemic"]) == 1

    def test_message_length_within_telegram_limit(self):
        """Even with a heavy news day, message should stay under 4096 chars."""
        buckets = {c: [] for c in CATEGORY_DISPLAY}
        # Use the new category names
        cat_list = ["market_risk", "interest_rate", "purchasing_power",
                    "exchange_rate", "default_risk", "liquidity_risk",
                    "financial_risk", "business_risk", "management_risk",
                    "geopolitical", "pandemic", "economic"]
        for cat in cat_list:
            for i in range(na.MAX_PER_CATEGORY):
                buckets[cat].append(Article(
                    title=f"Headline {i} for {cat}",
                    url=f"https://example.com/{cat}/{i}",
                    source="x", published=datetime.now(),
                    description="Some descriptive text here.",
                    category=cat,
                ))
        msg = render_telegram(buckets, date_str="27 Jun 2026")
        assert msg is not None
        assert len(msg) < 4096, f"message too long: {len(msg)} chars"


# ---------- Integration (real network) ----------

@pytest.mark.integration
class TestIntegration:
    def test_fetch_real_feeds(self):
        """Hit real RSS feeds; verify we get articles back."""
        articles = na.fetch_articles(timeout=15)
        assert len(articles) > 50  # 13+ feeds should yield lots
        sources = {a.source for a in articles}
        assert len(sources) >= 5  # at least 5 different feeds worked

    def test_gdacs_endpoint_returns_events(self):
        events = _gdacs_to_articles()
        # Either there are no recent disasters (acceptable) or we got some
        assert isinstance(events, list)

    def test_real_run_once_returns_dict(self, tmp_data_dir, clean_news_env):
        result = run_once()
        assert result["fetch_ok"] is True
        assert isinstance(result["categories"], dict)
        # Clear the seen cache and run again — we should still get
        # articles (since the feeds update between runs).
        na._save_seen({})
        result2 = run_once()
        assert result2["fetch_ok"] is True
        # Both runs should produce a dict (whether articles are kept
        # depends on the seen cache state)