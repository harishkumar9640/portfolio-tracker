"""
fair_value
----------
Fair-value checker for Indian stocks, refactored from FairValueCheck.

Public surface:
  - fair_value.fetcher.fetch(ticker) -> dict with current_price, eps,
    book_value, market_cap, operating_cash_flow_per_share
  - fair_value.valuation.graham_number(eps, bvps)
  - fair_value.valuation.pe_relative_value(eps, industry_pe)
  - fair_value.valuation.dcf_value(fcf_per_share, g1, g2, r)
  - fair_value.check(tickers, ...) -> list of ValuationRow

Caching: pages from screener.in are cached for 1 hour under
``<project>/.cache/screener/`` (separate from the SQLite history DB).
"""
from __future__ import annotations

from .fetcher import fetch
from .valuation import (
    graham_number,
    pe_relative_value,
    dcf_value,
    check,
)

__all__ = [
    "fetch",
    "graham_number",
    "pe_relative_value",
    "dcf_value",
    "check",
]