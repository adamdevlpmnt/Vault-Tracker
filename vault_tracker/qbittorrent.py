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

    # ── sync ──────────────────────────────────────────────────────────

    def sync_maindata(self, rid: int = 0) -> Dict[str, Any]:
        """Fetch delta sync data from qBittorrent.

        Uses /api/v2/sync/maindata with a request ID (rid) to receive only
        changes since the last call. Pass rid=0 for a full snapshot.
        Returns the parsed JSON response including 'rid', 'torrents', etc.
        """
        resp = self._request("GET", "sync/maindata", params={"rid": rid})
        return resp.json()

    # ── torrent operations ────────────────────────────────────────────

    def get_torrents(self) -> List[Dict[str, Any]]:
        """Return the full torrent list."""
        resp = self._request("GET", "torrents/info")
        return resp.json()

    def get_torrent_info(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """Return info for a single torrent by hash, or None if not found."""
        resp = self._request(
            "GET", "torrents/info", params={"hashes": torrent_hash}
        )
        result = resp.json()
        return result[0] if result else None

    def get_torrent_trackers(self, torrent_hash: str) -> List[Dict[str, Any]]:
        """Return tracker list for a torrent."""
        resp = self._request(
            "GET", "torrents/trackers", params={"hash": torrent_hash}
        )
        return resp.json()

    def remove_trackers(self, torrent_hash: str, urls: List[str]) -> None:
        """Remove tracker URLs from a torrent."""
        self._request(
            "POST",
            "torrents/removeTrackers",
            data={"hash": torrent_hash, "urls": "|".join(urls)},
        )

    def export_torrent(self, torrent_hash: str) -> bytes:
        """Export the .torrent file for a torrent.

        Returns the raw binary content of the .torrent file.
        """
        resp = self._request(
            "GET", "torrents/export", params={"hash": torrent_hash}
        )
        return resp.content

    def delete_torrent(self, torrent_hash: str, delete_files: bool = False) -> None:
        """Delete a torrent from qBittorrent.

        By default keeps downloaded files on disk (delete_files=False).
        """
        self._request(
            "POST",
            "torrents/delete",
            data={
                "hashes": torrent_hash,
                "deleteFiles": str(delete_files).lower(),
            },
        )

    def add_torrent_file(
        self,
        torrent_bytes: bytes,
        save_path: str,
        category: str = "",
        tags: str = "",
        paused: bool = False,
    ) -> None:
        """Add a .torrent file to qBittorrent.

        Re-adds the torrent with its original save path, category, and tags.
        qBittorrent will check existing files and resume seeding.
        """
        files = {"torrents": ("torrent.torrent", torrent_bytes, "application/x-bittorrent")}
        data: Dict[str, str] = {
            "savepath": save_path,
            "paused": str(paused).lower(),
        }
        if category:
            data["category"] = category
        if tags:
            data["tags"] = tags
        self._request("POST", "torrents/add", files=files, data=data)

    # ── helpers ───────────────────────────────────────────────────────

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

        # Mask long segments in path (keys embedded in path)
        path = parsed.path
        parts = path.rsplit("/", 1)
        if len(parts) == 2 and len(parts[1]) > 8:
            segment = parts[1]
            masked_segment = segment[:3] + "***" + segment[-3:]
            path = parts[0] + "/" + masked_segment

        return f"{parsed.scheme}://{parsed.netloc}{path}"
