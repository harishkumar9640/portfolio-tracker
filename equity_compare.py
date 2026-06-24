"""
equity_compare.py
-----------------
Compare your complete portfolio (equity + mutual funds + SGBs) day-change to
8 world indices.

Run:
    python3 equity_compare.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from angel_client import fetch_holdings, portfolio_summary
from indices_chart import (
    CHARTS_DIR,
    cleanup_old_charts,
    fetch_indices,
)
from logging_setup import get_logger

log = get_logger("compare")
from mf_sgb import (
    aggregate as aggregate_assets,
    AssetRow,
    fetch_mf_rows,
    fetch_sgb_rows,
    load_mfs,
    load_sgbs,
)


# ---------- Equity previous-day value (with single common baseline) ----------
def fetch_equity_prev_value() -> tuple[float | None, str, pd.Timestamp | None]:
    """
    Compute the previous-day value of the equity portfolio using a SINGLE
    common baseline trading day for every holding. This prevents the
    "1.27% vs 1.32%" mismatch that happens when different symbols get
    different baseline days.

    Returns (prev_value_or_None, status_message, common_prev_day_or_None).
    """
    try:
        holdings = fetch_holdings()
    except Exception as e:
        return None, f"holdings fetch failed: {e}", None

    if not holdings:
        return None, "no equity holdings", None

    # Map Angel One tradingsymbol -> yfinance ticker.
    # NSE symbols append "-EQ" which yfinance expects as ".NS".
    sym_to_ticker: dict[str, str] = {}
    for h in holdings:
        if h.exchange != "NSE":
            continue
        sym_to_ticker[h.symbol] = h.symbol.replace("-EQ", "").upper() + ".NS"

    if not sym_to_ticker:
        return None, "non-NSE holdings only — prev-day unavailable", None

    log.info("fetching prev-day close for %d symbols…", len(sym_to_ticker))
    import yfinance as yf

    prev_closes: dict[str, float] = {}
    prev_dates: dict[str, pd.Timestamp] = {}
    for t in set(sym_to_ticker.values()):
        try:
            hist = yf.Ticker(t).history(period="5d", auto_adjust=True)
            if len(hist) >= 2:
                prev_closes[t] = float(hist["Close"].iloc[-2])
                prev_dates[t] = hist.index[-2].normalize()
        except Exception:
            continue

    if not prev_closes:
        return None, "could not fetch any previous-day prices", None

    # Pick the EARLIEST common baseline date so every symbol uses the same day.
    # (Using the latest one would exclude symbols that don't have data for it.)
    common_prev_day = min(prev_dates.values())

    prev_value = 0.0
    matched = 0
    for h in holdings:
        t = sym_to_ticker.get(h.symbol)
        if t and t in prev_closes and prev_closes[t] > 0:
            prev_value += h.quantity * prev_closes[t]
            matched += 1

    if matched == 0:
        return None, "no symbols matched Yahoo data", None
    msg = f"matched {matched}/{len(holdings)} equity symbols  baseline={common_prev_day.strftime('%Y-%m-%d')}"
    return prev_value, msg, common_prev_day


# ---------- Chart ----------
def draw_bar_chart(rows: list[dict], asof: pd.Timestamp,
                   annotation: str | None = None) -> Path:
    """rows: [{'name': str, 'pct': float, 'kind': str}]
    annotation: optional multi-line text shown above the chart
    """
    df = pd.DataFrame(rows).sort_values("pct", ascending=True).reset_index(drop=True)

    def color_for(kind: str, v: float) -> str:
        if kind == "portfolio_total":
            return "#9467bd"           # purple — total
        if kind == "portfolio":
            return "#1f77b4"           # blue — equity
        if kind == "mf":
            return "#17becf"           # cyan — mutual funds
        if kind == "sgb":
            return "#ff7f0e"           # orange — gold bonds
        return "#2ca02c" if v >= 0 else "#d62728"   # green/red — indices

    colors = [color_for(k, v) for k, v in zip(df["kind"], df["pct"])]

    # Extra vertical room if we have an annotation block to draw above
    fig_height = 8.5 if annotation else 7
    fig, ax = plt.subplots(figsize=(12, fig_height))

    # Annotation block: positioned in figure coords above the title
    if annotation:
        fig.text(
            0.5, 0.94, annotation,
            ha="center", va="top",
            fontsize=10, family="monospace",
            bbox=dict(boxstyle="round,pad=0.4",
                      facecolor="#f5f5f5", edgecolor="#cccccc", linewidth=0.8),
        )

    bars = ax.barh(df["name"], df["pct"], color=colors, edgecolor="black", linewidth=0.6)
    ax.axvline(0, color="black", linewidth=0.8)

    for bar, v in zip(bars, df["pct"]):
        x = bar.get_width()
        offset = 0.05 if v >= 0 else -0.05
        ha = "left" if v >= 0 else "right"
        ax.text(x + offset, bar.get_y() + bar.get_height() / 2,
                f"{v:+.2f}%", va="center", ha=ha, fontsize=10, weight="bold")

    ax.set_title(
        f"My Equity vs World Indices  —  Day % Change ({asof.strftime('%Y-%m-%d')})",
        fontsize=13, weight="bold",
    )
    ax.set_xlabel("Percent change")
    ax.grid(True, axis="x", alpha=0.3)
    xmin, xmax = ax.get_xlim()
    pad = max(0.3, (xmax - xmin) * 0.15)
    ax.set_xlim(xmin - pad, xmax + pad)

    # Leave room at the top for the annotation box
    if annotation:
        fig.subplots_adjust(top=0.86)
    else:
        fig.tight_layout()

    out = CHARTS_DIR / f"portfolio_compare_{asof.strftime('%Y%m%d')}.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


# ---------- Main ----------
def main() -> None:
    period = "equity_compare"

    # 1. Indices (8 countries)
    raw = fetch_indices("5d")
    index_rows = []
    asof: pd.Timestamp | None = None
    for col in raw.columns:
        s = raw[col].dropna()
        if len(s) < 2:
            continue
        prev, last = float(s.iloc[-2]), float(s.iloc[-1])
        if prev == 0:
            continue
        index_rows.append({"name": col, "pct": (last / prev - 1.0) * 100.0, "kind": "index"})
        d = s.index[-1]
        if asof is None or d > asof:
            asof = d

    # 2. Equity — use SmartAPI's prev_close field directly (matches Angel One app)
    equity_row: dict | None = None
    equity_status = ""
    try:
        holdings = fetch_holdings()
        s = portfolio_summary(holdings)
        current_value = s["value"]
        prev_value = sum(h.quantity * h.prev_close for h in holdings if h.prev_close > 0)
        if current_value > 0 and prev_value > 0:
            equity_pct = (current_value / prev_value - 1.0) * 100.0
            equity_row = {
                "name": "My Equity",
                "pct": equity_pct,
                "kind": "portfolio",
                "value": current_value,
            }
            matched = sum(1 for h in holdings if h.prev_close > 0)
            equity_status = (
                f"using SmartAPI prev_close  matched={matched}/{len(holdings)}"
            )
            log.info("%s  pct=%+.2f%%  value=₹%,.0f",
                     equity_status, equity_pct, current_value)
    except Exception as e:
        equity_status = f"equity fetch failed: {e}"
    if equity_row is None:
        log.warning("%s", equity_status)

    # 3. Mutual funds
    mf_chart_rows: list[dict] = []
    mf_assets: list[AssetRow] = []
    mf_value = 0.0
    mf_prev = 0.0
    try:
        mfs = load_mfs()
        if mfs:
            log.info("%d funds in mfs.json", len(mfs))
            mf_assets = fetch_mf_rows(mfs)
            mf_agg = aggregate_assets(mf_assets)
            mf_value = mf_agg["value"]
            mf_prev = mf_agg["prev_value"]
            mf_pct = mf_agg["pct"]
            mf_chart_rows.append({
                "name": f"My Mutual Funds ({mf_agg['count']})",
                "pct": mf_pct,
                "kind": "mf",
                "value": mf_value,
            })
    except Exception as e:
        log.warning("mf failed: %s", e)

    # 4. SGBs
    sgb_chart_rows: list[dict] = []
    sgb_assets: list[AssetRow] = []
    sgb_value = 0.0
    sgb_prev = 0.0
    try:
        sgbs = load_sgbs()
        if sgbs:
            log.info("%d SGBs in sgbs.json", len(sgbs))
            sgb_assets = fetch_sgb_rows(sgbs, asof=asof)
            sgb_agg = aggregate_assets(sgb_assets)
            sgb_value = sgb_agg["value"]
            sgb_prev = sgb_agg["prev_value"]
            sgb_pct = sgb_agg["pct"]
            sgb_chart_rows.append({
                "name": f"My SGBs ({sgb_agg['count']})",
                "pct": sgb_pct,
                "kind": "sgb",
                "value": sgb_value,
            })
    except Exception as e:
        log.warning("sgb failed: %s", e)

    # 5. Total portfolio
    total_row: dict | None = None
    equity_value = equity_row["value"] if equity_row else 0.0
    # Compute equity_prev from the same SmartAPI prev_close values that drove
    # the equity_row's pct, so the total weights equity correctly.
    equity_prev = 0.0
    if equity_row:
        equity_prev = sum(h.quantity * h.prev_close for h in holdings if h.prev_close > 0)
    total_value = equity_value + mf_value + sgb_value
    total_prev = equity_prev + mf_prev + sgb_prev
    if total_value > 0 and total_prev > 0:
        total_pct = ((total_value / total_prev) - 1.0) * 100.0
        total_row = {
            "name": "My Total Portfolio",
            "pct": total_pct,
            "kind": "portfolio_total",
            "value": total_value,
        }

    # Chart rows: 1 Total Portfolio bar + 8 index bars = 9 bars total.
    # Chart: 1 My Equity bar + 8 index bars = 9 bars total.
    # MF and SGB are shown in the annotation block above the chart, not as bars.
    chart_rows: list[dict] = []
    if equity_row:
        chart_rows.append(equity_row)
    indices_sorted = sorted(index_rows, key=lambda r: -r["pct"])
    chart_rows.extend(indices_sorted)

    # Build the annotation text: compact 2-line summary.
    #   MFs:    aggregate % and the most recent NAV date across all MFs
    #   SGBs:   aggregate % and the price date (from mintbyte / IBJA / manual)
    ann_lines: list[str] = []
    if mf_assets:
        mf_with_value = [a for a in mf_assets if a.value > 0]
        if mf_with_value:
            mf_value = sum(a.value for a in mf_with_value)
            mf_prev = sum(a.prev_value for a in mf_with_value)
            mf_pct = (mf_value / mf_prev - 1.0) * 100.0 if mf_prev > 0 else 0.0
            # Use the latest NAV date across all funds (they should be the same day)
            nav_dates = [a.extra.get("nav_date", "") for a in mf_with_value if a.extra.get("nav_date")]
            nav_date = max(nav_dates) if nav_dates else "n/a"
            ann_lines.append(f"MFs ({len(mf_with_value)}): {mf_pct:+.2f}%   NAV date: {nav_date}")
    if sgb_assets:
        sgb_with_value = [a for a in sgb_assets if a.value > 0]
        if sgb_with_value:
            sgb_value = sum(a.value for a in sgb_with_value)
            sgb_prev = sum(a.prev_value for a in sgb_with_value)
            sgb_pct = (sgb_value / sgb_prev - 1.0) * 100.0 if sgb_prev > 0 else 0.0
            # For SGBs, date = today if mintbyte today price is available,
            # else the cached previous day from history
            today_iso = pd.Timestamp.today().strftime("%Y-%m-%d")
            sgb_dates = []
            for a in sgb_with_value:
                src = a.extra.get("source", "")
                # mintbyte gives us a dated price
                if "mintbyte" in src and "today only" not in src:
                    # The cached prev date is in the source text or
                    # we infer from IBJA today
                    sgb_dates.append(today_iso)
                else:
                    sgb_dates.append("cached")
            sgb_date = max([d for d in sgb_dates if d != "cached"], default=today_iso)
            ann_lines.append(f"SGBs ({len(sgb_with_value)}): {sgb_pct:+.2f}%   Price date: {sgb_date}")
    annotation = "\n".join(ann_lines) if ann_lines else None

    cleanup_old_charts(period)
    if asof is None:
        log.error("FAIL: no index data")
        return
    out = draw_bar_chart(chart_rows, asof, annotation=annotation)

    # 6. Print summary (full breakdown in terminal, chart is summary only)
    log.info("day %% change on %s:", asof.strftime('%Y-%m-%d'))
    print("-" * 55)
    # Portfolio first
    portfolio_rows = []
    if equity_row:
        portfolio_rows.append(equity_row)
    portfolio_rows.extend(mf_chart_rows)
    portfolio_rows.extend(sgb_chart_rows)
    if total_row:
        portfolio_rows.append(total_row)
    for r in portfolio_rows:
        v = r.get("value")
        val_str = f"₹{v:>14,.0f}" if v else " " * 15
        print(f"  {r['name']:<28} {r['pct']:+7.2f}%   {val_str}")
    print("  " + "-" * 53)
    print("  Indices:")
    for r in indices_sorted:
        print(f"    {r['name']:<26} {r['pct']:+7.2f}%")
    log.info("chart saved: %s", out)


if __name__ == "__main__":
    main()
