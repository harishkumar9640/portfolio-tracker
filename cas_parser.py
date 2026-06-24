"""
cas_parser.py
-------------
Parse a CAMS / Kuvera / MFCentral Consolidated Account Statement (CAS) PDF
and extract mutual fund holdings: (scheme_name, isin, folio, units, nav, value).

Handles:
  - Password-protected PDFs (password loaded from secrets.local.json)
  - Both CAMS and Kuvera CAS layouts (they differ slightly)

Free dependencies only: pypdf
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader

PROJECT = Path(__file__).resolve().parent
SECRETS_FILE = PROJECT / "secrets.local.json"


def _load_cas_password() -> str:
    if not SECRETS_FILE.exists():
        raise RuntimeError(
            f"Missing {SECRETS_FILE}. "
            f"Copy secrets.local.json.example to secrets.local.json "
            f"and set cas_pdf_password."
        )
    raw = SECRETS_FILE.read_text()
    # Strip // and # comment lines so the example template "just works"
    # if a user copies it verbatim. JSON itself has no comment syntax.
    cleaned = "\n".join(
        line for line in raw.splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{SECRETS_FILE} is not valid JSON: {e}. "
            f"Make sure the file contains a single JSON object like "
            f'{{"cas_pdf_password": "your_pin"}}.'
        ) from e
    pw = (data.get("cas_pdf_password") or "").strip()
    if not pw or pw == "your_cas_password_here":
        raise RuntimeError("cas_pdf_password is empty in secrets.local.json")
    return pw


@dataclass
class MfHolding:
    scheme_name: str
    isin: str
    folio: str
    units: float
    nav: float            # current / latest NAV
    nav_date: str         # YYYY-MM-DD
    value: float          # units * nav

    @property
    def amfi_code(self) -> str | None:
        """We don't know the AMFI code from CAS — look it up later via mfapi.in."""
        return None


# ---------- PDF extraction ----------
def read_cas_text(pdf_path: Path) -> str:
    pw = _load_cas_password()
    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        # Some CAS PDFs use the password as both user and owner
        if not reader.decrypt(pw):
            raise RuntimeError("CAS PDF password is incorrect")
    chunks: list[str] = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks)


# ---------- Pattern helpers ----------
# Common Indian MF scheme suffixes to strip when normalizing names
_SCHEME_SUFFIX_RE = re.compile(
    r"\s*-\s*(Direct Plan|Growth|IDCW|Dividend|Bonus|Regular Plan).*$",
    re.IGNORECASE,
)
_ISIN_RE = re.compile(r"\b(INF[A-Z0-9]{9})\b")
_UNITS_RE = re.compile(r"([\d,]+\.\d+|[\d,]+)")
_FOLIO_RE = re.compile(r"Folio\s*(?:No\.?|Number)?\s*[:\-]?\s*(\S+)", re.IGNORECASE)
_NAV_RE = re.compile(r"NAV\s*[:\-]?\s*([\d,]+\.\d+)", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{2}[-/][A-Za-z]{3}[-/]\d{4}|\d{2}[-/]\d{2}[-/]\d{4})")


def _clean_name(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


# ---------- Parsers for common layouts ----------
def _parse_cams_kuvera(text: str) -> list[MfHolding]:
    """
    Generic line-based parser that works on most CAS layouts.
    We look for blocks containing: scheme name, ISIN, units, NAV, value.
    """
    holdings: list[MfHolding] = []
    lines = text.splitlines()

    # Heuristic: a line with an ISIN is a holding header
    isin_to_idx: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = _ISIN_RE.search(ln)
        if m:
            isin_to_idx.append((i, m.group(1)))

    for idx, isin in isin_to_idx:
        # Scheme name is usually on the same line as the ISIN, before it
        head = lines[idx]
        scheme = _clean_name(_ISIN_RE.sub("", head))
        scheme = _SCHEME_SUFFIX_RE.sub("", scheme).strip(" -:")
        if not scheme:
            # fall back: previous non-empty line
            for j in range(idx - 1, max(idx - 5, -1), -1):
                if lines[j].strip():
                    scheme = _clean_name(lines[j])
                    break

        # Look at the next ~10 lines for units / nav / value
        block = "\n".join(lines[idx: idx + 12])

        units = 0.0
        nav = 0.0
        nav_date = ""

        # Units is usually the first large number near "Unit" or "Quantity"
        m_units = re.search(
            r"(?:Unit(?:s)?|Quantity|Balance)\s*[:\-]?\s*([\d,]+\.\d+)",
            block, re.IGNORECASE,
        )
        if m_units:
            units = float(m_units.group(1).replace(",", ""))
        else:
            # Fallback: first decimal number in the block
            m = _UNITS_RE.search(block)
            if m:
                try:
                    units = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass

        m_nav = _NAV_RE.search(block)
        if m_nav:
            nav = float(m_nav.group(1).replace(",", ""))

        m_date = _DATE_RE.search(block)
        if m_date:
            nav_date = m_date.group(1)

        # Folio (optional) - search a wider window
        folio = ""
        wider = "\n".join(lines[max(0, idx - 3): idx + 12])
        m_folio = _FOLIO_RE.search(wider)
        if m_folio:
            folio = m_folio.group(1).strip(" .,")

        if units > 0 and nav > 0:
            holdings.append(MfHolding(
                scheme_name=scheme or "(unknown scheme)",
                isin=isin,
                folio=folio,
                units=units,
                nav=nav,
                nav_date=nav_date,
                value=units * nav,
            ))

    # Deduplicate by ISIN (CAS sometimes repeats)
    seen: set[str] = set()
    unique: list[MfHolding] = []
    for h in holdings:
        if h.isin in seen:
            continue
        seen.add(h.isin)
        unique.append(h)
    return unique


def parse_cas(pdf_path: Path) -> list[MfHolding]:
    if not pdf_path.exists():
        raise RuntimeError(f"CAS PDF not found: {pdf_path}")
    text = read_cas_text(pdf_path)
    if not text.strip():
        raise RuntimeError("CAS PDF produced no text — is it scanned/image-only?")
    return _parse_cams_kuvera(text)


# ---------- Manual list support ----------
def load_manual_mfs(json_path: Path) -> list[dict]:
    """
    Optional: instead of (or in addition to) a CAS, you can keep a small JSON
    file of MFs in the format:
      [
        {"scheme_name": "Parag Parikh Flexi Cap - Direct Growth",
         "units": 1234.567},
        ...
      ]
    """
    if not json_path.exists():
        return []
    return json.loads(json_path.read_text())


# ---------- CLI ----------
def main() -> None:
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 cas_parser.py <path-to-cas.pdf>")
        return
    pdf = Path(sys.argv[1]).expanduser()
    holdings = parse_cas(pdf)
    print(f"Found {len(holdings)} mutual fund holdings:\n")
    total = 0.0
    for h in holdings:
        print(f"  {h.scheme_name[:60]:<60} units={h.units:>12.2f}  "
              f"NAV={h.nav:>8.2f}  value=₹{h.value:>12,.2f}")
        total += h.value
    print(f"\nTotal MF value (as per CAS): ₹{total:,.2f}")


if __name__ == "__main__":
    main()
