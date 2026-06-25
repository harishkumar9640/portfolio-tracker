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
# Graham and PE-Relative are intentionally kept in the module for
# callers that want them (e.g. tests, CLI), but the web UI now only
# surfaces Two-stage DCF. The DCF method is the primary recommendation
# because it directly values future cash generation, which is what
# ultimately drives share price over the long run. Graham is a useful
# sanity check for stable, profitable companies; PE-Relative is useful
# when a fair industry PE is known. Both are hidden in the UI by
# default but exposed in the /api/fairvalue/lookup response under
# `other_methods` for users who want to see them.

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


def dcf_breakdown(fcf_per_share: float, g1: float = 0.10,
                  g2: float = 0.03, r: float = 0.10,
                  years: int = 5) -> dict:
    """
    Year-by-year DCF calculation, returned as a structured dict that
    the UI can render as a worked math problem. Returns an empty dict
    if inputs are invalid.

    Layout of the response:
      {
        "inputs": {"fcf_per_share": 14.73, "g1": 0.10, "g2": 0.03,
                   "r": 0.10, "years": 5},
        "years": [
          # one row per stage-1 year
          {"year": 1, "fcf": 16.20, "pv": 14.73, "formula": "..."},
          ...
        ],
        "terminal": {
          "fcf_year6": ..., "growth": 0.03, "discount": 0.10,
          "value": ..., "pv": ...,
          "formula": "...",
        },
        "totals": {
          "pv_stage1": ...,    # sum of stage-1 PVs
          "pv_terminal": ...,  # discounted terminal value
          "dcf": ...,          # = pv_stage1 + pv_terminal
          "terminal_pct": ..., # terminal as fraction of DCF (often >70%)
        },
        "step_math": "...",    # one-paragraph LaTeX-free summary
      }
    """
    if not fcf_per_share or fcf_per_share <= 0 or r <= g2:
        return {}

    years_data = []
    pv_stage1 = 0.0
    for t in range(1, years + 1):
        # Projected FCF = current FCF * (1 + g1)^t
        projected = fcf_per_share * (1 + g1) ** t
        # Present value = projected FCF / (1 + r)^t
        pv = projected / ((1 + r) ** t)
        pv_stage1 += pv
        years_data.append({
            "year": t,
            "projected_fcf": round(projected, 4),
            "discount_factor": round(1 / ((1 + r) ** t), 6),
            "present_value": round(pv, 4),
            "formula": (
                f"FCF_{t} = {fcf_per_share:.2f} × (1 + {g1:.2f})^{t} = {projected:.2f};  "
                f"PV = {projected:.2f} / (1 + {r:.2f})^{t} = {pv:.2f}"
            ),
        })

    # Terminal value: year-N+1 FCF / (r - g2), then discount back
    fcf_n_plus_1 = fcf_per_share * (1 + g1) ** years * (1 + g2)
    terminal_value = fcf_n_plus_1 / (r - g2)
    pv_terminal = terminal_value / ((1 + r) ** years)
    dcf = pv_stage1 + pv_terminal
    terminal_pct = (pv_terminal / dcf) if dcf > 0 else 0.0

    step_math = (
        f"Step 1 — Project FCF for years 1 to {years} at growth rate g₁ = {g1*100:.1f}%:  "
        f"FCF_t = {fcf_per_share:.2f} × (1 + {g1:.2f})^t.\n"
        f"Step 2 — Discount each year's projected FCF back to today at rate r = {r*100:.1f}%:  "
        f"PV_t = FCF_t / (1 + r)^t.  Sum = ₹{pv_stage1:.2f}.\n"
        f"Step 3 — Terminal value (year {years+1} onward, growing at g₂ = {g2*100:.1f}% forever):  "
        f"TV = FCF_{years} × (1 + g₂) / (r − g₂) = "
        f"{fcf_per_share * (1 + g1) ** years:.2f} × {1+g2:.2f} / {r-g2:.2f} = ₹{terminal_value:.2f}.\n"
        f"Step 4 — Discount terminal value back to today:  "
        f"PV(TV) = {terminal_value:.2f} / (1 + {r:.2f})^{years} = ₹{pv_terminal:.2f}.\n"
        f"Step 5 — Intrinsic value = PV(stage 1) + PV(terminal) = "
        f"{pv_stage1:.2f} + {pv_terminal:.2f} = ₹{dcf:.2f}.\n"
        f"Note: terminal value contributes {terminal_pct*100:.1f}% of the total. "
        f"Small changes in r or g₂ will significantly affect this number."
    )

    return {
        "inputs": {
            "fcf_per_share": fcf_per_share,
            "g1": g1, "g2": g2, "r": r, "years": years,
        },
        "years": years_data,
        "terminal": {
            "fcf_year_n_plus_1": round(fcf_n_plus_1, 4),
            "growth_rate": g2,
            "discount_rate": r,
            "terminal_value": round(terminal_value, 4),
            "present_value": round(pv_terminal, 4),
            "formula": (
                f"TV = FCF_{years} × (1 + g₂) / (r − g₂) = "
                f"{fcf_per_share * (1 + g1) ** years:.2f} × {1+g2:.2f} / {r-g2:.2f} = ₹{terminal_value:.2f};  "
                f"PV(TV) = TV / (1 + r)^{years} = ₹{pv_terminal:.2f}"
            ),
        },
        "totals": {
            "pv_stage1": round(pv_stage1, 4),
            "pv_terminal": round(pv_terminal, 4),
            "dcf": round(dcf, 4),
            "terminal_pct": round(terminal_pct, 4),
        },
        "step_math": step_math,
    }


def graham_breakdown(eps: float, bvps: float) -> dict:
    """Show the math behind the Graham Number. Returns {} if inputs invalid."""
    if not eps or not bvps or eps <= 0 or bvps <= 0:
        return {}
    value = (22.5 * eps * bvps) ** 0.5
    return {
        "inputs": {"eps": eps, "bvps": bvps, "multiplier": 22.5},
        "value": round(value, 4),
        "formula": f"sqrt(22.5 × {eps:.2f} × {bvps:.2f}) = sqrt({22.5 * eps * bvps:.2f}) = ₹{value:.2f}",
        "step_math": (
            f"Graham Number = sqrt(22.5 × EPS × BVPS).\n"
            f"  EPS = ₹{eps:.2f},  BVPS = ₹{bvps:.2f}.\n"
            f"  = sqrt(22.5 × {eps:.2f} × {bvps:.2f}) = sqrt({22.5 * eps * bvps:.2f}) = ₹{value:.2f}.\n"
            f"Note: This is a conservative floor price for stable, profitable companies "
            f"with low debt. It assumes the company earns at least its book value."
        ),
    }


def pe_relative_breakdown(eps: float, industry_pe: float) -> dict:
    """Show the math behind PE-Relative valuation. Returns {} if inputs invalid."""
    if not eps or eps <= 0 or not industry_pe or industry_pe <= 0:
        return {}
    value = eps * industry_pe
    return {
        "inputs": {"eps": eps, "industry_pe": industry_pe},
        "value": round(value, 4),
        "formula": f"EPS × Industry PE = {eps:.2f} × {industry_pe:.1f} = ₹{value:.2f}",
        "step_math": (
            f"PE-Relative value = EPS × Industry PE.\n"
            f"  EPS = ₹{eps:.2f},  Industry PE = {industry_pe:.1f}.\n"
            f"  = {eps:.2f} × {industry_pe:.1f} = ₹{value:.2f}.\n"
            f"Note: Only meaningful when you know the right industry PE. "
            f"Different sectors trade at different multiples (banks ~12, FMCG ~40, IT ~25)."
        ),
    }


# ---------- Aggregated "check" helper ----------
@dataclass
class ValuationRow:
    ticker: str
    price: Optional[float] = None
    eps: Optional[float] = None
    book_value: Optional[float] = None
    fcf_per_share: Optional[float] = None
    market_cap: Optional[float] = None
    graham: Optional[float] = None
    pe_relative: Optional[float] = None
    dcf: Optional[float] = None
    # Structured breakdowns for the UI (year-by-year DCF, Graham formula, etc.)
    dcf_breakdown: Optional[dict] = None
    other_methods: Optional[dict] = None  # graham + pe_relative breakdowns
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
        market_cap = data.get("market_cap")

        graham = graham_number(eps, bvps) if (eps and bvps) else None
        pe_rel = pe_relative_value(eps, industry_pe) if (eps and industry_pe) else None
        dcf = dcf_value(fcf, dcf_g1, dcf_g2, dcf_r) if fcf else None

        # Compute breakdowns for the UI worked-example. The other_methods
        # dict carries Graham + PE-Relative breakdowns (hidden by default).
        dcf_bd = dcf_breakdown(fcf, dcf_g1, dcf_g2, dcf_r) if fcf else {}
        other = {}
        if eps and bvps:
            other["graham"] = graham_breakdown(eps, bvps)
        if eps and industry_pe:
            other["pe_relative"] = pe_relative_breakdown(eps, industry_pe)

        rows.append(ValuationRow(
            ticker=ticker,
            price=price,
            eps=eps,
            book_value=bvps,
            fcf_per_share=fcf,
            market_cap=market_cap,
            graham=graham,
            pe_relative=pe_rel,
            dcf=dcf,
            dcf_breakdown=dcf_bd if dcf_bd else None,
            other_methods=other if other else None,
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