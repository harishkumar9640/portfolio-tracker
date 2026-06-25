#!/usr/bin/env python3
"""
fairvalue.py
------------
Thin CLI shim. The actual implementation lives in ``fair_value/``.

Run:
    python3 fairvalue.py RELIANCE TCS INFY
    python3 fairvalue.py                       # reads my_tickers.txt
    python3 fairvalue.py --output-file out.csv
"""
from fair_value.valuation import main

if __name__ == "__main__":
    main()