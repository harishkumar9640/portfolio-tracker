"""
runtime_paths.py
----------------
Resolves the writable data root for this process.

Locally (your Mac, or any normal host) this is simply the project's own
``data/`` directory, exactly as before.

On Vercel, the deployment filesystem is read-only except for ``/tmp``
(see https://vercel.com/docs/functions/serverless-functions/runtimes#file-system).
Every pipeline module that writes logs, caches, chart HTML, or alert
dedupe state does so under a ``data/`` folder computed from the project
root — which fails immediately at import time on Vercel with a
read-only-filesystem error, crashing the function (FUNCTION_INVOCATION_FAILED).

Vercel sets the ``VERCEL`` environment variable to "1" in every deployed
function's runtime (see
https://vercel.com/docs/environment-variables/system-environment-variables).
We use that to redirect all writes to ``/tmp/portfolio-tracker-data``
instead of the repo's ``data/`` folder when running there.

IMPORTANT CAVEAT: ``/tmp`` on Vercel is ephemeral per-instance and is
wiped on cold start. This means on Vercel:
  - Log files, chart caches, and price caches simply get rebuilt as
    needed — no functional loss.
  - Alert dedupe state (seen.json / log.json for news_alert,
    portfolio_impact, mf_holdings_alert, shareholding_alert, etc.)
    will NOT persist across invocations, so those schedulers should
    NOT be relied on when running on Vercel. This is expected: the
    scheduler threads only start from webapp.server's __main__ guard
    (see webapp/server.py), which Vercel's serverless import of
    ``webapp.server:app`` never triggers. The Telegram alert jobs
    should keep running from a long-lived process (e.g. your Mac via
    ``python -m webapp.server``), not from the Vercel deployment.
"""
from __future__ import annotations

import os
from pathlib import Path

# Any project module can import PROJECT_ROOT() instead of recomputing
# Path(__file__).resolve().parent.parent locally.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_vercel() -> bool:
    """True when running as a Vercel serverless function."""
    return bool(os.environ.get("VERCEL"))


def data_root() -> Path:
    """
    Return the writable root to use for the ``data/`` tree.

    Locally: <project>/data (unchanged behaviour).
    On Vercel: /tmp/portfolio-tracker-data (the only writable path).
    """
    if is_vercel():
        root = Path("/tmp/portfolio-tracker-data")
    else:
        root = PROJECT_ROOT / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root
