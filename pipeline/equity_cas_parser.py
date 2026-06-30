"""
equity_cas_parser.py
-------------------
Parse CDSL Consolidated Account Statement (CAS) PDFs and extract
every equity transaction: (date, ISIN, security, op_bal, credit, debit,
cl_bal, stamp_duty).

Handles:
  - Password-protected PDFs (uses 'equity_cas_password' from secrets.local.json)
  - The bilingual CDSL CAS layout (English + Devanagari headers,
    garbled when extracted with pypdf)
  - Both encrypted (AES) and unencrypted files

Output: list of EquityTransaction named tuples, ready for lot tracking.

Free dependencies only: pypdf
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import pypdf

PROJECT = Path(__file__).resolve().parent.parent
SECRETS_FILE = PROJECT / "secrets.local.json"

# Known ISINs for your 8 portfolio stocks (used for verification + filtering)
PORTFOLIO_ISINS = {
    # Add as we discover them
}


def _load_cas_password() -> str:
    """Load the equity CAS password from secrets.local.json.
    Strips JS-style comments before parsing JSON."""
    raw = SECRETS_FILE.read_text()
    stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
    secrets = json.loads(stripped)
    pwd = secrets.get("equity_cas_password") or secrets.get("cas_pdf_password")
    if not pwd:
        raise RuntimeError(
            "no equity_cas_password or cas_pdf_password in secrets.local.json"
        )
    return pwd


@dataclass
class EquityTransaction:
    """One transaction row from a CDSL CAS."""
    isin: str
    security: str
    date: str           # ISO format YYYY-MM-DD
    op_bal: float
    credit: float       # shares bought
    debit: float        # shares sold
    cl_bal: float
    stamp_duty: float   # ₹
    source_file: str    # which CAS this came from

    def __post_init__(self):
        # Validate basic sanity
        if not self.isin or len(self.isin) != 12:
            raise ValueError(f"invalid ISIN: {self.isin!r}")
        if not self.date:
            raise ValueError("missing date")


def read_cas_text(pdf_path: Path) -> tuple[str, str]:
    """Open a CDSL CAS PDF and return (full_text, period_label).

    period_label is e.g. '01-12-2024 to 31-12-2024' parsed from the header.
    Raises RuntimeError if the PDF is encrypted and the password is wrong.
    """
    pwd = _load_cas_password()
    try:
        reader = pypdf.PdfReader(str(pdf_path), password=pwd)
    except Exception as e:
        # Try without password (some files are unencrypted)
        try:
            reader = pypdf.PdfReader(str(pdf_path))
        except Exception:
            raise RuntimeError(
                f"could not open {pdf_path.name}: {e}. "
                f"Check 'equity_cas_password' in secrets.local.json"
            )
    full_text = ""
    for page in reader.pages:
        full_text += page.extract_text() + "\n"
    # Extract the period label (e.g. "01-12-2024 TO 31-12-2024")
    m = re.search(
        r"PERIOD\s+FROM\s+(\d{2}-\d{2}-\d{4})\s+TO\s+(\d{2}-\d{2}-\d{4})",
        full_text, re.IGNORECASE,
    )
    if m:
        period = f"{m.group(1)} to {m.group(2)}"
    else:
        period = pdf_path.stem
    return full_text, period


# Pattern for the transaction table. CDSL layout, after extracting with
# pypdf, looks roughly like:
#   ISIN Security Particulars Date Op.Bal Credit Debit Cl.Bal Stamp
#   INE040A01034 HDFC BANK LIMITED... 20-12-2024 1.000 -- 1.000 0.000 0
# The bilingual headers are garbled; we focus on the data rows.
#
# The data rows we care about have a 12-char ISIN, a date in DD-MM-YYYY,
# and 4 numeric fields (op/credit/debit/cl).
#
# Note: the "security" field may span multiple lines (e.g. "HDFC BANK
# LIMITED#NEW EQUITY SHARES WITH FACE VALUE RE. 1/- AFTER SUBDIVISION")
# and the "particulars" field has txn IDs. The text extraction tends
# to wrap differently for different PDFs, so we parse line-by-line
# looking for ISIN + date + 4 numbers.

# CDSL ISINs are 12 chars: 3-letter prefix + 9 alphanumeric.
# Equity:  INE prefix (3+9=12)
# MF:       INF prefix
ISIN_EQUITY_RE = re.compile(r"\b(INE[A-Z0-9]{9})\b")
ISIN_MF_RE     = re.compile(r"\b(INF[A-Z0-9]{9})\b")
DATE_RE       = re.compile(r"\b(\d{2}-\d{2}-\d{4})\b")   # DD-MM-YYYY
NUM_TOKEN_RE  = re.compile(r"-?\d+\.\d+|--")              # numeric or "--"


def _parse_transaction_row(line: str, next_lines: list[str],
                            source_file: str) -> EquityTransaction | None:
    """Try to extract one transaction from a line (and possibly the next
    line if the security name wrapped onto the next line)."""
    isin_m = ISIN_EQUITY_RE.search(line)
    if not isin_m:
        return None
    isin = isin_m.group(1)
    date_m = DATE_RE.search(line)
    if not date_m:
        return None
    date_str = date_m.group(1)

    # Find the 5 numeric fields. CDSL format has them after the date:
    # op_bal, credit, debit, cl_bal, stamp_duty
    # Some have "--" for zero, others have "0.000".
    after_date = line[date_m.end():]

    # Sometimes pypdf joins numbers + next-line text together, so the
    # 5 fields can span two visual lines. Merge the next non-table-line
    # if we don't have 5 tokens yet.
    if next_lines and len(NUM_TOKEN_RE.findall(after_date)) < 5:
        lookahead = next_lines[0]
        # Only merge if lookahead doesn't start with a new ISIN/date
        # (those would be a new transaction row)
        if not (ISIN_EQUITY_RE.match(lookahead) or
                ISIN_MF_RE.match(lookahead) or
                DATE_RE.match(lookahead)):
            after_date = after_date + " " + lookahead

    nums = NUM_TOKEN_RE.findall(after_date)
    if len(nums) < 5:
        return None
    # The expected pattern is: op, credit, debit, cl, stamp.
    # Sometimes the table also has a Portfolio Value at the end
    # (in the current-holdings section). Take only the first 5.
    nums = nums[:5]

    def to_float(s: str) -> float:
        if s == "--" or not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0

    op_bal = to_float(nums[0])
    credit = to_float(nums[1])
    debit = to_float(nums[2])
    cl_bal = to_float(nums[3])
    stamp_duty = to_float(nums[4])

    # Skip rows that aren't actual transactions: opening balance only.
    if credit == 0 and debit == 0:
        return None

    # Extract security name: between ISIN and date (single line).
    sec_start = isin_m.end()
    sec_end = date_m.start()
    security = line[sec_start:sec_end].strip()
    # If empty, pull from the next line (security wrapped to next line)
    if not security and next_lines:
        security = next_lines[0].strip()[:80]
    # Clean up garbled Devanagari and # markers
    security = re.sub(r"\s+", " ", security)
    security = security.replace("\ufffd", "")
    if "#" in security:
        security = security.split("#")[0].strip()

    # Convert DD-MM-YYYY to ISO YYYY-MM-DD
    try:
        d, m, y = date_str.split("-")
        iso_date = f"{y}-{m}-{d}"
    except ValueError:
        return None

    return EquityTransaction(
        isin=isin,
        security=security[:80],
        date=iso_date,
        op_bal=op_bal,
        credit=credit,
        debit=debit,
        cl_bal=cl_bal,
        stamp_duty=stamp_duty,
        source_file=source_file,
    )


# CDSL CAS layout (after pypdf extraction) has TWO patterns for a
# transaction row. The data is spread across ~5-10 lines:
#
# Pattern A — short security name, single line per field:
#   LINE: 'INE386C01029'                                (ISIN alone)
#   LINE: 'ASTRA MICROWAVE'                              (security line 1)
#   LINE: 'PRODUCTS LIMITED -'                           (security line 2)
#   LINE: 'EP-DR Txn:06043342'                          (txn ID line 1)
#   LINE: '16-05-2025 7.000 -- 7.000 0.000 0'          (date + 5 numbers)
#
# Pattern B — security + txn ID on same line as ISIN:
#   LINE: 'INE154A01025 ITC LIMITED - EQUITY SHARES ... 240.000 -- -- -- 240.000 287.0000 68,880.00'
#
# We detect Pattern A by finding the 'date + 5 numbers' line first,
# then walking back to find the ISIN. Pattern B has ISIN + date
# on the same line, which we also handle.

# Match a line that has just a date + 5 numeric tokens.
# CDSL format quirk: the stamp duty field is often just '0' (no decimals)
# while the qty/price fields are like '7.000'. So we accept both.
DATE_NUMS_RE = re.compile(
    r"^\s*(\d{2}-\d{2}-\d{4})\s+"
    r"(-?\d+\.\d+|--)\s+(-?\d+\.\d+|--)\s+"
    r"(-?\d+\.\d+|--)\s+(-?\d+\.\d+|--)\s+(-?\d+(?:\.\d+)?|--)\s*$"
)


def _walk_back_to_isin(lines: list[str], start: int, max_back: int = 12,
                        ) -> tuple[str, str] | None:
    """Starting from a 'date + numbers' line, walk back to find the
    most recent ISIN and the security name (lines between)."""
    for k in range(start - 1, max(start - max_back, -1), -1):
        line = lines[k]
        isin_m = ISIN_EQUITY_RE.search(line)
        if isin_m:
            isin = isin_m.group(1)
            # Security name is everything between the ISIN line and
            # the txn ID lines, typically right after the ISIN line
            sec_parts = []
            for m in range(k + 1, start):
                part = lines[m].strip()
                # Stop at txn ID markers
                if re.search(r"\b(Txn|TM/CP|SETT|BSECH|ON-CR|EP-DR|"
                               r"PAYOUT-CR|INTDEP-CR|CTBO)\b", part):
                    break
                # Stop at pure numeric / date lines
                if DATE_RE.search(part) or NUM_TOKEN_RE.fullmatch(part):
                    break
                if part:
                    sec_parts.append(part)
            security = " ".join(sec_parts)
            security = re.sub(r"\s+", " ", security)
            security = security.replace("\ufffd", "")
            if "#" in security:
                security = security.split("#")[0].strip()
            return isin, security
        # If we hit another 'date + numbers' line, we went too far
        if DATE_NUMS_RE.match(line):
            return None
    return None


def _parse_pattern_a(line: str, idx: int, lines: list[str],
                      source_file: str) -> EquityTransaction | None:
    """Pattern A: date + numbers on a single line, ISIN walks back."""
    m = DATE_NUMS_RE.match(line)
    if not m:
        return None
    date_str = m.group(1)
    op_bal   = _to_float(m.group(2))
    credit   = _to_float(m.group(3))
    debit    = _to_float(m.group(4))
    cl_bal   = _to_float(m.group(5))
    stamp    = _to_float(m.group(6))

    # Skip rows with no actual transaction (open/close balance only)
    if credit == 0 and debit == 0:
        return None

    isin_info = _walk_back_to_isin(lines, idx)
    if isin_info is None:
        return None
    isin, security = isin_info

    # Convert DD-MM-YYYY to ISO YYYY-MM-DD
    try:
        d, m, y = date_str.split("-")
        iso_date = f"{y}-{m}-{d}"
    except ValueError:
        return None

    return EquityTransaction(
        isin=isin, security=security[:80], date=iso_date,
        op_bal=op_bal, credit=credit, debit=debit, cl_bal=cl_bal,
        stamp_duty=stamp, source_file=source_file,
    )


def _parse_pattern_b(line: str, source_file: str) -> EquityTransaction | None:
    """Pattern B: ISIN + date + numbers all on one line."""
    isin_m = ISIN_EQUITY_RE.search(line)
    date_m = DATE_RE.search(line)
    if not isin_m or not date_m:
        return None
    isin = isin_m.group(1)
    date_str = date_m.group(1)
    after_date = line[date_m.end():]
    # Match 5 numeric tokens, allowing stamp to be '0' (no decimal)
    nums = re.findall(r"-?\d+(?:\.\d+)?|--", after_date)
    if len(nums) < 5:
        return None
    nums = nums[:5]
    op_bal = _to_float(nums[0])
    credit = _to_float(nums[1])
    debit = _to_float(nums[2])
    cl_bal = _to_float(nums[3])
    stamp = _to_float(nums[4])
    if credit == 0 and debit == 0:
        return None
    sec_start = isin_m.end()
    sec_end = date_m.start()
    security = line[sec_start:sec_end].strip()
    security = re.sub(r"\s+", " ", security)
    security = security.replace("\ufffd", "")
    if "#" in security:
        security = security.split("#")[0].strip()
    try:
        d, m, y = date_str.split("-")
        iso_date = f"{y}-{m}-{d}"
    except ValueError:
        return None
    return EquityTransaction(
        isin=isin, security=security[:80], date=iso_date,
        op_bal=op_bal, credit=credit, debit=debit, cl_bal=cl_bal,
        stamp_duty=stamp, source_file=source_file,
    )


def _to_float(s: str) -> float:
    if s == "--" or not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_cas(pdf_path: Path) -> list[EquityTransaction]:
    """Parse a CDSL CAS PDF and return all equity transactions.

    The CAS has a 'STATEMENT OF TRANSACTIONS' section followed by
    data rows in two patterns (see DATE_NUMS_RE comment). We iterate
    line-by-line, attempting both parsers on each line.
    """
    text, period = read_cas_text(pdf_path)
    source = pdf_path.name

    lines = text.split("\n")
    txns: list[EquityTransaction] = []
    in_txn_section = False
    for i, line in enumerate(lines):
        if "TRANSACTION" in line.upper() and "PERIOD" in line.upper():
            in_txn_section = True
            continue
        # End of transactions: next major section or page break
        if in_txn_section and re.search(
            r"\b(MUTUAL\s+FUND\s+FOLIO|MF\s+FOLIO|PORTFOLIO\s+VAL|"
            r"CDSL\s+DEMAT\s+ACCOUNT\s+DETAILS|"
            r"^Page\s+\d+\s+of\s+)\b",
            line, re.IGNORECASE,
        ):
            in_txn_section = False
        if not in_txn_section:
            continue

        # Try Pattern A first (date + numbers on a line, ISIN walks back)
        txn = _parse_pattern_a(line, i, lines, source)
        if txn is None:
            # Try Pattern B (ISIN + date + numbers all on one line)
            txn = _parse_pattern_b(line, source)
        if txn is not None:
            txns.append(txn)

    return txns


def dedupe_transactions(txns: list[EquityTransaction]) -> list[EquityTransaction]:
    """Remove duplicates that appear in multiple CAS files.

    Same (ISIN, date, credit, debit, cl_bal) row appearing in two CAS
    files is the same trade — keep one.
    """
    seen = set()
    out = []
    for t in txns:
        key = (t.isin, t.date, t.credit, t.debit, t.cl_bal)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


# ---------- CLI ----------
def main():
    import argparse
    p = argparse.ArgumentParser(
        description="Parse CDSL equity CAS PDFs and dump transactions as JSON"
    )
    p.add_argument("pdfs", nargs="+", help="CDSL CAS PDF files")
    p.add_argument("--out", help="Write transactions to this JSON file")
    p.add_argument("--summary", action="store_true",
                   help="Print a summary by ISIN instead of all transactions")
    args = p.parse_args()

    all_txns = []
    for pdf in args.pdfs:
        path = Path(pdf)
        if not path.exists():
            print(f"SKIP {pdf}: not found")
            continue
        try:
            txns = parse_cas(path)
            print(f"  {path.name}: {len(txns)} transactions")
            all_txns.extend(txns)
        except Exception as e:
            print(f"ERROR {path.name}: {e}")

    all_txns = dedupe_transactions(all_txns)
    print(f"\nTotal unique transactions: {len(all_txns)}")

    if args.summary:
        # Group by ISIN, show buys and sells
        from collections import defaultdict
        by_isin = defaultdict(lambda: {"buys": [], "sells": []})
        for t in all_txns:
            if t.credit > 0:
                by_isin[t.isin]["buys"].append(t)
            if t.debit > 0:
                by_isin[t.isin]["sells"].append(t)
        for isin, info in sorted(by_isin.items()):
            sec = info["buys"][0].security if info["buys"] else (
                info["sells"][0].security if info["sells"] else "?")
            sec = sec.split("#")[0].strip()[:30]
            total_buy = sum(b.credit for b in info["buys"])
            total_sell = sum(s.debit for s in info["sells"])
            net = total_buy - total_sell
            print(f"  {isin}  {sec:32s}  buy={total_buy:6.0f}  sell={total_sell:6.0f}  net={net:6.0f}")
        return

    if args.out:
        Path(args.out).write_text(json.dumps(
            [asdict(t) for t in all_txns], indent=2
        ))
        print(f"Wrote {args.out}")
    else:
        for t in all_txns:
            if t.credit > 0 or t.debit > 0:  # only show actual trades
                print(f"  {t.date}  {t.isin}  {t.security[:40]:40s}  "
                      f"buy={t.credit:6.0f}  sell={t.debit:6.0f}  "
                      f"cl={t.cl_bal:6.0f}")


if __name__ == "__main__":
    main()