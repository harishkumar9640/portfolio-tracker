"""Zerodha Console P&L CSV adapter.

Zerodha's Console "Tax P&L" export (https://console.zerodha.com/reports/tax-pnl)
is a flat CSV with one row per closed trade. Columns (header line, fuzzy-matched):

    Symbol, ISIN, Buy Date, Buy Price, Sell Date, Sell Price,
    Quantity, Realised P&L, Holding Period (days), Type

Where Type is one of:
    "Delivery" | "Intraday" | "Futures" | "Options"

The export is per-FY (you download one file per financial year).

This adapter is a best-effort implementation. Since the user does not have
a sample Zerodha file, the column names are documented from public Zerodha
docs and the parser is tolerant of extra / missing columns.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from pipeline.tax_pnl import (
    FyTotals, OpenHolding, Trade, _num, detect_fy_from_filename, parse_date,
)


# Canonical column names -> the set of fuzzy variations we've seen
_COL_VARIANTS = {
    "symbol":   {"symbol", "scrip", "stock", "trading symbol", "instrument"},
    "isin":     {"isin", "isin code"},
    "buy_date": {"buy date", "purchase date", "buydate", "buy_dt", "buy date"},
    "buy_price": {"buy price", "purchase price", "buy rate", "buy_price"},
    "sell_date": {"sell date", "sale date", "selldate", "sell_dt", "sell date"},
    "sell_price": {"sell price", "sale price", "sell rate", "sell_price"},
    "quantity": {"quantity", "qty", "shares", "units"},
    "pnl":      {"realised p&l", "realized p&l", "realised pnl", "realized pnl",
                 "p&l", "pnl", "profit", "profit/loss"},
    "charges":  {"charges", "total charges", "fees", "transaction charges"},
    "type":     {"type", "segment", "category", "trade type", "instrument type"},
}


def _norm_header(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _resolve_columns(headers: list[str]) -> dict[str, int]:
    """Map canonical column names to their 0-based index in `headers`."""
    norm = [_norm_header(h) for h in headers]
    out = {}
    for canon, variants in _COL_VARIANTS.items():
        for v in variants:
            if v in norm:
                out[canon] = norm.index(v)
                break
    return out


class ZerodhaAdapter:
    name = "zerodha"

    def can_parse(self, file: Path) -> bool:
        """Zerodha Console exports are CSV. Heuristic: first non-empty
        cell of the file is a header row containing 'Symbol' and
        ('Realised P&L' or 'Realized P&L')."""
        if file.suffix.lower() != ".csv":
            return False
        try:
            with open(file, "r", encoding="utf-8-sig", errors="replace") as f:
                # Read up to 5 lines to find the header
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        return False
                    line_norm = _norm_header(line)
                    if ("symbol" in line_norm
                            and ("realised p&l" in line_norm
                                 or "realized p&l" in line_norm
                                 or "pnl" in line_norm)):
                        return True
            return False
        except Exception:
            return False

    def parse(self, file: Path) -> dict:
        fy_label = detect_fy_from_filename(file.name)
        result = {
            "fy_summaries": {fy_label: _empty_fy_dict()},
            "trades": [],
            "open_holdings": [],
        }
        fy = result["fy_summaries"][fy_label]

        with open(file, "r", encoding="utf-8-sig", errors="replace") as f:
            # Find the header line
            reader = None
            for _ in range(5):
                pos = f.tell()
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                headers = next(csv.reader([stripped]))
                cols = _resolve_columns(headers)
                if "symbol" in cols and "quantity" in cols:
                    f.seek(pos + len(line))
                    reader = csv.DictReader(f, fieldnames=headers)
                    break
            if reader is None:
                return result

            for row in reader:
                if not row or not any(row.values()):
                    continue
                cols = _resolve_columns(list(row.keys()))
                if "symbol" not in cols or "quantity" not in cols:
                    continue

                def cell(col: str) -> str:
                    idx = cols.get(col)
                    if idx is None or idx >= len(row):
                        return ""
                    v = list(row.values())[idx] if isinstance(row, dict) else row[idx]
                    return (v or "").strip()

                scrip = cell("symbol")
                if not scrip:
                    continue
                qty = _num(cell("quantity"))
                if qty <= 0:
                    continue
                buy_price = _num(cell("buy_price"))
                sell_price = _num(cell("sell_price"))
                buy_val = buy_price * qty
                sell_val = sell_price * qty
                pnl = _num(cell("pnl")) if "pnl" in cols else (sell_val - buy_val)
                charges = _num(cell("charges")) if "charges" in cols else 0.0
                trade_type = (cell("type") or "").lower()
                is_intraday = "intra" in trade_type or "speculat" in trade_type
                is_fno = "future" in trade_type or "option" in trade_type

                if is_fno:
                    # Tally into F&O summary (we don't have separate turnover figures)
                    fy["fno"]["options_turnover" if "option" in trade_type else "futures_turnover"] += max(buy_val, sell_val)
                    fy["fno"]["options_pnl" if "option" in trade_type else "futures_pnl"] += pnl
                    continue

                fy["equity_buy_value"] += buy_val
                fy["equity_sell_value"] += sell_val
                if is_intraday:
                    # Intraday P&L is speculative and tracked separately; do
                    # NOT add it to equity_pnl (which is the delivery total).
                    fy["equity_intraday_pnl"] += pnl
                else:
                    fy["equity_pnl"] += pnl
                    # Heuristic: holding period > 365 days = LTCG, else STCG
                    buy_d = parse_date(cell("buy_date"))
                    sell_d = parse_date(cell("sell_date"))
                    if buy_d and sell_d and (sell_d - buy_d).days > 365:
                        fy["equity_ltcg"] += pnl
                    else:
                        fy["equity_stcg"] += pnl

                result["trades"].append(Trade(
                    scrip=scrip,
                    isin=cell("isin") or None,
                    quantity=qty,
                    buy_date=parse_date(cell("buy_date")),
                    buy_value=buy_val,
                    sell_date=parse_date(cell("sell_date")),
                    sell_value=sell_val,
                    pnl=pnl,
                    charges=charges,
                    fy=fy_label,
                    source_broker=self.name,
                ))

        return result


def _empty_fy_dict() -> dict:
    return {
        "equity_buy_value": 0.0,
        "equity_sell_value": 0.0,
        "equity_pnl": 0.0,
        "equity_stcg": 0.0,
        "equity_ltcg": 0.0,
        "equity_stamp_duty": 0.0,
        "equity_stt": 0.0,
        "equity_brokerage": 0.0,
        "equity_other_charges": 0.0,
        "equity_intraday_pnl": 0.0,
        "fno": {
            "options_turnover": 0.0, "options_pnl": 0.0,
            "futures_turnover": 0.0, "futures_pnl": 0.0,
            "stt": 0.0, "charges": 0.0, "brokerage": 0.0,
        },
        "dividend_income": 0.0,
        "open_holdings_cost": 0.0,
        "open_holdings_market_value": 0.0,
        "open_holdings_unrealised": 0.0,
        "open_holdings_st_unrealised": 0.0,
        "open_holdings_lt_unrealised": 0.0,
    }
