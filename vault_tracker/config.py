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

    # Polling
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "10"))  # seconds

    # Retry
    RETRY_DELAY: int = int(os.getenv("RETRY_DELAY", "30"))  # seconds
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "0"))  # 0 = infinite

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "/data/vault-tracker.db")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # Behaviour
    STRIP_PUBLIC_TRACKERS: bool = os.getenv("STRIP_PUBLIC_TRACKERS", "false").lower() == "true"

    @property
    def qb_url(self) -> str:
        """Full base URL for the qBittorrent WebUI API."""
        host = self.QB_HOST.rstrip("/")
        return f"{host}:{self.QB_PORT}"

    def __repr__(self) -> str:
        return (
            f"Config(qb_url={self.qb_url!r}, poll={self.POLL_INTERVAL}s, "
            f"retry={self.RETRY_DELAY}s, db={self.DB_PATH!r}, "
            f"log_level={self.LOG_LEVEL!r})"
        )
