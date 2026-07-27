"""
logging_setup.py
----------------
Centralised logging for the portfolio tracker.

Goals:
  - One call sets up everything: stdout + daily-rotated log file.
  - Captures timestamp, level, module, message.
  - logzero handles rotation (uses the existing ``logzero`` dep).
  - Idempotent: calling configure_logging() twice returns the same logger.
  - Safe to import before any other project module.

Usage:
    from .logging_setup import get_logger
    log = get_logger(__name__)
    log.info("hello")
    log.warning("something off: %s", detail)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import logzero
from logzero import LogFormatter

from pipeline.runtime_paths import data_root

PROJECT = Path(__file__).resolve().parent.parent  # portfolio-tracker/ root
LOGS_DIR = data_root() / "logs"

# Default log level. Override via PT_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR env.
_DEFAULT_LEVEL = "INFO"

_configured = False


def _resolve_level() -> int:
    import os
    name = os.getenv("PT_LOG_LEVEL", _DEFAULT_LEVEL).upper()
    return getattr(logging, name, logging.INFO)


def configure_logging(level: int | None = None) -> logging.Logger:
    """
    Configure the root ``portfolio`` logger.
    Safe to call multiple times — only the first call has effect.
    """
    global _configured
    if _configured:
        return logging.getLogger("portfolio")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    day_dir = LOGS_DIR / datetime.now().strftime("%Y-%m-%d")
    day_dir.mkdir(exist_ok=True)
    log_file = day_dir / "app.log"

    lvl = level if level is not None else _resolve_level()

    # logzero's setup_logger gives us both a rotating file handler and a
    # colourised stdout handler in one call.
    formatter = LogFormatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logzero.setup_logger(
        name="portfolio",
        logfile=str(log_file),
        level=lvl,
        formatter=formatter,
        maxBytes=5_000_000,    # 5 MB
        backupCount=5,
        disableStderrLogger=False,
    )
    # Silence the noisy SmartConnect logger (it spams "in pool" at INFO)
    logging.getLogger("SmartApi").setLevel(logging.WARNING)
    logging.getLogger("smartConnect").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _configured = True
    logger = logging.getLogger("portfolio")
    logger.info("logging configured  level=%s  file=%s", logging.getLevelName(lvl), log_file)
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger under the ``portfolio`` namespace.
    The root logger is configured lazily on first use.
    """
    if not _configured:
        configure_logging()
    if not name.startswith("portfolio"):
        name = f"portfolio.{name}"
    return logging.getLogger(name)