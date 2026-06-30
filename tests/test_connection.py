"""
test_connection.py
------------------
Smoke test for Stage 2 setup.

Run:
    python3 test_connection.py

What it does:
  1. Loads credentials from .env
  2. Logs into Angel One via SmartAPI (TOTP + MPIN)
  3. Prints your holdings count and a small table

If this fails, the daily script will also fail — fix this first.
"""
from pipeline.angel_client import fetch_holdings, portfolio_summary


def main() -> None:
    print("== Angel One connection test ==")
    try:
        holdings = fetch_holdings()
    except Exception as e:
        print(f"\nFAIL: {e}")
        print("\nChecklist:")
        print("  - .env exists in the project root and has all 4 values")
        print("  - API key was copied exactly (no leading/trailing spaces)")
        print("  - MPIN matches the one you use in the Angel One app")
        print("  - TOTP secret is the base32 string from the QR code")
        print("  - Your system clock is accurate (TOTP is time-based)")
        return

    s = portfolio_summary(holdings)
    print(f"\nholdings : {s['count']}")
    print(f"invested : ₹{s['invested']:,.2f}")
    print(f"value    : ₹{s['value']:,.2f}")
    print(f"P&L      : ₹{s['pnl']:,.2f}  ({s['pnl_pct']:+.2f}%)")

    if holdings:
        print("\nTop 5 holdings by current value:")
        for h in sorted(holdings, key=lambda x: -x.current_value)[:5]:
            print(f"  {h.symbol:<22} qty={h.quantity:>6}  "
                  f"avg={h.avg_price:>10.2f}  ltp={h.ltp:>10.2f}  "
                  f"pnl={h.pnl_pct:+7.2f}%")


if __name__ == "__main__":
    main()
