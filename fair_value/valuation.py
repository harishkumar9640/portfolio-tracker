"""
fair_value.valuation
--------------------
Three classic valuation models, all in pure Python (no numpy).

  - Graham Number:  sqrt(22.5 * EPS * BVPS)
  - PE-relative:    EPS * industry_pe
  - Two-stage DCF:  PV of FCF for years 1-5 + discounted terminal value
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

from .fetcher import fetch
from logging_setup import get_logger

log = get_logger("fair_value")

PROJECT = Path(__file__).resolve().parent.parent
TICKERS_FILE = PROJECT / "my_tickers.txt"


# ---------- Pure model functions ----------
def graham_number(eps: float, bvps: float) -> float:
    """Graham Number = sqrt(22.5 * EPS * BVPS). Returns 0 if inputs invalid."""
    if not eps or not bvps or eps <= 0 or bvps <= 0:
        return 0.0
    return (22.5 * eps * bvps) ** 0.5


def pe_relative_value(eps: float, industry_pe: float) -> float:
    """Intrinsic value based on PE relative to industry: EPS * industry_pe."""
    if not eps or not industry_pe or eps <= 0 or industry_pe <= 0:
        return 0.0
    return eps * industry_pe


def dcf_value(fcf_per_share: float, g1: float = 0.10,
              g2: float = 0.03, r: float = 0.10) -> float:
    """
    Two-stage DCF on per-share free cash flow.
    Stage 1: 5 years of growth at g1.
    Terminal: perpetual growth at g2.
    Returns intrinsic value per share (or 0 if inputs invalid).
    """
    if not fcf_per_share or fcf_per_share <= 0:
        return 0.0
    if r <= g2:
        return 0.0
    pv_stage1 = 0.0
    for t in range(1, 6):
        pv_stage1 += fcf_per_share * (1 + g1) ** t / ((1 + r) ** t)
    terminal = fcf_per_share * (1 + g1) ** 5 * (1 + g2) / (r - g2)
    pv_terminal = terminal / ((1 + r) ** 5)
    return pv_stage1 + pv_terminal


# ---------- Aggregated "check" helper ----------
@dataclass
class ValuationRow:
    ticker: str
    price: Optional[float] = None
    eps: Optional[float] = None
    book_value: Optional[float] = None
    fcf_per_share: Optional[float] = None
    graham: Optional[float] = None
    pe_relative: Optional[float] = None
    dcf: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _load_tickers_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.upper())
    return out


def load_tickers(path: Optional[Path] = None) -> list[str]:
    """Load tickers from a file (one per line, '#' = comment)."""
    return _load_tickers_file(path or TICKERS_FILE)


def check(
    tickers: Iterable[str],
    *,
    industry_pe: Optional[float] = None,
    dcf_g1: float = 0.10,
    dcf_g2: float = 0.03,
    dcf_r: float = 0.10,
) -> list[ValuationRow]:
    """
    Run the three valuation models against a list of tickers.
    Returns one ValuationRow per ticker, in the input order.
    """
    rows: list[ValuationRow] = []
    for ticker in tickers:
        ticker = ticker.upper().strip()
        data = fetch(ticker)
        if data.get("error"):
            rows.append(ValuationRow(ticker=ticker, error=data["error"]))
            continue

        price = data.get("current_price")
        eps = data.get("eps")
        bvps = data.get("book_value")
        fcf = data.get("operating_cash_flow_per_share")

        graham = graham_number(eps, bvps) if (eps and bvps) else None
        pe_rel = pe_relative_value(eps, industry_pe) if (eps and industry_pe) else None
        dcf = dcf_value(fcf, dcf_g1, dcf_g2, dcf_r) if fcf else None

        rows.append(ValuationRow(
            ticker=ticker,
            price=price,
            eps=eps,
            book_value=bvps,
            fcf_per_share=fcf,
            graham=graham,
            pe_relative=pe_rel,
            dcf=dcf,
        ))
    return rows


# ---------- CLI ----------
def main() -> None:
    """CLI entry point: `python3 fairvalue.py [ticker ...]` or reads my_tickers.txt."""
    import argparse
    import csv
    import sys

    parser = argparse.ArgumentParser(description="Check fair value of Indian stocks.")
    parser.add_argument("tickers", nargs="*", help="Ticker symbols (e.g. RELIANCE TCS).")
    parser.add_argument("--input-file", default=str(TICKERS_FILE))
    parser.add_argument("--output-file", help="Write results to this CSV file.")
    parser.add_argument("--industry-pe", type=float, help="Industry PE for PE-relative model.")
    parser.add_argument("--dcf-g1", type=float, default=0.10, help="DCF stage-1 growth.")
    parser.add_argument("--dcf-g2", type=float, default=0.03, help="DCF terminal growth.")
    parser.add_argument("--dcf-r",  type=float, default=0.10, help="DCF discount rate.")
    args = parser.parse_args()

    tickers = args.tickers if args.tickers else _load_tickers_file(Path(args.input_file))
    if not tickers:
        print("No tickers provided.", file=sys.stderr)
        sys.exit(1)

    rows = check(
        tickers,
        industry_pe=args.industry_pe,
        dcf_g1=args.dcf_g1,
        dcf_g2=args.dcf_g2,
        dcf_r=args.dcf_r,
    )

    if args.output_file:
        with open(args.output_file, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].to_dict().keys()))
            w.writeheader()
            for r in rows:
                w.writerow(r.to_dict())
        print(f"Wrote {len(rows)} rows to {args.output_file}")
    else:
        print(f"{'Ticker':<10} {'Price':>10} {'Graham':>10} {'PE-Rel':>10} {'DCF':>10}")
        print("-" * 55)
        for r in rows:
            def fmt(v):
                return f"{v:>10.2f}" if v is not None else f"{'N/A':>10}"
            print(f"{r.ticker:<10} {fmt(r.price)} {fmt(r.graham)} {fmt(r.pe_relative)} {fmt(r.dcf)}")


if __name__ == "__main__":
    main()