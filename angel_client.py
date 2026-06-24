"""
angel_client.py
----------------
Thin wrapper around Angel One SmartAPI.

Responsibilities:
  - Load credentials from the local `.env` file (never hard-coded).
  - Log in once per day using TOTP + MPIN.
  - Read-only calls only: getHoldings, getLTP, getRMS.
  - NEVER log the credentials. Log only "login ok" or "login failed: <reason>".

The SmartAPI session token expires daily; we generate a fresh one on every
script run. The TOTP secret + MPIN are used only for that single login.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pyotp
from dotenv import load_dotenv

from logging_setup import get_logger

log = get_logger("angel")

# Load .env from the project root, regardless of cwd.
PROJECT = Path(__file__).resolve().parent
load_dotenv(PROJECT / ".env")


def _get(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val or val == "replace_me":
        raise RuntimeError(
            f"Missing {name} in .env. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


@dataclass
class Holding:
    symbol: str              # tradingsymbol, e.g. "RELIANCE-EQ"
    exchange: str            # "NSE" or "BSE"
    quantity: int
    avg_price: float         # cost basis (SmartAPI: averageprice)
    ltp: float               # last traded price (SmartAPI: ltp)
    prev_close: float        # previous trading day's close (SmartAPI: close)
    symbol_token: str        # needed for some SmartAPI calls

    @property
    def invested(self) -> float:
        return self.quantity * self.avg_price

    @property
    def current_value(self) -> float:
        return self.quantity * self.ltp

    @property
    def pnl(self) -> float:
        return self.current_value - self.invested

    @property
    def pnl_pct(self) -> float:
        if self.invested == 0:
            return 0.0
        return (self.ltp / self.avg_price - 1.0) * 100.0

    @property
    def day_pnl(self) -> float:
        """Day change in value vs previous close."""
        if self.prev_close <= 0:
            return 0.0
        return self.quantity * (self.ltp - self.prev_close)

    @property
    def day_pct(self) -> float:
        """Day change % vs previous close."""
        if self.prev_close <= 0:
            return 0.0
        return (self.ltp / self.prev_close - 1.0) * 100.0


def login() -> "SmartConnect":
    """Create a logged-in SmartConnect client. Logs in once per call."""
    # Import lazily so the rest of the project still works without the SDK installed.
    from SmartApi import SmartConnect  # type: ignore

    api_key = _get("ANGEL_API_KEY")
    client_code = _get("ANGEL_CLIENT_CODE")
    pin = _get("ANGEL_MPIN")
    totp_secret = _get("ANGEL_TOTP_SECRET")

    totp = pyotp.TOTP(totp_secret).now()

    obj = SmartConnect(api_key=api_key)
    try:
        data = obj.generateSession(client_code, pin, totp)
    except Exception as e:
        raise RuntimeError(f"Angel One login failed: {e}") from e

    if not data or not isinstance(data, dict):
        raise RuntimeError("Angel One login failed: empty response")
    if data.get("status") is False or not data.get("data"):
        raise RuntimeError(
            f"Angel One login failed: {data.get('message', 'unknown error')}"
        )

    # Refresh the access token; some endpoints require it.
    auth_token = data["data"]["jwtToken"]
    refresh_token = data["data"]["refreshToken"]
    try:
        obj.getProfile(refresh_token)
    except Exception:
        pass  # profile fetch is best-effort

    # We do NOT log or return the tokens — they stay inside the SmartConnect obj.
    log.info("login ok  client=%s  ts=%d", client_code, int(time.time()))
    return obj


def fetch_holdings() -> list[Holding]:
    """
    Fetch equity holdings. SmartAPI returns both the current LTP and the
    previous trading day's close in the same response — we use both for
    day-change computation (matches Angel One's app exactly, no yfinance
    dependency, no auto-adjust surprises).
    """
    obj = login()

    resp = obj.holding() or {}
    # SmartAPI's holding() returns a dict of the form:
    #   {"status": bool, "message": str, "data": [ {holding...}, ... ]}
    if isinstance(resp, dict):
        if resp.get("status") is False:
            raise RuntimeError(f"Angel One holdings error: {resp.get('message', 'unknown')}")
        raw = resp.get("data") or []
    elif isinstance(resp, list):
        raw = resp
    else:
        raise RuntimeError(f"Unexpected holding() response type: {type(resp).__name__}")

    if not raw:
        log.info("no holdings found")
        return []

    # Fallback: any symbol with ltp=0 needs a fresh LTP fetch
    needs_ltp = [
        h for h in raw
        if h.get("tradingsymbol") and h.get("symboltoken")
        and (h.get("ltp") is None or float(h.get("ltp") or 0) == 0)
    ]
    ltp_map: dict[str, float] = {}
    if needs_ltp:
        payload = [
            {
                "exchange": h.get("exchange", "NSE"),
                "tradingsymbol": h.get("tradingsymbol"),
                "symboltoken": h.get("symboltoken"),
            }
            for h in needs_ltp
        ]
        try:
            r = obj.getMarketData(mode="LTP", exchangeTokens=payload) or {}
            fetched = r.get("data", []) if isinstance(r, dict) else []
            for item in fetched:
                ts = item.get("tradingsymbol")
                ltp_map[ts] = float(item.get("ltp", 0) or 0)
        except Exception as e:
            log.warning("LTP fetch failed: %s  (using avg price as fallback)", e)

    holdings: list[Holding] = []
    missing_prev_close = 0
    for h in raw:
        ts = h.get("tradingsymbol")
        if not ts:
            continue
        ltp_field = float(h.get("ltp") or 0)
        ltp = ltp_map.get(ts) or ltp_field or float(h.get("averageprice") or 0)
        prev_close = float(h.get("close") or 0)
        if prev_close <= 0:
            missing_prev_close += 1
        holdings.append(
            Holding(
                symbol=ts,
                exchange=h.get("exchange", "NSE"),
                quantity=int(h.get("quantity", 0) or 0),
                avg_price=float(h.get("averageprice", 0) or 0),
                ltp=ltp,
                prev_close=prev_close,
                symbol_token=h.get("symboltoken", ""),
            )
        )
    if missing_prev_close:
        log.info("%d holdings have prev_close=0 "
                 "(likely newly listed or no prior session); day-change for those will be 0%%",
                 missing_prev_close)
    return holdings


def portfolio_summary(holdings: Iterable[Holding]) -> dict:
    """Aggregate holdings into a single portfolio snapshot."""
    hs = list(holdings)
    invested = sum(h.invested for h in hs)
    value = sum(h.current_value for h in hs)
    return {
        "count": len(hs),
        "invested": invested,
        "value": value,
        "pnl": value - invested,
        "pnl_pct": ((value / invested) - 1.0) * 100.0 if invested else 0.0,
    }
