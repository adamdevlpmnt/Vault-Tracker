"""qBittorrent WebUI API client with automatic session management and retries."""

from __future__ import annotations

import re
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

    def get_torrent_properties(self, torrent_hash: str) -> Dict[str, Any]:
        """Return torrent properties."""
        resp = self._request(
            "GET", "torrents/properties", params={"hash": torrent_hash}
        )
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

    # ── private detection ─────────────────────────────────────────────

    @staticmethod
    def is_private_from_info(torrent: Dict[str, Any]) -> Optional[bool]:
        """Check the is_private / isPrivate field from torrents/info.
        Available since qBittorrent 5.0. Returns None if field is absent."""
        # Try both field names (API inconsistency between versions)
        for key in ("is_private", "isPrivate", "private"):
            if key in torrent:
                return bool(torrent[key])
        return None

    @staticmethod
    def is_private_from_properties(props: Dict[str, Any]) -> Optional[bool]:
        """Check the private flag from torrents/properties.
        Field name varies between qBittorrent versions."""
        for key in ("isPrivate", "is_private", "private"):
            if key in props:
                return bool(props[key])
        return None

    @staticmethod
    def is_private_from_tracker_messages(trackers: List[Dict[str, Any]]) -> bool:
        """Check if any tracker's message says 'private'.
        qBittorrent DHT/PeX/LSD entries show 'Ce torrent est privé'
        or 'This torrent is private' for private torrents."""
        for t in trackers:
            msg = t.get("msg", "").lower()
            if "private" in msg or "privé" in msg or "privee" in msg:
                return True
        return False

    @staticmethod
    def get_real_trackers(trackers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter out qBittorrent internal entries (DHT, PeX, LSD).
        Only keep actual HTTP/HTTPS/UDP tracker URLs."""
        return [
            t for t in trackers
            if t.get("url", "").startswith(("http://", "https://", "udp://"))
        ]

    @staticmethod
    def mask_url(url: str) -> str:
        """Partially mask a tracker URL for safe logging.

        Handles both:
         - Query params: ?passkey=abc123 → ?passkey=abc***123
         - Path keys:    /announce/abc123def456 → /announce/abc***456
        """
        parsed = urlparse(url)

        if parsed.query:
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

        # Mask long hex/alphanum segments in path (keys embedded in path)
        path = parsed.path
        parts = path.rsplit("/", 1)
        if len(parts) == 2 and len(parts[1]) > 8:
            segment = parts[1]
            masked_segment = segment[:3] + "***" + segment[-3:]
            path = parts[0] + "/" + masked_segment

        return f"{parsed.scheme}://{parsed.netloc}{path}"
