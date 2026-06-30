"""
Bootstrap script for fair-value CLI subprocess tests.

Usage:
    python3 -m _mockpkg fairvalue.py RELIANCE --industry-pe 25

It installs the pipeline.fair_value mock and then exec()s the rest of argv
as if you had run them directly. Only does anything when
PT_FV_MOCK=1.
"""
import os
import sys


_FIXTURES = {
    "RELIANCE": {"ticker": "RELIANCE", "current_price": 1327.0,
                 "eps": 14.26, "book_value": 668.0, "market_cap": 1700000.0,
                 "operating_cash_flow_per_share": 141.97,
                 "source_url": "https://www.screener.in/company/RELIANCE/consolidated/",
                 "fetched_at": "2026-06-25T11:00:00"},
    "TCS": {"ticker": "TCS", "current_price": 2199.0,
            "eps": 31.13, "book_value": 296.0, "market_cap": 800000.0,
            "operating_cash_flow_per_share": 144.0,
            "source_url": "https://www.screener.in/company/TCS/consolidated/",
            "fetched_at": "2026-06-25T11:00:00"},
    "INFY": {"ticker": "INFY", "current_price": 1054.0,
             "eps": 32.0, "book_value": 220.0, "market_cap": 440000.0,
             "operating_cash_flow_per_share": 70.0,
             "source_url": "https://www.screener.in/company/INFY/consolidated/",
             "fetched_at": "2026-06-25T11:00:00"},
    "EDGE": {"ticker": "EDGE", "current_price": None, "eps": None,
             "book_value": None, "market_cap": None,
             "operating_cash_flow_per_share": None,
             "source_url": "", "fetched_at": "2026-06-25T11:00:00"},
    "X": {"ticker": "X", "current_price": 100.0, "eps": 10.0,
          "book_value": 50.0, "market_cap": 10000.0,
          "operating_cash_flow_per_share": 8.0,
          "source_url": "", "fetched_at": "2026-06-25T11:00:00"},
}


def main():
    if os.environ.get("PT_FV_MOCK") != "1":
        sys.stderr.write("PT_FV_MOCK must be set\n")
        sys.exit(2)
    # Patch pipeline.fair_value BEFORE fairvalue.py runs
    import pipeline.fair_value.fetcher as fetcher
    import pipeline.fair_value.valuation as fv
    import pipeline.fair_value as fv_pkg
    import pipeline.fair_value.search as search

    def fake_fetch(ticker):
        t = ticker.upper().strip()
        return _FIXTURES.get(t, {
            "ticker": t, "error": "mock: no fixture for " + t,
            "source_url": "", "fetched_at": "2026-06-25T11:00:00",
        })

    def fake_resolve(user_input):
        t = user_input.upper().strip()
        if t in _FIXTURES:
            return t, _FIXTURES[t].get("name", t)
        return t, user_input

    fetcher.fetch = fake_fetch
    fv.fetch = fake_fetch
    search.resolve_ticker = fake_resolve
    fv_pkg.resolve_ticker = fake_resolve

    # Now run the rest of argv. We're invoked as
    # `python3 -m tests._mockpkg <script> <args...>`
    # or  `python3 -m tests._mockpkg -m <module> <args...>`
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python3 -m _mockpkg <script|-m module> <args...>\n")
        sys.exit(2)
    script_and_args = sys.argv[1:]
    if script_and_args[0] == "-m":
        # Run a module: `python -m <module>` with the rest of argv
        if len(script_and_args) < 2:
            sys.stderr.write("usage: python3 -m _mockpkg -m <module> <args...>\n")
            sys.exit(2)
        import runpy
        module_name = script_and_args[1]
        sys.argv = [module_name] + script_and_args[2:]
        runpy.run_module(module_name, run_name="__main__", alter_sys=True)
    else:
        script_path = script_and_args[0]
        script_args = script_and_args[1:]
        # exec the script with its __name__ == "__main__"
        sys.argv = [script_path] + script_args
        with open(script_path, "r", encoding="utf-8") as f:
            source = f.read()
        exec(compile(source, script_path, "exec"),
             {"__name__": "__main__", "__file__": script_path})


if __name__ == "__main__":
    main()
