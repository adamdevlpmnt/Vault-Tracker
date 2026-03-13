"""Core Vault-Tracker service — the main polling loop.

Lifecycle:
    1. Connect to qBittorrent (retry until reachable).
    2. On restart, scan database for pending reinjections and process them.
    3. Enter polling loop:
       a. Fetch all torrents.
       b. Detect new torrents (not yet in our known set).
       c. For each new torrent:
          - Fetch torrent properties → check is_private flag.
          - If private: save ALL tracker URLs to DB, then strip them ALL.
          - Fallback: if properties don't have the flag, check URLs for
            passkey/authkey heuristics.
       d. For torrents in seeding state, reinject pending trackers from DB.
"""

from __future__ import annotations

import signal
import sys
import time
from typing import Any, Dict, List, Set

from vault_tracker.config import Config
from vault_tracker.database import TrackerDB
from vault_tracker.logger import get_logger
from vault_tracker.qbittorrent import QBittorrentClient, QBittorrentError

log = get_logger()

# qBittorrent states that mean "finished downloading and seeding"
SEEDING_STATES = frozenset({
    "uploading",      # actively seeding
    "stalledUP",      # seeding but no peers
    "forcedUP",       # force-seeding
    "queuedUP",       # queued for seeding
    "checkingUP",     # checking after download complete
})

# States that mean "still downloading"
DOWNLOADING_STATES = frozenset({
    "downloading",
    "stalledDL",
    "forcedDL",
    "queuedDL",
    "metaDL",
    "allocating",
    "checkingDL",
})


class VaultService:
    """Main service orchestrator."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._db = TrackerDB(cfg.DB_PATH)
        self._qb = QBittorrentClient(cfg)
        self._known_hashes: Set[str] = set()
        self._downloading_logged: Set[str] = set()
        self._running = True

        # Graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _: Any) -> None:
        log.info("🛑 Received signal %s — shutting down gracefully…", signum)
        self._running = False

    # ── connection with retry ─────────────────────────────────────────

    def _connect(self) -> None:
        """Block until qBittorrent is reachable."""
        attempt = 0
        while self._running:
            attempt += 1
            try:
                self._qb.login()
                return
            except QBittorrentError:
                if self._cfg.MAX_RETRIES and attempt >= self._cfg.MAX_RETRIES:
                    log.error(
                        "⚠️  qBittorrent unreachable after %d attempts — giving up",
                        attempt,
                    )
                    sys.exit(1)
                log.warning(
                    "⚠️  qBittorrent unreachable → retrying in %ds (attempt %d)",
                    self._cfg.RETRY_DELAY,
                    attempt,
                )
                time.sleep(self._cfg.RETRY_DELAY)

    # ── startup recovery ──────────────────────────────────────────────

    def _recover_pending(self) -> None:
        """On restart, list all pending reinjections and process any whose
        torrents are already seeding."""
        pending = self._db.get_all_pending()
        if not pending:
            log.info("🔁 Container restart → no pending reinjections in database")
            return

        # Group by torrent
        by_hash: Dict[str, list] = {}
        for thash, tname, turl, tier in pending:
            by_hash.setdefault(thash, []).append((tname, turl, tier))

        log.info(
            "🔁 Container restart → resuming from database state, "
            "%d torrent(s) with pending reinjections:",
            len(by_hash),
        )
        for thash, items in by_hash.items():
            name = items[0][0]
            log.info("    ├── %s [hash: %s] — %d tracker(s)", name, thash[:8], len(items))

        # Try to reinject any that are already seeding
        try:
            torrents = self._qb.get_torrents()
        except QBittorrentError:
            log.warning("⚠️  Cannot fetch torrent list for recovery — will retry in main loop")
            return

        torrent_map = {t["hash"]: t for t in torrents}
        for thash, items in by_hash.items():
            self._known_hashes.add(thash)
            t = torrent_map.get(thash)
            if t and t.get("state") in SEEDING_STATES:
                self._reinject_trackers(thash, t.get("name", "?"))

    # ── private torrent detection ─────────────────────────────────────

    def _is_torrent_private(self, thash: str, tname: str, trackers: List[Dict[str, Any]]) -> bool:
        """Determine if a torrent is private using multiple methods:
        1. torrent properties is_private flag (authoritative)
        2. Fallback: URL-based heuristics on tracker URLs
        """
        # Method 1: Check the is_private flag via torrent properties
        try:
            props = self._qb.get_torrent_properties(thash)
            if QBittorrentClient.is_private_torrent(props):
                log.info('🔍 Private tracker check for "%s" → private (is_private=true)', tname)
                return True
        except QBittorrentError as exc:
            log.warning('⚠️  Could not fetch properties for "%s": %s — falling back to URL check', tname, exc)

        # Method 2: Check tracker URLs for passkey/authkey patterns
        for tracker in trackers:
            if QBittorrentClient.has_private_tracker_url(tracker):
                log.info('🔍 Private tracker check for "%s" → private (passkey detected in URL)', tname)
                return True

        log.info('🔍 Private tracker check for "%s" → public', tname)
        return False

    # ── new torrent processing ────────────────────────────────────────

    def _process_new_torrent(self, torrent: Dict[str, Any]) -> None:
        """Handle a freshly detected torrent."""
        thash = torrent["hash"]
        tname = torrent.get("name", "unknown")
        log.info('🆕 New torrent detected: "%s" [hash: %s]', tname, thash[:8])

        # Skip if we already have records for this torrent (e.g. re-added)
        if self._db.has_records(thash):
            log.info('   ↳ Already processed in database — skipping')
            return

        # Fetch tracker list
        try:
            trackers = self._qb.get_torrent_trackers(thash)
        except QBittorrentError as exc:
            log.error('❌ Failed to fetch trackers for "%s": %s', tname, exc)
            return

        # Keep only real tracker URLs (exclude DHT, PeX, LSD)
        real_trackers = QBittorrentClient.get_real_trackers(trackers)

        if not real_trackers:
            log.info('🔍 Private tracker check for "%s" → public (no tracker URLs)', tname)
            return

        # Check if the torrent is private
        if not self._is_torrent_private(thash, tname, real_trackers):
            return

        # ── PRIVATE TORRENT: save ALL trackers to DB, then strip ALL ──
        log.info('   ↳ %d tracker URL(s) to save and strip', len(real_trackers))

        urls_to_strip = []
        for tracker in real_trackers:
            url = tracker["url"]
            tier = tracker.get("tier", 0)
            masked = QBittorrentClient.mask_url(url)

            saved = self._db.save_tracker(thash, tname, url, tier)
            if saved:
                log.info("💾 Tracker URL saved: %s → ✅ OK", masked)
            else:
                log.info("💾 Tracker URL already in database: %s → ✅ OK (duplicate)", masked)
            urls_to_strip.append(url)

        # Strip all trackers at once
        if urls_to_strip:
            try:
                self._qb.remove_trackers(thash, urls_to_strip)
                log.info('✂️  Tracker(s) stripped from "%s" → ✅ OK (%d removed)', tname, len(urls_to_strip))
            except QBittorrentError as exc:
                log.error('✂️  Failed to strip tracker(s) from "%s": %s → ❌ ERROR', tname, exc)

    # ── tracker reinjection ───────────────────────────────────────────

    def _reinject_trackers(self, thash: str, tname: str) -> None:
        """Reinject all saved trackers for a torrent that has entered seeding state."""
        pending = self._db.get_pending(thash)
        if not pending:
            return

        log.info('✅ Torrent completed, seeding state detected: "%s"', tname)

        for tracker_url, tier in pending:
            masked = QBittorrentClient.mask_url(tracker_url)
            try:
                self._qb.add_trackers(thash, [tracker_url])
                self._db.mark_reinjected(thash, tracker_url)
                log.info("💉 Tracker URL reinjected: %s → ✅ OK", masked)
            except QBittorrentError as exc:
                log.error("💉 Failed to reinject tracker %s → ❌ ERROR (%s)", masked, exc)

    # ── main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Main entry point — connect, recover, poll forever."""
        log.info("=" * 60)
        log.info("🚀 Vault-Tracker v1.1.0 starting")
        log.info("   Config: %s", self._cfg)
        log.info("=" * 60)

        self._connect()
        self._recover_pending()

        while self._running:
            try:
                torrents = self._qb.get_torrents()
            except QBittorrentError:
                log.warning(
                    "⚠️  qBittorrent unreachable → retrying in %ds",
                    self._cfg.RETRY_DELAY,
                )
                time.sleep(self._cfg.RETRY_DELAY)
                self._connect()
                continue

            log.info("🔄 Polling cycle — %d torrent(s) detected", len(torrents))

            for torrent in torrents:
                thash = torrent["hash"]
                tname = torrent.get("name", "unknown")
                state = torrent.get("state", "unknown")

                # New torrent detection
                if thash not in self._known_hashes:
                    self._known_hashes.add(thash)
                    self._process_new_torrent(torrent)

                # Log download progress (once per torrent)
                if state in DOWNLOADING_STATES and thash not in self._downloading_logged:
                    progress = torrent.get("progress", 0) * 100
                    log.info(
                        '⏳ Torrent download in progress: "%s" [%.1f%%] — state: %s',
                        tname,
                        progress,
                        state,
                    )
                    self._downloading_logged.add(thash)

                # Seeding → reinject
                if state in SEEDING_STATES:
                    self._downloading_logged.discard(thash)
                    if self._db.get_pending(thash):
                        self._reinject_trackers(thash, tname)

            time.sleep(self._cfg.POLL_INTERVAL)

        # Cleanup
        log.info("🛑 Vault-Tracker stopped.")
        self._db.close()
