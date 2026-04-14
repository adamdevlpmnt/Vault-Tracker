"""Configuration loaded entirely from environment variables."""

from __future__ import annotations

import os


class Config:
    """Immutable runtime configuration read from env vars."""

    # qBittorrent connection
    QB_HOST: str = os.getenv("QB_HOST", "http://localhost")
    QB_PORT: int = int(os.getenv("QB_PORT", "8080"))
    QB_USERNAME: str = os.getenv("QB_USERNAME", "admin")
    QB_PASSWORD: str = os.getenv("QB_PASSWORD", "adminadmin")

    # Retry (initial connection only)
    RETRY_DELAY: int = int(os.getenv("RETRY_DELAY", "30"))  # seconds
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "0"))  # 0 = infinite

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "/data/vault-tracker.db")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # Filter: minimum torrent size in bytes (0 = no filter)
    MIN_SIZE_BYTES: int = int(os.getenv("MIN_SIZE_BYTES", "0"))

    @property
    def qb_url(self) -> str:
        """Full base URL for the qBittorrent WebUI API."""
        host = self.QB_HOST.rstrip("/")
        return f"{host}:{self.QB_PORT}"

    @property
    def min_size_display(self) -> str:
        """Human-readable minimum size, or 'no filter' when disabled."""
        if self.MIN_SIZE_BYTES == 0:
            return "no filter"
        gb = self.MIN_SIZE_BYTES / (1024 * 1024 * 1024)
        if gb >= 1:
            return f"{gb:.1f} GB"
        mb = self.MIN_SIZE_BYTES / (1024 * 1024)
        return f"{mb:.0f} MB"

    def __repr__(self) -> str:
        return (
            f"Config(qb_url={self.qb_url!r}, "
            f"retry={self.RETRY_DELAY}s, db={self.DB_PATH!r}, "
            f"min_size={self.min_size_display}, log_level={self.LOG_LEVEL!r})"
        )
