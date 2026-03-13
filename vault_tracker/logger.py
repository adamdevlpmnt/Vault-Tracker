"""Structured, emoji-rich logging for Vault-Tracker.

Every log line follows the format:
    [2026-03-13 13:00:01] [LEVEL] <emoji> <message>
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone


class VaultFormatter(logging.Formatter):
    """Custom formatter producing aligned, timestamped lines."""

    LEVEL_MAP = {
        "DEBUG": "DEBUG",
        "INFO": "INFO ",
        "WARNING": "WARN ",
        "ERROR": "ERROR",
        "CRITICAL": "CRIT ",
    }

    def format(self, record: logging.LogRecord) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level = self.LEVEL_MAP.get(record.levelname, record.levelname.ljust(5))
        msg = record.getMessage()
        return f"[{now}] [{level}] {msg}"


def get_logger(name: str = "vault-tracker", level: str = "INFO") -> logging.Logger:
    """Return the singleton application logger."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(VaultFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger
