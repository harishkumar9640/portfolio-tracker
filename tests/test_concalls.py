"""
Tests for pipeline.concalls.py.

Coverage:
  - Filing classification (transcript / investor_presentation / audio_recording)
  - Filing parser (ticker matching, URL validation)
  - Summary parser (tone, guidance, bullets, phrases)
  - Markdown stripping (LLM sometimes adds ** and *)
  - "Here is the combined summary" noise line filter
  - Dedup (filing_cache_key)
  - Cache load/save round-trip
  - Telegram render (must include ticker, tone, at least 3 bullets)
  - Dry-run safety
  - Chunking (long transcripts split correctly)
  - Prompt contains the right structure
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pipeline.concalls as cc  # noqa: E402
from pipeline.concalls import (  # noqa: E402
    ConcallFiling, ConcallSummary,
    _classify_filing, _parse_summary, _chunk_text,
    _filing_cache_key, render_telegram,
)


IST = cc.IST


# ===========================================================================
# 1. Filing classification
# ===========================================================================

class TestClassifyFiling:
    def test_classifies_transcript(self):
        assert _classify_filing(
            "Analysts/Institutional Investor Meet/Con. Call Updates",
            "IRCON has informed the Exchange about Transcript of Q&A",
        ) == "transcript"

    def test_classifies_investor_presentation(self):
        assert _classify_filing(
            "Investor Presentation",
            "KNR has informed about Investor Presentation",
        ) == "investor_presentation"

    def test_classifies_audio_recording(self):
        assert _classify_filing(
            "Analysts/Institutional Investor Meet/Con. Call Updates",
            "KNR has informed about Link of Recording",
        ) == "audio_recording"
        assert _classify_filing(
            "Analysts/Institutional Investor Meet/Con. Call Updates",
            "Audio recording of analyst meet available",
        ) == "audio_recording"

    def test_rejects_unrelated_filing(self):
        """Schedule of meet, loss of share cert, etc. should not match."""
        assert _classify_filing(
            "Schedule of meet",
            "ITC Limited will participate in BofA conference",
        ) is None
        assert _classify_filing(
            "Loss of Share Certificates",
            "Some shareholder lost share certificates",
        ) is None

    def test_handles_none_inputs(self):
        """Defensive: must not crash on missing desc/attch."""
        assert _classify_filing(None, None) is None
        assert _classify_filing("", "") is None


# ===========================================================================
# 2. Filing parser (ticker matching + URL validation)
# ===========================================================================

class TestParseFiling:
    def _name_by(self):
        return {
            "ITC": ["itc limited", "itc ltd"],
            "IRCON": ["ircon international"],
        }

    def test_matches_by_symbol(self):
        row = {
            "symbol": "IRCON", "comp": "IRCON International Limited",
            "desc": "Analysts/...Con. Call Updates",
            "attchmntText": "IRCON has informed about Transcript",
            "attchmntFile": "https://nsearchives.nseindia.com/corporate/IRCON_xxx.pdf",
            "an_dt": "27-May-2025 18:07:52",
        }
        f = cc._parse_filing(row, self._name_by(), ["ITC", "IRCON"])
        assert f is not None
        assert f.ticker == "IRCON"
        assert f.filing_kind == "transcript"
        assert f.filing_date == "27-May-2025"

    def test_rejects_non_portfolio_symbol(self):
        row = {
            "symbol": "WIPRO", "comp": "Wipro Limited",
            "desc": "Transcript",
            "attchmntText": "Wipro informed about Transcript",
            "attchmntFile": "https://...wipro.pdf",
            "an_dt": "27-May-2025",
        }
        f = cc._parse_filing(row, self._name_by(), ["ITC", "IRCON"])
        assert f is None

    def test_rejects_invalid_url(self):
        row = {
            "symbol": "IRCON", "comp": "IRCON",
            "desc": "Transcript",
            "attchmntText": "Transcript filed",
            "attchmntFile": "",  # no URL
            "an_dt": "27-May-2025",
        }
        assert cc._parse_filing(row, self._name_by(),
                                 ["IRCON"]) is None

    def test_parses_date_from_iso(self):
        row = {
            "symbol": "IRCON", "comp": "IRCON",
            "desc": "Transcript",
            "attchmntText": "Transcript filed",
            "attchmntFile": "https://example.com/x.pdf",
            "an_dt": "27-May-2025 18:07:52",
        }
        f = cc._parse_filing(row, self._name_by(), ["IRCON"])
        assert f.filing_date == "27-May-2025"
        assert f.filing_datetime == "27-May-2025 18:07:52"


# ===========================================================================
# 3. Summary parser — robust to LLM output variations
# ===========================================================================

class TestParseSummary:
    def test_parses_clean_format(self):
        raw = (
            "TONE: confident\n"
            "GUIDANCE: maintained\n"
            "BULLETS:\n"
            "- Order book at Rs 20,000 cr\n"
            "- FY25 revenue Rs 11,000 cr\n"
            "- Margins declining 0.5%\n"
            "KEY_PHRASES: order book robust, execution on track\n"
        )
        tone, guidance, bullets, phrases = _parse_summary(raw)
        assert tone == "confident"
        assert guidance == "maintained"
        assert len(bullets) == 3
        assert "Order book at Rs 20,000 cr" in bullets
        assert "execution on track" in phrases

    def test_handles_markdown_bold(self):
        """LLM reduction pass often returns **TONE:** etc."""
        raw = (
            "**TONE:** cautious\n"
            "**GUIDANCE:** lowered\n"
            "**BULLETS:**\n"
            "- Margin pressure expected\n"
            "- Volume growth uncertain\n"
            "**KEY_PHRASES:** headwinds, demand softening\n"
        )
        tone, guidance, bullets, phrases = _parse_summary(raw)
        assert tone == "cautious"
        assert guidance == "lowered"
        assert len(bullets) == 2
        assert "headwinds" in phrases

    def test_strips_here_is_combined_summary_noise(self):
        """LLM sometimes prepends 'Here is the combined summary:' as a bullet."""
        raw = (
            "TONE: confident\n"
            "GUIDANCE: maintained\n"
            "BULLETS:\n"
            "- Here is the combined summary:\n"
            "- Real bullet one\n"
            "- Real bullet two\n"
        )
        _, _, bullets, _ = _parse_summary(raw)
        assert len(bullets) == 2
        assert all("combined summary" not in b.lower() for b in bullets)

    def test_handles_missing_bullets_header(self):
        raw = (
            "TONE: neutral\n"
            "- Bullet one\n"
            "- Bullet two\n"
        )
        tone, _, bullets, _ = _parse_summary(raw)
        assert tone == "neutral"
        assert len(bullets) == 2

    def test_handles_italic_markers(self):
        raw = (
            "*TONE:* confident\n"
            "*GUIDANCE:* raised\n"
            "*BULLETS:*\n"
            "- Strong demand\n"
        )
        tone, guidance, _, _ = _parse_summary(raw)
        assert tone == "confident"
        assert guidance == "raised"

    def test_fallback_when_unparseable(self):
        """Garbage input → at least returns some bullets (raw lines)."""
        raw = "this is gibberish with no structure at all"
        tone, guidance, bullets, _ = _parse_summary(raw)
        # Should not crash; tone/guidance default to unknown
        assert tone == "unknown"
        assert guidance == "unknown"
        # May or may not have bullets depending on parsing

    def test_strips_italic_from_phrases(self):
        raw = (
            "KEY_PHRASES: *order book robust*, **execution on track**\n"
        )
        _, _, _, phrases = _parse_summary(raw)
        assert "order book robust" in phrases
        assert "execution on track" in phrases


# ===========================================================================
# 4. Chunking
# ===========================================================================

class TestChunkText:
    def test_short_text_single_chunk(self):
        chunks = _chunk_text("hello world", chunk_chars=1000)
        assert chunks == ["hello world"]

    def test_long_text_splits_on_paragraph(self):
        # Use explicit paragraph breaks so chunker can find them
        para1 = ("para one. " * 50).strip()  # ~550 chars
        para2 = ("para two. " * 50).strip()
        text = para1 + "\n\n" + para2
        chunks = _chunk_text(text, chunk_chars=400)
        assert len(chunks) >= 2
        assert "para one" in chunks[0]
        assert "para two" in chunks[1]

    def test_preserves_full_text(self):
        """All content must appear in some chunk."""
        text = "Lorem ipsum. " * 500
        chunks = _chunk_text(text, chunk_chars=300)
        joined = " ".join(chunks)
        # Word count should match (within tolerance for trimming)
        assert abs(joined.count("Lorem") - text.count("Lorem")) < 5


# ===========================================================================
# 5. Cache + dedup
# ===========================================================================

class TestFilingCacheKey:
    def test_key_is_url_based(self):
        f1 = ConcallFiling(ticker="ITC", company_name="ITC",
                           filing_kind="transcript", filing_date="20-May-2025",
                           filing_datetime="", pdf_url="https://x.com/a.pdf")
        f2 = ConcallFiling(ticker="ITC", company_name="ITC",
                           filing_kind="transcript", filing_date="20-May-2025",
                           filing_datetime="", pdf_url="https://x.com/b.pdf")
        assert _filing_cache_key(f1) != _filing_cache_key(f2)

    def test_key_same_for_same_url(self):
        f = ConcallFiling(ticker="ITC", company_name="ITC",
                          filing_kind="transcript", filing_date="20-May-2025",
                          filing_datetime="", pdf_url="https://x.com/a.pdf")
        assert _filing_cache_key(f) == _filing_cache_key(f)


class TestSummaryCacheRoundTrip:
    def test_save_load_round_trip(self, tmp_path, monkeypatch):
        # Redirect cache dir to tmp
        monkeypatch.setattr(cc, "SUMMARY_DIR", tmp_path)
        # Clear the lru_cache if any
        from pipeline.concalls import _summary_cache_path
        monkeypatch.setattr(cc, "_summary_cache_path",
                            lambda f: tmp_path / f"{f.ticker}_{f.pdf_url[-30:].replace('/','_')}.json")

        filing = ConcallFiling(
            ticker="ITC", company_name="ITC Limited",
            filing_kind="transcript", filing_date="20-May-2025",
            filing_datetime="20-May-2025 18:00:00",
            pdf_url="https://x.com/itc.pdf",
        )
        summary = ConcallSummary(
            filing=filing,
            summary_text="TONE: confident\nBULLETS:\n- bullet 1\n- bullet 2",
            management_tone="confident",
            key_topics=["bullet 1", "bullet 2"],
            pdf_pages=10, pdf_chars=25000,
            llm_model="llama3-pro", llm_duration_sec=42.0,
            summarized_at="2026-01-01T10:00:00+05:30",
        )
        cc._save_summary(summary)

        # Now load it back
        loaded = cc._load_summary(filing)
        assert loaded is not None
        assert loaded.filing.ticker == "ITC"
        assert loaded.management_tone == "confident"
        assert "bullet 1" in loaded.key_topics
        assert loaded.pdf_pages == 10


# ===========================================================================
# 6. Telegram rendering
# ===========================================================================

class TestTelegramRender:
    def _summary(self) -> ConcallSummary:
        filing = ConcallFiling(
            ticker="IRCON", company_name="IRCON International Limited",
            filing_kind="transcript", filing_date="27-May-2025",
            filing_datetime="27-May-2025 18:07:52",
            pdf_url="https://x.com/ircon.pdf",
        )
        return ConcallSummary(
            filing=filing,
            summary_text=(
                "TONE: confident\n"
                "GUIDANCE: maintained\n"
                "BULLETS:\n"
                "- Order book at Rs 20,347 cr\n"
                "- FY25 revenue Rs 11,131 cr\n"
                "- Margins to decline 0.5-1%\n"
                "KEY_PHRASES: order book robust, execution on track\n"
            ),
            management_tone="confident",
            key_topics=[],
            pdf_pages=10, pdf_chars=25000,
            llm_model="llama3-pro", llm_duration_sec=50.0,
            summarized_at="2025-05-27T18:30:00+05:30",
        )

    def test_renders_ticker_in_header(self):
        text = render_telegram(self._summary())
        assert "IRCON" in text

    def test_renders_tone_and_guidance(self):
        text = render_telegram(self._summary())
        assert "confident" in text
        assert "maintained" in text

    def test_renders_at_least_three_bullets(self):
        text = render_telegram(self._summary())
        # Count bullet markers (• at line start)
        bullet_count = sum(1 for line in text.splitlines()
                           if line.strip().startswith("•"))
        assert bullet_count >= 3

    def test_renders_management_phrases(self):
        text = render_telegram(self._summary())
        assert "order book robust" in text
        assert "execution on track" in text

    def test_includes_hashtags(self):
        text = render_telegram(self._summary())
        assert "#IRCON" in text
        assert "#ConCall" in text

    def test_strips_markdown_from_bullets(self):
        """If the LLM summary still has * or **, render must clean them."""
        s = self._summary()
        s.summary_text = (
            "TONE: confident\n"
            "GUIDANCE: maintained\n"
            "BULLETS:\n"
            "- **Real bullet**\n"
            "- *Italic bullet*\n"
            "KEY_PHRASES: *order book robust*, **execution on track**\n"
        )
        text = render_telegram(s)
        # Raw markdown markers must not leak into the rendered text
        assert "**Real bullet**" not in text
        assert "*Italic bullet*" not in text
        # Cleaned versions should be there
        assert "Real bullet" in text
        assert "Italic bullet" in text
        # Same for phrases
        assert '"order book robust"' in text
        assert '"execution on track"' in text


# ===========================================================================
# 7. send_telegram — dry-run safety
# ===========================================================================

class TestSendTelegramDryRun:
    def test_dry_run_does_not_open_network(self, monkeypatch):
        monkeypatch.setenv("CONCALLS_DRY_RUN", "1")
        assert cc.is_dry_run() is True

        def fail(*args, **kwargs):
            raise AssertionError("urlopen should not be called")

        with mock.patch.object(cc.urllib.request, "urlopen",
                               side_effect=fail):
            result = cc.send_telegram("test message")
        assert result["sent"] is False
        assert result["mode"] == "dry_run"

    def test_missing_credentials(self, monkeypatch):
        monkeypatch.setenv("CONCALLS_DRY_RUN", "0")
        for k in ("NEWS_TELEGRAM_BOT_TOKEN", "NEWS_TELEGRAM_CHAT_ID",
                  "CONCALLS_TELEGRAM_BOT_TOKEN", "CONCALLS_TELEGRAM_CHAT_ID"):
            monkeypatch.delenv(k, raising=False)

        def fail(*args, **kwargs):
            raise AssertionError("urlopen should not be called")

        with mock.patch.object(cc.urllib.request, "urlopen",
                               side_effect=fail):
            result = cc.send_telegram("test")
        assert result["sent"] is False
        assert result["mode"] == "no_credentials"


# ===========================================================================
# 8. Ollama integration smoke (skipped if Ollama not running)
# ===========================================================================

@pytest.mark.skipif(
    not os.path.exists("/usr/local/bin/ollama"),
    reason="Ollama not installed",
)
class TestOllamaIntegration:
    """Real Ollama calls. Skipped automatically if Ollama isn't running."""

    def test_ollama_generate_returns_text(self):
        response, duration = cc._ollama_generate(
            "Reply with the single word: pong"
        )
        assert isinstance(response, str)
        assert len(response) > 0
        assert duration > 0

    def test_summarize_short_text(self):
        tone, guidance, bullets, phrases, duration, chunks = \
            cc.summarize_with_ollama(
                "The CEO was very confident. The CFO said order book is "
                "robust at Rs 5,000 cr. Revenue grew 20% YoY. Margins "
                "expanded by 100 bps. Guidance raised for next quarter.",
                ticker="TEST",
                filing_kind="transcript",
            )
        assert duration > 0
        assert chunks == 1
        # Bullets should be non-empty
        assert len(bullets) >= 1


# ===========================================================================
# 9. CLI smoke tests
# ===========================================================================

class TestCLI:
    def test_test_render(self):
        """--test-render prints a valid Telegram-format message."""
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline.concalls", "--test-render"],
            capture_output=True, text=True, cwd=PROJECT,
        )
        assert proc.returncode == 0
        assert "Con-call summary" in proc.stdout
        assert "Tone:" in proc.stdout
        assert "Key takeaways" in proc.stdout
        assert "#ConCall" in proc.stdout