"""Broker adapters for Tax P&L parsing.

Each adapter implements the BrokerAdapter protocol:
    name: str
    can_parse(file: Path) -> bool
    parse(file: Path) -> dict  # {"fy_summaries", "trades", "open_holdings"}

Public entry point: get_adapter(file) -> BrokerAdapter | None
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pipeline.tax_pnl.adapters.angel_one import AngelOneAdapter
from pipeline.tax_pnl.adapters.zerodha import ZerodhaAdapter
from pipeline.tax_pnl.adapters.generic import GenericAdapter


class BrokerAdapter(Protocol):
    name: str

    def can_parse(self, file: Path) -> bool: ...
    def parse(self, file: Path) -> dict: ...


_ADAPTERS: list[BrokerAdapter] = [
    AngelOneAdapter(),
    ZerodhaAdapter(),
    # GenericAdapter is only used via explicit user-supplied column mapping,
    # not via can_parse() auto-detection.
]


def get_adapter(file: Path) -> BrokerAdapter | None:
    """Return the first adapter that claims to handle this file, or None."""
    for adapter in _ADAPTERS:
        if adapter.can_parse(file):
            return adapter
    return None


def get_generic_adapter(mapping: dict) -> GenericAdapter:
    """Build a GenericAdapter with the user-provided column mapping."""
    return GenericAdapter(column_mapping=mapping)


def all_supported_brokers() -> list[dict]:
    """Human-readable list of supported brokers for the upload UI."""
    return [
        {"name": "angel_one", "label": "Angel One (SmartAPI 'Tax PNL' xlsx)"},
        {"name": "zerodha",   "label": "Zerodha (Console P&L CSV)"},
        {"name": "generic",   "label": "Generic (manual column mapping)"},
    ]
