"""
pipeline.tax_pnl
----------------
Broker-agnostic Tax P&L parser.

Public API:
    parse_files(files: list[Path]) -> NormalizedTaxPnl
    detect_broker(file: Path) -> str | None        # "angel_one" | "zerodha" | "generic"
    build_markdown_report(data: NormalizedTaxPnl, label: str) -> str

Adapters live in pipeline.tax_pnl.adapters.*. Each implements the
BrokerAdapter protocol:

    class BrokerAdapter:
        name: str
        def can_parse(self, file: Path) -> bool: ...
        def parse(self, file: Path) -> list[Trade]: ...

The output NormalizedTaxPnl is what the existing /tax dashboard consumes,
so the rest of webapp.tax_dashboard doesn't need to know which broker
the file came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional


@dataclass
class Trade:
    """One closed equity trade, broker-agnostic."""
    scrip: str
    isin: Optional[str]
    quantity: float
    buy_date: Optional[date]
    buy_value: float          # total buy value (qty * buy_price) including charges
    sell_date: Optional[date]
    sell_value: float         # total sell proceeds (qty * sell_price)
    pnl: float                # realised P&L (sell - buy - charges allocated)
    charges: float = 0.0      # STT + stamp duty + brokerage allocated to this trade
    stt: float = 0.0
    fy: str = ""              # Indian financial year, e.g. "2024-25"
    source_broker: str = ""   # "angel_one" | "zerodha" | "generic"


@dataclass
class OpenHolding:
    """An open position carried into a new FY."""
    scrip: str
    isin: Optional[str]
    quantity: float
    buy_value: float          # cost
    current_value: float      # market value at the time of the export
    unrealised: float         # current - cost
    st_unrealised: float = 0.0
    lt_unrealised: float = 0.0
    fy: str = ""


@dataclass
class FnoSummary:
    """F&O summary (per FY)."""
    options_turnover: float = 0.0
    options_pnl: float = 0.0
    futures_turnover: float = 0.0
    futures_pnl: float = 0.0
    stt: float = 0.0
    charges: float = 0.0
    brokerage: float = 0.0


@dataclass
class FyTotals:
    """Per-FY rollup, matches the existing tax_dashboard.fy dict shape."""
    fy: str
    equity_buy_value: float = 0.0
    equity_sell_value: float = 0.0
    equity_pnl: float = 0.0
    equity_stcg: float = 0.0
    equity_ltcg: float = 0.0
    equity_stamp_duty: float = 0.0
    equity_stt: float = 0.0
    equity_brokerage: float = 0.0
    equity_other_charges: float = 0.0
    equity_intraday_pnl: float = 0.0
    fno: FnoSummary = field(default_factory=FnoSummary)
    dividend_income: float = 0.0
    open_holdings_cost: float = 0.0
    open_holdings_market_value: float = 0.0
    open_holdings_unrealised: float = 0.0
    open_holdings_st_unrealised: float = 0.0
    open_holdings_lt_unrealised: float = 0.0


@dataclass
class NormalizedTaxPnl:
    """Output of the broker-agnostic parser. The /tax dashboard consumes
    this directly — no further broker-specific knowledge required."""
    label: str                         # user-provided or auto ("Uploaded 2026-07-10 18:32")
    source_files: list[str]           # filenames actually parsed
    detected_brokers: list[str]       # e.g. ["angel_one", "zerodha"]
    fys: list[FyTotals] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    open_holdings: list[OpenHolding] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)

    # --- Convenience accessors that mirror the existing tax_dashboard dict ---
    def totals(self) -> dict:
        """Return the same shape as extract_tax_data()['totals']."""
        t = FyTotals(fy="ALL")
        for fy in self.fys:
            t.equity_buy_value += fy.equity_buy_value
            t.equity_sell_value += fy.equity_sell_value
            t.equity_pnl += fy.equity_pnl
            t.equity_stcg += fy.equity_stcg
            t.equity_ltcg += fy.equity_ltcg
            t.equity_stamp_duty += fy.equity_stamp_duty
            t.equity_stt += fy.equity_stt
            t.equity_brokerage += fy.equity_brokerage
            t.equity_other_charges += fy.equity_other_charges
            t.equity_intraday_pnl += fy.equity_intraday_pnl
            t.dividend_income += fy.dividend_income
            t.open_holdings_cost += fy.open_holdings_cost
            t.open_holdings_market_value += fy.open_holdings_market_value
            t.open_holdings_unrealised += fy.open_holdings_unrealised
            t.open_holdings_st_unrealised += fy.open_holdings_st_unrealised
            t.open_holdings_lt_unrealised += fy.open_holdings_lt_unrealised
        # F&O
        t.fno = FnoSummary(
            options_turnover=sum(f.fno.options_turnover for f in self.fys),
            options_pnl=sum(f.fno.options_pnl for f in self.fys),
            futures_turnover=sum(f.fno.futures_turnover for f in self.fys),
            futures_pnl=sum(f.fno.futures_pnl for f in self.fys),
            stt=sum(f.fno.stt for f in self.fys),
            charges=sum(f.fno.charges for f in self.fys),
            brokerage=sum(f.fno.brokerage for f in self.fys),
        )
        return {
            "equity_buy_value": t.equity_buy_value,
            "equity_sell_value": t.equity_sell_value,
            "equity_pnl": t.equity_pnl,
            "equity_stcg": t.equity_stcg,
            "equity_ltcg": t.equity_ltcg,
            "equity_stamp_duty": t.equity_stamp_duty,
            "equity_stt": t.equity_stt,
            "equity_brokerage": t.equity_brokerage,
            "equity_other_charges": t.equity_other_charges,
            "equity_intraday_pnl": t.equity_intraday_pnl,
            "fno_options_turnover": t.fno.options_turnover,
            "fno_options_pnl": t.fno.options_pnl,
            "fno_futures_turnover": t.fno.futures_turnover,
            "fno_futures_pnl": t.fno.futures_pnl,
            "fno_stt": t.fno.stt,
            "fno_charges": t.fno.charges,
            "fno_brokerage": t.fno.brokerage,
            "dividend_income": t.dividend_income,
            "open_holdings_cost": t.open_holdings_cost,
            "open_holdings_market_value": t.open_holdings_market_value,
            "open_holdings_unrealised": t.open_holdings_unrealised,
            "open_holdings_st_unrealised": t.open_holdings_st_unrealised,
            "open_holdings_lt_unrealised": t.open_holdings_lt_unrealised,
        }

    def by_fy(self) -> list[dict]:
        """Return per-FY dicts in the same shape the existing template expects."""
        out = []
        for fy in self.fys:
            out.append({
                "fy": fy.fy,
                "equity_buy_value": fy.equity_buy_value,
                "equity_sell_value": fy.equity_sell_value,
                "equity_pnl": fy.equity_pnl,
                "equity_stcg": fy.equity_stcg,
                "equity_ltcg": fy.equity_ltcg,
                "equity_stamp_duty": fy.equity_stamp_duty,
                "equity_stt": fy.equity_stt,
                "equity_brokerage": fy.equity_brokerage,
                "equity_other_charges": fy.equity_other_charges,
                "equity_intraday_pnl": fy.equity_intraday_pnl,
                "fno_options_pnl": fy.fno.options_pnl,
                "fno_options_turnover": fy.fno.options_turnover,
                "fno_futures_pnl": fy.fno.futures_pnl,
                "fno_futures_turnover": fy.fno.futures_turnover,
                "fno_charges": fy.fno.charges,
                "fno_stt": fy.fno.stt,
                "fno_brokerage": fy.fno.brokerage,
                "dividend_income": fy.dividend_income,
                "open_holdings_cost": fy.open_holdings_cost,
                "open_holdings_market_value": fy.open_holdings_market_value,
                "open_holdings_unrealised": fy.open_holdings_unrealised,
                "open_holdings_st_unrealised": fy.open_holdings_st_unrealised,
                "open_holdings_lt_unrealised": fy.open_holdings_lt_unrealised,
            })
        return out


def parse_date(v) -> Optional[date]:
    """Best-effort date parser. Accepts datetime, date, ISO string, dd/mm/yyyy, dd-mm-yyyy."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y",
                "%d/%m/%y", "%d-%m-%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def detect_fy_from_filename(name: str) -> str:
    """Pull '2024-25' out of 'Tax PNL 2024-25.xlsx' or 'PnL_2023_24.xlsx'.
    If no FY pattern is found, return the current Indian financial year.
    """
    import re
    m = re.search(r"(20\d{2})[-_/](\d{2,4})", name)
    if m:
        start = int(m.group(1))
        end_token = m.group(2)
        # Could be "25" or "2025"
        end = start + 1 if len(end_token) == 2 else int(end_token)
        if end == start + 1:
            return f"{start}-{str(end)[-2:]}"
        # Ambiguous: just return the first year
        return f"{start}-{str(start + 1)[-2:]}"
    m2 = re.search(r"(20\d{2})", name)
    if m2:
        y = int(m2.group(1))
        return f"{y}-{str(y + 1)[-2:]}"
    # No year in filename — fall back to the current Indian FY
    return current_indian_fy()


def current_indian_fy() -> str:
    """Return the current Indian financial year as '2024-25'.
    Indian FY runs April 1 to March 31. If month >= 4, FY = YYYY-(Y+1).
    """
    from datetime import date
    today = date.today()
    if today.month >= 4:
        start = today.year
    else:
        start = today.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("₹", "").replace(" ", "")
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        try:
            return float(s)
        except ValueError:
            return 0.0
    return 0.0


def parse_files(files: list[Path], label: str = "Uploaded") -> NormalizedTaxPnl:
    """Top-level entry: parse a list of files, route to the right adapter,
    stitch the results together into a single NormalizedTaxPnl."""
    from pipeline.tax_pnl.adapters import get_adapter

    out = NormalizedTaxPnl(label=label, source_files=[], detected_brokers=[])

    fy_aggregator: dict[str, FyTotals] = {}

    def get_or_make_fy(fy_label: str) -> FyTotals:
        if fy_label not in fy_aggregator:
            fy_aggregator[fy_label] = FyTotals(fy=fy_label)
        return fy_aggregator[fy_label]

    for path in files:
        if not path.exists():
            out.parse_warnings.append(f"file not found: {path}")
            continue

        adapter = get_adapter(path)
        if adapter is None:
            out.parse_warnings.append(
                f"could not detect broker for {path.name}; "
                "supported: Angel One 'Tax PNL' xlsx, Zerodha Console P&L CSV"
            )
            continue

        out.source_files.append(path.name)
        if adapter.name not in out.detected_brokers:
            out.detected_brokers.append(adapter.name)

        try:
            parsed = adapter.parse(path)
        except Exception as e:
            out.parse_warnings.append(f"{path.name}: parse failed: {e}")
            continue

        # parsed is a dict with keys: fy_summaries, trades, open_holdings
        for fy_label, fy_summary in parsed.get("fy_summaries", {}).items():
            fy = get_or_make_fy(fy_label)
            for k, v in fy_summary.items():
                if k == "fno" and isinstance(v, dict):
                    fy.fno.options_turnover += v.get("options_turnover", 0.0)
                    fy.fno.options_pnl += v.get("options_pnl", 0.0)
                    fy.fno.futures_turnover += v.get("futures_turnover", 0.0)
                    fy.fno.futures_pnl += v.get("futures_pnl", 0.0)
                    fy.fno.stt += v.get("stt", 0.0)
                    fy.fno.charges += v.get("charges", 0.0)
                    fy.fno.brokerage += v.get("brokerage", 0.0)
                elif hasattr(fy, k):
                    setattr(fy, k, getattr(fy, k) + v)

        out.trades.extend(parsed.get("trades", []))
        out.open_holdings.extend(parsed.get("open_holdings", []))

    # Sort FYs oldest first for stable display
    out.fys = [fy_aggregator[k] for k in sorted(fy_aggregator.keys())]
    return out


def parse_files_with_mapping(files: list[Path],
                              column_mapping: dict,
                              label: str = "Uploaded") -> NormalizedTaxPnl:
    """Parse files using a user-supplied column mapping. Bypasses broker
    auto-detect and uses the Generic adapter for every file.

    Multiple files are stitched together. `label` is the human-readable
    name for the session, surfaced in the report.
    """
    from pipeline.tax_pnl.adapters.generic import GenericAdapter

    out = NormalizedTaxPnl(label=label, source_files=[], detected_brokers=["generic"])
    fy_aggregator: dict[str, FyTotals] = {}

    def get_or_make_fy(fy_label: str) -> FyTotals:
        if fy_label not in fy_aggregator:
            fy_aggregator[fy_label] = FyTotals(fy=fy_label)
        return fy_aggregator[fy_label]

    for path in files:
        if not path.exists():
            out.parse_warnings.append(f"file not found: {path}")
            continue
        adapter = GenericAdapter(column_mapping)
        out.source_files.append(path.name)
        try:
            parsed = adapter.parse(path)
        except Exception as e:
            out.parse_warnings.append(f"{path.name}: parse failed: {e}")
            continue
        for fy_label, fy_summary in parsed.get("fy_summaries", {}).items():
            fy = get_or_make_fy(fy_label)
            for k, v in fy_summary.items():
                if k == "fno" and isinstance(v, dict):
                    fy.fno.options_turnover += v.get("options_turnover", 0.0)
                    fy.fno.options_pnl += v.get("options_pnl", 0.0)
                    fy.fno.futures_turnover += v.get("futures_turnover", 0.0)
                    fy.fno.futures_pnl += v.get("futures_pnl", 0.0)
                    fy.fno.stt += v.get("stt", 0.0)
                    fy.fno.charges += v.get("charges", 0.0)
                    fy.fno.brokerage += v.get("brokerage", 0.0)
                elif hasattr(fy, k):
                    setattr(fy, k, getattr(fy, k) + v)
        out.trades.extend(parsed.get("trades", []))
        out.open_holdings.extend(parsed.get("open_holdings", []))

    out.fys = [fy_aggregator[k] for k in sorted(fy_aggregator.keys())]
    return out
