"""qBittorrent WebUI API client with automatic session management and retries."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from vault_tracker.config import Config
from vault_tracker.logger import get_logger

log = get_logger()


class QBittorrentError(Exception):
    """Raised when the qBittorrent API returns an unexpected response."""


class QBittorrentClient:
    """Stateful HTTP client for the qBittorrent WebUI API v2."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._base = cfg.qb_url.rstrip("/") + "/api/v2"
        self._session = requests.Session()
        self._authenticated = False

    # ── authentication ────────────────────────────────────────────────

    def login(self) -> None:
        """Authenticate and store the SID cookie."""
        url = f"{self._base}/auth/login"
        try:
            resp = self._session.post(
                url,
                data={
                    "username": self._cfg.QB_USERNAME,
                    "password": self._cfg.QB_PASSWORD,
                },
                timeout=15,
            )
            if resp.text.strip().lower() == "ok.":
                self._authenticated = True
                log.info("🔌 Connected to qBittorrent WebUI → ✅ OK")
            else:
                self._authenticated = False
                log.error(
                    "🔌 Connected to qBittorrent WebUI → ❌ ERROR (bad credentials)"
                )
                raise QBittorrentError("Authentication failed – check QB_USERNAME / QB_PASSWORD")
        except requests.RequestException as exc:
            self._authenticated = False
            log.error("🔌 Connecting to qBittorrent WebUI → ❌ ERROR (%s)", exc)
            raise QBittorrentError(str(exc)) from exc

    def _request(
        self,
        method: str,
        endpoint: str,
        retries: int = 2,
        **kwargs: Any,
    ) -> requests.Response:
        """Perform an API call, re-authenticating once on 403."""
        url = f"{self._base}/{endpoint}"
        kwargs.setdefault("timeout", 15)
        for attempt in range(retries + 1):
            try:
                resp = self._session.request(method, url, **kwargs)
                if resp.status_code == 403 and attempt < retries:
                    log.warning("⚠️  Session expired, re-authenticating…")
                    self.login()
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException:
                if attempt < retries:
                    time.sleep(1)
                    continue
                raise
        raise QBittorrentError("Max retries exceeded")  # pragma: no cover

    # ── torrent operations ────────────────────────────────────────────

    def get_torrents(self) -> List[Dict[str, Any]]:
        """Return the full torrent list."""
        resp = self._request("GET", "torrents/info")
        return resp.json()

    def get_torrent_trackers(self, torrent_hash: str) -> List[Dict[str, Any]]:
        """Return tracker list for a torrent."""
        resp = self._request(
            "GET", "torrents/trackers", params={"hash": torrent_hash}
        )
        return resp.json()

    def add_trackers(self, torrent_hash: str, urls: List[str]) -> None:
        """Add one or more tracker URLs to a torrent."""
        self._request(
            "POST",
            "torrents/addTrackers",
            data={"hash": torrent_hash, "urls": "\n".join(urls)},
        )

    def remove_trackers(self, torrent_hash: str, urls: List[str]) -> None:
        """Remove tracker URLs from a torrent."""
        self._request(
            "POST",
            "torrents/removeTrackers",
            data={"hash": torrent_hash, "urls": "|".join(urls)},
        )

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def is_private_tracker(tracker: Dict[str, Any]) -> bool:
        """Heuristic: a tracker is private if its message or status indicates so,
        or the URL contains a passkey / authkey / pid / torrent_pass / secure param."""
        url = tracker.get("url", "")
        # qBittorrent status codes: 0=disabled, 1=not contacted, 2=working, 3=updating, 4=not working
        # msg field may contain "This torrent is private"

        # Check URL for private-tracker tell-tales
        lower_url = url.lower()
        private_params = [
            "passkey=", "authkey=", "torrent_pass=", "pid=",
            "secure=", "auth=", "key=", "user=",
        ]
        for param in private_params:
            if param in lower_url:
                return True

        return False

    @staticmethod
    def mask_url(url: str) -> str:
        """Partially mask a tracker URL for safe logging.

        Example:
            https://tracker.example.com/ann?passkey=abc123def456
            → https://tracker.example.com/ann?passkey=abc***456
        """
        parsed = urlparse(url)
        if not parsed.query:
            return url
        # Mask each parameter value
        pairs = parsed.query.split("&")
        masked = []
        for pair in pairs:
            if "=" in pair:
                key, val = pair.split("=", 1)
                if len(val) > 6:
                    val = val[:3] + "***" + val[-3:]
                else:
                    val = "***"
                masked.append(f"{key}={val}")
            else:
                masked.append(pair)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{'&'.join(masked)}"
