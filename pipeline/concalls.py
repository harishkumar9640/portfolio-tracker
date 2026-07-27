"""
Con-call transcript discovery, extraction, and summarization.

Pipeline:
  1. Find transcripts / investor presentations filed via NSE
     corporate-announcements (the same endpoint earnings_alert.py uses).
  2. Download the PDF from nsearchives.nseindia.com.
  3. Extract text via pypdf.
  4. Summarize via local Ollama (llama3-pro) — privacy-preserving, free.
  5. Cache the summary on disk so we never re-summarize the same filing.
  6. Notify via Telegram with a structured bullet-point summary.
  7. Surface in the web dashboard.

What "con-call filing" looks like in NSE's data:
  - desc = "Analysts/Institutional Investor Meet/Con. Call Updates"
  - attchmntText contains "Transcript" or "Link of Recording" or
    "Investor Presentation"
  - attchmntFile = "https://nsearchives.nseindia.com/corporate/<file>.pdf"

Schedule: triggered automatically when a new transcript filing appears.
  - The orchestrator runs once at 18:45 IST (same as flows_alert)
  - Or you can run on-demand:  python concalls.py --run-once --ticker ITC

Configuration (env vars, all optional):
  OLLAMA_URL            default: http://localhost:11434
  OLLAMA_MODEL          default: llama3-pro:latest
  CONCALLS_DRY_RUN      default: 1  (set 0 to actually send Telegram)
  CONCALLS_MAX_PAGES    default: 50  (skip PDFs larger than this)
  CONCALLS_CHUNK_CHARS  default: 6000  (per-chunk prompt size)

Usage:
    python concalls.py --run-once
    python concalls.py --ticker ITC
    python concalls.py --test-render          # sample render from cache
    python concalls.py --start-scheduler
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .portfolio_impact import PORTFOLIO_EXPOSURE  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

from pipeline.runtime_paths import data_root

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONCALLS_DIR = data_root() / "alerts" / "concalls"
LOG_FILE = _CONCALLS_DIR / "run.log"
SEEN_FILE = _CONCALLS_DIR / "seen.json"          # dedup: filing_key -> date
SUMMARY_DIR = _CONCALLS_DIR / "cache"            # one .json per filing
LOG_FILE_HISTORY = _CONCALLS_DIR / "log.json"    # alert send history
SUMMARY_DIR.mkdir(parents=True, exist_ok=True)

NSE_ANNOUNCEMENTS_URL = (
    "https://www.nseindia.com/api/corporate-announcements"
)
NSE_ARCHIVES_BASE = "https://nsearchives.nseindia.com/corporate/"

# Keywords that mark a filing as a con-call-related artifact.
TRANSCRIPT_KEYWORDS = ("transcript",)
PRESENTATION_KEYWORDS = ("investor presentation",)
RECORDING_KEYWORDS = ("link of recording", "audio recording", "audio",
                      "recording of")

# LLM configuration
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3-pro:latest")
CONCALLS_MAX_PAGES = int(os.environ.get("CONCALLS_MAX_PAGES", "50"))
CONCALLS_CHUNK_CHARS = int(os.environ.get("CONCALLS_CHUNK_CHARS", "6000"))

# HTTP
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,application/pdf,text/html",
    "Accept-Language": "en-IN,en;q=0.9",
}
HTTP_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("concalls")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ConcallFiling:
    """One con-call-related filing discovered on NSE."""
    ticker: str
    company_name: str
    filing_kind: str              # "transcript" | "investor_presentation" | "audio_recording"
    filing_date: str              # NSE format: "27-May-2025"
    filing_datetime: str          # full ISO timestamp from NSE
    pdf_url: str
    nse_attch_text: str = ""
    nse_desc: str = ""


@dataclass
class ConcallSummary:
    """The LLM-generated summary for one filing, plus metadata."""
    filing: ConcallFiling
    summary_text: str             # the LLM output (markdown bullets)
    management_tone: str          # "confident" | "cautious" | "neutral" | ...
    key_topics: list[str] = field(default_factory=list)
    raw_pdf_path: Optional[str] = None
    pdf_pages: int = 0
    pdf_chars: int = 0
    llm_model: str = ""
    llm_duration_sec: float = 0.0
    summarized_at: str = ""


# ---------------------------------------------------------------------------
# NSE cookie-aware session (same pattern as earnings_alert)
# ---------------------------------------------------------------------------

from http.cookiejar import CookieJar


_NSE_OPENER: Optional[urllib.request.OpenerDirector] = None


def _get_nse_opener() -> urllib.request.OpenerDirector:
    global _NSE_OPENER
    if _NSE_OPENER is None:
        jar = CookieJar()
        _NSE_OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
        )
    return _NSE_OPENER


def _prime_nse_session() -> None:
    opener = _get_nse_opener()
    req = urllib.request.Request(
        "https://www.nseindia.com/", headers=HTTP_HEADERS,
    )
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as r:
            r.read()
        log.debug("NSE session primed")
    except Exception as e:
        log.debug("NSE prime failed: %s", e)


def _http_get_nse(url: str, params: Optional[dict] = None) -> str:
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    opener = _get_nse_opener()
    with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_download(url: str) -> bytes:
    """Download bytes (for PDFs). Uses Playwright for cookie bootstrap."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=HTTP_HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
            locale="en-IN", timezone_id="Asia/Kolkata",
        )
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined});"
        )
        try:
            page.goto("https://www.nseindia.com/",
                      wait_until="load", timeout=30000)
        except Exception:
            pass
        time.sleep(2)
        resp = page.request.get(
            url,
            headers={"Accept": "application/pdf,*/*",
                     "Referer": "https://www.nseindia.com/"},
        )
        body = resp.body() if resp.status == 200 else b""
        browser.close()
    return body


# ---------------------------------------------------------------------------
# Filing discovery
# ---------------------------------------------------------------------------

def _classify_filing(desc: str, attch: str) -> Optional[str]:
    """Return 'transcript' | 'investor_presentation' | 'audio_recording'
    based on NSE's desc + attchmntText, or None if not a con-call filing."""
    desc_l = (desc or "").lower()
    attch_l = (attch or "").lower()

    # Transcript has highest signal-to-noise — must mention transcript
    if "transcript" in attch_l or "transcript" in desc_l:
        return "transcript"
    # Recording = audio only, no text body
    if any(kw in attch_l for kw in RECORDING_KEYWORDS):
        return "audio_recording"
    # Investor presentation = the slide deck (text-rich, useful for context)
    if any(kw in desc_l for kw in PRESENTATION_KEYWORDS):
        return "investor_presentation"
    # Con-call updates without specifying transcript = schedule notice only,
    # not the actual content. Skip.
    return None


def _parse_filing(row: dict, name_by_ticker: dict[str, list[str]],
                  tickers: list[str]) -> Optional[ConcallFiling]:
    """Map one NSE announcement row to a ConcallFiling (or None)."""
    sym = (row.get("symbol") or "").upper().strip()
    comp = (row.get("comp") or "").lower()
    desc = row.get("desc") or ""
    attch = row.get("attchmntText") or ""
    file_url = row.get("attchmntFile") or ""
    date_iso = row.get("an_dt") or ""

    kind = _classify_filing(desc, attch)
    if kind is None:
        return None

    matched_ticker: Optional[str] = None
    if sym in tickers:
        matched_ticker = sym
    if not matched_ticker:
        # alias fallback
        for tkr in tickers:
            for needle in name_by_ticker.get(tkr, []):
                if not needle or len(needle) < 5:
                    continue
                if needle in comp:
                    matched_ticker = tkr
                    break
            if matched_ticker:
                break
    if not matched_ticker:
        return None

    if not file_url.startswith("http"):
        return None

    # Parse NSE date "27-May-2025 18:07:52" → date "27-May-2025"
    filing_date = date_iso.split()[0] if date_iso else ""

    return ConcallFiling(
        ticker=matched_ticker,
        company_name=row.get("comp") or matched_ticker,
        filing_kind=kind,
        filing_date=filing_date,
        filing_datetime=date_iso,
        pdf_url=file_url,
        nse_attch_text=attch,
        nse_desc=desc,
    )


def find_recent_filings(
    tickers: list[str], days_back: int = 7,
    today: Optional[datetime] = None,
) -> list[ConcallFiling]:
    """Find con-call filings filed in the last `days_back` days."""
    today = today or datetime.now(IST)
    from_date = today - timedelta(days=days_back)

    # Build name-by-ticker (full alias set, like earnings_alert)
    name_by_ticker: dict[str, list[str]] = {}
    for tkr, info in PORTFOLIO_EXPOSURE.items():
        names = {info["name"].lower()}
        names.update(a.lower() for a in info["aliases"])
        names.add(tkr.lower())
        name_by_ticker[tkr.upper()] = list(names)

    _prime_nse_session()
    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": today.strftime("%d-%m-%Y"),
    }
    try:
        body = _http_get_nse(NSE_ANNOUNCEMENTS_URL, params=params)
        rows = json.loads(body)
    except Exception as e:
        log.warning("NSE fetch failed: %s", e)
        return []

    out: list[ConcallFiling] = []
    for r in rows:
        f = _parse_filing(r, name_by_ticker, tickers)
        if f is not None:
            out.append(f)
    log.info("found %d con-call filings in last %d days",
             len(out), days_back)
    return out


# ---------------------------------------------------------------------------
# PDF download + text extraction
# ---------------------------------------------------------------------------

def download_and_extract(filing: ConcallFiling) -> tuple[str, int, int]:
    """Download the PDF and extract text. Returns (text, pages, char_count).

    Empty string on failure. Skips PDFs over CONCALLS_MAX_PAGES.
    """
    try:
        pdf_bytes = _http_download(filing.pdf_url)
    except Exception as e:
        log.warning("PDF download failed for %s: %s", filing.pdf_url, e)
        return "", 0, 0

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        log.warning("non-PDF response for %s (first 80: %r)",
                    filing.pdf_url, pdf_bytes[:80])
        return "", 0, 0

    # Save PDF to cache dir
    pdf_path = SUMMARY_DIR / f"{filing.ticker}_{filing.filing_date}_" \
        f"{filing.filing_kind}.pdf"
    try:
        pdf_path.write_bytes(pdf_bytes)
    except Exception as e:
        log.warning("could not save PDF to %s: %s", pdf_path, e)
        pdf_path = None  # type: ignore

    try:
        import pypdf
    except ImportError:
        log.error("pypdf not installed; cannot extract PDF text")
        return "", 0, 0

    try:
        reader = pypdf.PdfReader(str(pdf_path) if pdf_path else
                                  __import__("io").BytesIO(pdf_bytes))
        if len(reader.pages) > CONCALLS_MAX_PAGES:
            log.info("skipping %s: %d pages > max %d",
                     filing.ticker, len(reader.pages), CONCALLS_MAX_PAGES)
            return "", len(reader.pages), 0

        chunks: list[str] = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception as e:
                log.debug("page extract failed: %s", e)
        text = "\n".join(chunks)
        log.info("extracted %d pages, %d chars from %s",
                 len(reader.pages), len(text), filing.ticker)
        return text, len(reader.pages), len(text)
    except Exception as e:
        log.exception("PDF parse failed for %s", filing.ticker)
        return "", 0, 0


# ---------------------------------------------------------------------------
# LLM summarization (Ollama)
# ---------------------------------------------------------------------------

SUMMARIZATION_PROMPT = """You are an expert equity-research analyst summarising an Indian listed company's
post-earnings conference call transcript or investor presentation.

Read the following excerpt and produce a CONCISE summary in this exact format:

TONE: <one of: confident | cautious | neutral | defensive | mixed>
GUIDANCE: <one of: raised | maintained | lowered | no_guidance>
BULLETS:
- <bullet 1: most material new information or metric>
- <bullet 2: management commentary on demand / order book / margins>
- <bullet 3: forward-looking commentary or capex / expansion plans>
- <bullet 4: key risk or headwind mentioned>
- <bullet 5: analyst question that received a telling answer>
KEY_PHRASES: <2-4 verbatim short phrases management used, comma-separated>

Be specific. Use numbers, percentages, and ₹ crores when present.
Do NOT editorialize or add information not in the text.
Do NOT start with phrases like "The transcript shows" or "Management said".
Each bullet must be one sentence, <25 words.

EXCERPT:
\"\"\"
{text}
\"\"\"
"""


def _ollama_generate(prompt: str, model: str = OLLAMA_MODEL) -> tuple[str, float]:
    """Call Ollama's /api/generate and return (response_text, duration_sec)."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,    # lower = more deterministic
            "num_predict": 800,    # cap output
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    duration = time.monotonic() - t0
    return body.get("response", ""), duration


def _chunk_text(text: str, chunk_chars: int = CONCALLS_CHUNK_CHARS
                ) -> list[str]:
    """Split a long transcript into chunks, breaking on paragraph boundaries."""
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= chunk_chars:
            chunks.append(text)
            break
        # Find a paragraph boundary near chunk_chars
        cut = text.rfind("\n\n", 0, chunk_chars)
        if cut == -1:
            cut = text.rfind(". ", 0, chunk_chars)
            if cut != -1:
                cut += 2
        if cut == -1 or cut < chunk_chars // 2:
            cut = chunk_chars
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return chunks


def _parse_summary(raw: str) -> tuple[str, str, list[str], list[str]]:
    """Parse the LLM output into structured fields.

    Returns (tone, guidance, bullets, key_phrases).
    Falls back to raw text if the LLM didn't follow the format.
    Tolerates markdown bold markers (**...**) which the LLM often adds
    in the reduction pass.
    """
    tone = "unknown"
    guidance = "unknown"
    bullets: list[str] = []
    phrases: list[str] = []

    # Strip markdown bold/italic markers before line matching
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", raw)
    cleaned = re.sub(r"\*(.*?)\*", r"\1", cleaned)

    in_bullets = False
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("TONE:"):
            tone = line[5:].strip().lower()
            in_bullets = False
        elif line.upper().startswith("GUIDANCE:"):
            guidance = line[9:].strip().lower()
            in_bullets = False
        elif line.upper().startswith("KEY_PHRASES:"):
            in_bullets = False
            ph = line[len("KEY_PHRASES:"):].strip()
            phrases = [p.strip().strip('"\'')
                       for p in ph.split(",") if p.strip()]
        elif line.upper().startswith("BULLETS:"):
            in_bullets = True
        elif in_bullets and line.startswith("-"):
            b = line.lstrip("-").strip()
            if b and not b.lower().startswith("here is the combined"):
                bullets.append(b)
        elif line.startswith("-"):
            # No BULLETS: header — treat all leading dashes as bullets
            b = line.lstrip("-").strip()
            if b and not b.lower().startswith("here is the combined"):
                bullets.append(b)

    if not bullets:
        bullets = [l.strip() for l in cleaned.splitlines()
                   if l.strip() and ":" not in l[:20]]
    return tone, guidance, bullets, phrases


def summarize_with_ollama(
    text: str, ticker: str, filing_kind: str,
) -> tuple[str, str, list[str], list[str], float, int]:
    """Summarize via local Ollama. Returns
        (tone, guidance, bullets, key_phrases, duration_sec, num_chunks).
    """
    chunks = _chunk_text(text)
    log.info("summarizing %s (%s): %d chunks, %d chars total",
             ticker, filing_kind, len(chunks), len(text))

    partial_summaries: list[str] = []
    total_duration = 0.0
    for i, chunk in enumerate(chunks):
        prompt = SUMMARIZATION_PROMPT.format(text=chunk)
        try:
            response, duration = _ollama_generate(prompt)
            total_duration += duration
            partial_summaries.append(response.strip())
            log.debug("chunk %d/%d summarised in %.1fs",
                      i + 1, len(chunks), duration)
        except Exception as e:
            log.warning("ollama call failed on chunk %d: %s", i + 1, e)
            return "unknown", "unknown", [], [], total_duration, len(chunks)

    # If multiple chunks, run a final reduction pass.
    if len(partial_summaries) > 1:
        combined = "\n\n".join(partial_summaries)
        reduction_prompt = (
            "Combine these partial summaries of the same company con-call "
            "into ONE final summary. Output PLAIN TEXT ONLY with no markdown "
            "formatting (no **, no *, no backticks). Use this exact format:\n\n"
            "TONE: <confident|cautious|neutral|defensive|mixed>\n"
            "GUIDANCE: <raised|maintained|lowered|no_guidance>\n"
            "BULLETS:\n"
            "- <bullet 1>\n"
            "- <bullet 2>\n"
            "- <bullet 3>\n"
            "- <bullet 4>\n"
            "- <bullet 5>\n"
            "KEY_PHRASES: <phrase 1>, <phrase 2>, <phrase 3>\n\n"
            f"PARTIAL SUMMARIES:\n{combined}\n"
        )
        try:
            response, duration = _ollama_generate(reduction_prompt)
            total_duration += duration
        except Exception as e:
            log.warning("reduction pass failed: %s", e)
            response = "\n\n".join(partial_summaries)

    tone, guidance, bullets, phrases = _parse_summary(response)
    return tone, guidance, bullets, phrases, total_duration, len(chunks)


# ---------------------------------------------------------------------------
# Cache + dedup
# ---------------------------------------------------------------------------

def _filing_cache_key(filing: ConcallFiling) -> str:
    """Unique key for one filing. URL is unique per NSE filing."""
    return f"{filing.ticker}|{filing.pdf_url}"


def _summary_cache_path(filing: ConcallFiling) -> Path:
    safe_url = re.sub(r"[^A-Za-z0-9._-]", "_", filing.pdf_url)[-80:]
    return SUMMARY_DIR / f"{filing.ticker}_{filing.filing_date}_" \
        f"{filing.filing_kind}_{safe_url}.json"


def _load_summary(filing: ConcallFiling) -> Optional[ConcallSummary]:
    path = _summary_cache_path(filing)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # Reconstruct nested objects
        fd = data["filing"]
        filing_recon = ConcallFiling(**fd)
        summary = ConcallSummary(
            filing=filing_recon,
            summary_text=data["summary_text"],
            management_tone=data["management_tone"],
            key_topics=data.get("key_topics", []),
            raw_pdf_path=data.get("raw_pdf_path"),
            pdf_pages=data.get("pdf_pages", 0),
            pdf_chars=data.get("pdf_chars", 0),
            llm_model=data.get("llm_model", ""),
            llm_duration_sec=data.get("llm_duration_sec", 0.0),
            summarized_at=data.get("summarized_at", ""),
        )
        # Parse out tone/guidance/bullets/phrases from stored summary_text
        return summary
    except Exception as e:
        log.warning("could not load cached summary %s: %s", path.name, e)
        return None


def _save_summary(summary: ConcallSummary) -> None:
    path = _summary_cache_path(summary.filing)
    try:
        path.write_text(json.dumps({
            "filing": asdict(summary.filing),
            "summary_text": summary.summary_text,
            "management_tone": summary.management_tone,
            "key_topics": summary.key_topics,
            "raw_pdf_path": summary.raw_pdf_path,
            "pdf_pages": summary.pdf_pages,
            "pdf_chars": summary.pdf_chars,
            "llm_model": summary.llm_model,
            "llm_duration_sec": summary.llm_duration_sec,
            "summarized_at": summary.summarized_at,
        }, indent=2))
    except Exception as e:
        log.warning("could not save summary %s: %s", path.name, e)


def _load_seen() -> dict[str, str]:
    if not SEEN_FILE.exists():
        return {}
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}


def _save_seen(seen: dict[str, str]) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def is_dry_run() -> bool:
    return _env("CONCALLS_DRY_RUN", "1") not in ("0", "false", "False")


def send_telegram(text: str) -> dict:
    if is_dry_run():
        log.info("[DRY-RUN] would send %d chars:\n%s", len(text), text)
        return {"sent": False, "mode": "dry_run", "chars": len(text)}

    bot = (_env("NEWS_TELEGRAM_BOT_TOKEN")
           or _env("CONCALLS_TELEGRAM_BOT_TOKEN"))
    chat = (_env("NEWS_TELEGRAM_CHAT_ID")
            or _env("CONCALLS_TELEGRAM_CHAT_ID"))
    if not bot or not chat:
        return {"sent": False, "mode": "no_credentials", "chars": len(text)}

    payload = json.dumps({
        "chat_id": chat,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            log.info("Telegram OK: %d chars", len(text))
            return {"sent": True, "mode": "telegram", "chars": len(text)}
    except Exception as e:
        log.exception("Telegram send failed")
        return {"sent": False, "mode": "error", "error": str(e)}


def _escape_md(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)


def render_telegram(summary: ConcallSummary) -> str:
    """Render a con-call summary for Telegram."""
    filing = summary.filing
    kind_emoji = {
        "transcript": "📞",
        "investor_presentation": "📊",
        "audio_recording": "🎙️",
    }.get(filing.filing_kind, "📄")
    kind_label = filing.filing_kind.replace("_", " ").title()

    # Re-parse the structured summary text for the alert
    tone, guidance, bullets, phrases = _parse_summary(summary.summary_text)

    lines: list[str] = []
    lines.append(f"{kind_emoji} *Con-call summary — {filing.ticker}*")
    lines.append("")
    lines.append(
        f"📊 *{filing.company_name}* — {kind_label} ({filing.filing_date})"
    )
    lines.append(
        f"🎯 *Tone:* {tone}  ·  *Guidance:* {guidance}"
    )

    if bullets:
        lines.append("")
        lines.append("📌 *Key takeaways:*")
        for b in bullets[:5]:
            # Strip any residual markdown markers the LLM left in
            cleaned_b = re.sub(r"\*+", "", b).strip()
            lines.append(f"   • {_escape_md(cleaned_b)}")

    if phrases:
        lines.append("")
        lines.append("🗣️ *Management phrases to remember:*")
        for p in phrases[:4]:
            cleaned_p = re.sub(r"\*+", "", p).strip()
            lines.append(f"   • \"{_escape_md(cleaned_p)}\"")

    lines.append("")
    lines.append(
        f"📖 _Summarised from {summary.pdf_pages}-page "
        f"{filing.filing_kind} via local Ollama "
        f"({summary.llm_duration_sec:.0f}s)_"
    )
    lines.append(f"\\#{filing.ticker} #ConCall #Earnings")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_filing(
    filing: ConcallFiling, force_resummarize: bool = False,
) -> Optional[ConcallSummary]:
    """Run the full pipeline for one filing: download → extract → summarise.
    Returns the summary (or None if it should be skipped)."""
    # 1. Cached?
    if not force_resummarize:
        cached = _load_summary(filing)
        if cached is not None:
            log.info("using cached summary for %s %s",
                     filing.ticker, filing.filing_date)
            return cached

    # 2. Download + extract
    text, pages, chars = download_and_extract(filing)
    if not text or chars < 200:
        log.info("no usable text from %s PDF; skipping summary",
                 filing.ticker)
        return None

    # 3. Summarise via Ollama
    tone, guidance, bullets, phrases, duration, chunks = \
        summarize_with_ollama(text, filing.ticker, filing.filing_kind)

    summary_text = "\n".join([
        f"TONE: {tone}",
        f"GUIDANCE: {guidance}",
        "BULLETS:",
        *(f"- {b}" for b in bullets),
        f"KEY_PHRASES: {', '.join(phrases)}",
    ])

    summary = ConcallSummary(
        filing=filing,
        summary_text=summary_text,
        management_tone=tone,
        key_topics=bullets,
        pdf_pages=pages,
        pdf_chars=chars,
        llm_model=OLLAMA_MODEL,
        llm_duration_sec=duration,
        summarized_at=datetime.now(IST).isoformat(timespec="seconds"),
    )
    _save_summary(summary)
    log.info("saved summary for %s %s (%d bullets, %.1fs)",
             filing.ticker, filing.filing_date, len(bullets), duration)
    return summary


def run_once(
    days_back: int = 7,
    today: Optional[datetime] = None,
    force_send: bool = False,
    only_ticker: Optional[str] = None,
) -> dict:
    """Discover → process → alert."""
    today = today or datetime.now(IST)
    tickers = ([only_ticker.upper()] if only_ticker
               else list(PORTFOLIO_EXPOSURE.keys()))

    filings = find_recent_filings(tickers, days_back=days_back, today=today)
    log.info("found %d con-call filings for tickers=%s",
             len(filings), tickers)

    seen = _load_seen()
    sent_count = 0
    skipped_count = 0
    errors: list[str] = []

    for filing in filings:
        key = _filing_cache_key(filing)
        if not force_send and key in seen:
            skipped_count += 1
            continue
        try:
            summary = process_filing(filing)
        except Exception as e:
            log.exception("process_filing failed for %s", filing.ticker)
            errors.append(f"{filing.ticker}: {e}")
            continue

        if summary is None:
            # Not summarisable (audio-only or extraction failed)
            # Still mark seen so we don't retry
            seen[key] = today.strftime("%Y-%m-%d")
            skipped_count += 1
            continue

        text = render_telegram(summary)
        result = send_telegram(text)
        seen[key] = today.strftime("%Y-%m-%d")
        sent_count += 1

        # Append to log
        with LOG_FILE_HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": today.isoformat(timespec="seconds"),
                "ticker": filing.ticker,
                "filing_kind": filing.filing_kind,
                "filing_date": filing.filing_date,
                "tone": summary.management_tone,
                "sent": result.get("sent", False),
                "mode": result.get("mode"),
                "llm_duration_sec": summary.llm_duration_sec,
            }) + "\n")

    _save_seen(seen)
    return {
        "ran_at": today.isoformat(timespec="seconds"),
        "filings_found": len(filings),
        "summaries_sent": sent_count,
        "skipped": skipped_count,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _next_run_ist(hour: int, minute: int) -> datetime:
    now = datetime.now(IST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _scheduler_loop(stop_event: threading.Event,
                    hour: int, minute: int) -> None:
    while not stop_event.is_set():
        target = _next_run_ist(hour, minute)
        wait_s = (target - datetime.now(IST)).total_seconds()
        log.info("concalls scheduler: next run at %02d:%02d IST "
                 "(in %.0fs)", hour, minute, wait_s)
        if stop_event.wait(timeout=wait_s):
            return
        try:
            run_once()
        except Exception as e:
            log.exception("scheduled run failed: %s", e)


def start_daily_scheduler(hour: int = 19, minute: int = 0) -> threading.Event:
    """Start background daemon. Default: 7:00 PM IST."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return threading.Event()
        _scheduler_started = True
        stop_event = threading.Event()
        t = threading.Thread(
            target=_scheduler_loop, args=(stop_event, hour, minute),
            name="concalls_scheduler", daemon=True,
        )
        t.start()
        log.info("concalls scheduler started (runs at %02d:%02d IST)",
                 hour, minute)
        return stop_event


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Con-call transcript summarizer")
    p.add_argument("--run-once", action="store_true")
    p.add_argument("--ticker", metavar="TICKER",
                   help="Process only this ticker")
    p.add_argument("--days-back", type=int, default=7)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="Re-summarize even if cached")
    p.add_argument("--start-scheduler", action="store_true")
    p.add_argument("--test-render", action="store_true")
    args = p.parse_args()

    if args.dry_run:
        os.environ["CONCALLS_DRY_RUN"] = "1"

    if args.test_render:
        # Synthesise a sample summary for IRCON-style content
        sample_filing = ConcallFiling(
            ticker="IRCON",
            company_name="IRCON International Limited",
            filing_kind="transcript",
            filing_date="27-May-2025",
            filing_datetime="27-May-2025 18:07:52",
            pdf_url="https://example.com/transcript.pdf",
        )
        sample = ConcallSummary(
            filing=sample_filing,
            summary_text=(
                "TONE: confident\n"
                "GUIDANCE: maintained\n"
                "BULLETS:\n"
                "- FY25 revenue Rs 11,131 cr, PAT Rs 728 cr, EPS Rs 7.73\n"
                "- Order book Rs 20,347 cr at year-end (90% domestic)\n"
                "- Final dividend Re.1 + interim Rs 1.65 = total Rs 2.65/share\n"
                "- International projects face geopolitical headwinds\n"
                "- Q4 margin dip due to one-off provisioning, not structural\n"
                "KEY_PHRASES: 'order book robust', 'execution on track', "
                "'geopolitical headwinds'\n"
            ),
            management_tone="confident",
            key_topics=[],
            pdf_pages=10,
            pdf_chars=25000,
            llm_model="llama3-pro:latest",
            llm_duration_sec=42.0,
            summarized_at=datetime.now(IST).isoformat(timespec="seconds"),
        )
        print(render_telegram(sample))
        return

    if args.start_scheduler:
        stop = start_daily_scheduler()
        try:
            while not stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            stop.set()
        return

    result = run_once(
        days_back=args.days_back,
        force_send=args.force,
        only_ticker=args.ticker,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()