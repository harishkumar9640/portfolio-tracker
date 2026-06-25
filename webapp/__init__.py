"""
webapp
------
FastAPI web dashboard for portfolio-tracker.

Surfaces:
  - Today's portfolio snapshot (equity + MF + SGB vs world indices)
  - Historical portfolio line chart (Plotly, embedded)
  - Fair-value table (screener.in data, computed valuations)
  - Settings (read-only view of mfs.json / sgbs.json / my_tickers.txt)
  - Refresh button that triggers a background re-run

Run:
    python3 -m webapp.server
    # Open http://localhost:8000
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "webapp" / "templates"
STATIC_DIR = PROJECT_ROOT / "webapp" / "static"