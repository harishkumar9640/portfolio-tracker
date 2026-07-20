"""Template-based markdown report builder for uploaded Tax P&L files.

This is intentionally simple: no LLM, no clever phrasing. Just numbers,
a verdict distribution, and 3-5 plain-English insights generated from
the parsed data. Every sentence is produced by a deterministic function
of the data, so the report is fully testable.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from pipeline.tax_pnl import NormalizedTaxPnl, Trade


def build_markdown_report(data: NormalizedTaxPnl, label: str | None = None) -> str:
    """Build a markdown report summarising the uploaded Tax P&L.

    The output has four sections:
      1. Headline (one paragraph: total P&L, CAGR, FYs covered)
      2. Year-by-year breakdown
      3. Verdict distribution
      4. Insights (3-5 plain-English observations)
    """
    label = label or data.label
    totals = data.totals()
    fys = data.by_fy()
    trades = data.trades

    lines: list[str] = []
    lines.append(f"# Tax P&L Report — {label}")
    lines.append("")
    lines.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
    lines.append(f"_Source files: {', '.join(data.source_files) or 'none'}_")
    lines.append(f"_Detected brokers: {', '.join(data.detected_brokers) or 'none'}_")
    lines.append("")

    # ---- Headline ----
    net_pnl = (totals["equity_pnl"]
               + totals["fno_options_pnl"] + totals["fno_futures_pnl"]
               + totals["equity_intraday_pnl"]
               + totals["open_holdings_unrealised"]
               + totals["dividend_income"])
    money_in = totals["equity_sell_value"] + totals["dividend_income"]
    money_out = (totals["equity_buy_value"]
                 + totals["fno_options_turnover"] + totals["fno_futures_turnover"]
                 + totals["equity_stt"] + totals["equity_stamp_duty"]
                 + totals["equity_other_charges"] + totals["fno_stt"]
                 + totals["fno_charges"] + totals["fno_brokerage"])
    fy_count = len(fys)
    fy_label = f"{fy_count} financial year{'s' if fy_count != 1 else ''}" if fy_count else "this period"
    cagr = _cagr(money_in, money_out, fys)
    sign = "📈 Profit" if net_pnl >= 0 else "📉 Loss"
    lines.append(f"## {sign}: ₹{abs(net_pnl):,.0f} over {fy_label}")
    lines.append("")
    if cagr is not None:
        lines.append(f"**Approximate CAGR on invested capital: {cagr:+.2f}% per year**")
        lines.append("")
    lines.append(f"- Total money out: ₹{money_out:,.0f}")
    lines.append(f"- Total money back: ₹{money_in:,.0f}")
    lines.append(f"- Net realised P&L: ₹{totals['equity_pnl'] + totals['fno_options_pnl'] + totals['fno_futures_pnl']:,.0f}")
    lines.append(f"- Unrealised P&L (open): ₹{totals['open_holdings_unrealised']:,.0f}")
    lines.append(f"- Dividend income: ₹{totals['dividend_income']:,.0f}")
    lines.append("")

    # ---- FY breakdown ----
    if fys:
        lines.append("## Year-by-year")
        lines.append("")
        lines.append("| FY | Bought | Sold | Realised P&L | Dividends | Unrealised |")
        lines.append("|---|---|---|---|---|---|")
        for fy in fys:
            lines.append(
                f"| {fy['fy']} "
                f"| ₹{fy['equity_buy_value']:,.0f} "
                f"| ₹{fy['equity_sell_value']:,.0f} "
                f"| ₹{fy['equity_pnl']:,.0f} "
                f"| ₹{fy['dividend_income']:,.0f} "
                f"| ₹{fy['open_holdings_unrealised']:,.0f} |"
            )
        lines.append("")

    # ---- Verdicts ----
    if trades:
        verdicts = _verdict_distribution(trades)
        lines.append("## Trade verdicts")
        lines.append("")
        lines.append("Per-trade grading based on realised P&L %:")
        lines.append("")
        for v, count, pct in verdicts:
            bar = "█" * int(pct / 2)
            lines.append(f"- **{v}** ({count} trades, {pct:.0f}%) {bar}")
        lines.append("")

    # ---- Insights ----
    insights = _build_insights(data, totals, trades)
    if insights:
        lines.append("## Insights")
        lines.append("")
        for ins in insights:
            lines.append(f"- {ins}")
        lines.append("")

    # ---- Warnings ----
    if data.parse_warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in data.parse_warnings:
            lines.append(f"- ⚠️ {w}")
        lines.append("")

    return "\n".join(lines)


# ---------- helpers ----------

def _verdict_for(pnl_pct: float) -> str:
    if pnl_pct > 20:   return "GREAT SELL"
    if pnl_pct > 5:    return "GOOD SELL"
    if pnl_pct > 0:    return "OK SELL"
    if pnl_pct > -5:   return "BAD TIMING"
    if pnl_pct > -20:  return "POOR SELL"
    return "TERRIBLE SELL"


def _verdict_distribution(trades: Iterable[Trade]) -> list[tuple[str, int, float]]:
    counts: dict[str, int] = {}
    for t in trades:
        sell_val = t.sell_value or 0
        pnl_pct = (t.pnl / sell_val * 100) if sell_val else 0
        v = _verdict_for(pnl_pct)
        counts[v] = counts.get(v, 0) + 1
    total = sum(counts.values()) or 1
    order = ["GREAT SELL", "GOOD SELL", "OK SELL", "BAD TIMING", "POOR SELL", "TERRIBLE SELL"]
    return [(v, counts.get(v, 0), counts.get(v, 0) / total * 100) for v in order]


def _cagr(money_in: float, money_out: float, fys: list[dict]) -> float | None:
    """Approximate CAGR = (money_in / money_out)^(1/years) - 1.
    Only meaningful if money_out > 0. Returns None if no FY info or zero capital."""
    if not fys or money_out <= 0 or money_in <= 0:
        return None
    # Years span = difference between earliest and latest FY start
    years = max(1, len(fys))
    ratio = money_in / money_out
    if ratio <= 0:
        return None
    return (ratio ** (1.0 / years) - 1.0) * 100


def _build_insights(data: NormalizedTaxPnl, totals: dict, trades: list[Trade]) -> list[str]:
    """Return 3-5 plain-English insights. Each insight is a deterministic
    function of the data — no LLM, no improvisation."""
    out: list[str] = []

    if trades:
        # Insight 1: top winners and losers
        sorted_trades = sorted(trades, key=lambda t: t.pnl, reverse=True)
        winners = [t for t in sorted_trades if t.pnl > 0][:3]
        losers = [t for t in sorted_trades if t.pnl < 0][-3:][::-1]
        if winners:
            w = ", ".join(f"{t.scrip} (+₹{t.pnl:,.0f})" for t in winners)
            out.append(f"**Top winners:** {w}.")
        if losers:
            l = ", ".join(f"{t.scrip} (-₹{abs(t.pnl):,.0f})" for t in losers)
            out.append(f"**Top losers:** {l}.")

        # Insight 2: holding bias
        intraday_pnl = totals["equity_intraday_pnl"]
        delivery_pnl = totals["equity_pnl"] - intraday_pnl
        if abs(intraday_pnl) > 1 and abs(delivery_pnl) > 1:
            dominant = "delivery (buy-and-hold)" if abs(delivery_pnl) > abs(intraday_pnl) else "intraday"
            out.append(
                f"P&L is dominated by **{dominant} trades** "
                f"(delivery: ₹{delivery_pnl:,.0f} vs intraday: ₹{intraday_pnl:,.0f})."
            )

    # Insight 3: charges drag
    total_charges = (totals["equity_stt"] + totals["equity_stamp_duty"]
                     + totals["equity_other_charges"] + totals["fno_stt"]
                     + totals["fno_charges"] + totals["fno_brokerage"])
    options_turnover = totals["fno_options_turnover"]
    if options_turnover > 0 and total_charges > 0:
        drag_pct = total_charges / options_turnover * 100
        if drag_pct > 0.5:
            out.append(
                f"Transaction costs (STT + brokerage + GST) total ₹{total_charges:,.0f}, "
                f"which is {drag_pct:.1f}% of options turnover — "
                f"{'high' if drag_pct > 2 else 'moderate'} drag."
            )

    # Insight 4: dividend vs trading income
    if totals["dividend_income"] > 0 and totals["equity_buy_value"] > 0:
        div_yield = totals["dividend_income"] / totals["equity_buy_value"] * 100
        out.append(
            f"Dividend income ₹{totals['dividend_income']:,.0f} "
            f"({div_yield:.2f}% yield on capital deployed)."
        )

    # Insight 5: open position health
    if totals["open_holdings_cost"] > 0 and totals["open_holdings_unrealised"] != 0:
        upct = totals["open_holdings_unrealised"] / totals["open_holdings_cost"] * 100
        sign = "up" if upct >= 0 else "down"
        out.append(
            f"Open holdings are {sign} {abs(upct):.1f}% from cost "
            f"(₹{totals['open_holdings_unrealised']:,.0f} on ₹{totals['open_holdings_cost']:,.0f} invested)."
        )

    # Insight 6: FY coverage warning
    if data.parse_warnings:
        out.append(
            f"{len(data.parse_warnings)} file(s) could not be fully parsed — see warnings below."
        )

    return out[:6]
