"""Core Vault-Tracker service — the main polling loop.

Logic (v2.0.1-beta):
    - New torrent detected (any state) → register it, save tracker URLs to DB.
    - Torrent enters "downloading" state → strip tracker URLs immediately.
    - Torrent enters seeding state       → reinject saved tracker URLs from DB.
    - On restart                         → check DB for pending reinjections.

Conditions:
    - Only torrents larger than MIN_SIZE_BYTES are processed (default 4 GB).
    - Tracker stripping waits for actual "downloading" state (not stalled/queued).
"""

from __future__ import annotations

import signal
import sys
import time
from typing import Any, Dict, Set

from vault_tracker.config import Config
from vault_tracker.database import TrackerDB
from vault_tracker.logger import get_logger
from vault_tracker.qbittorrent import QBittorrentClient, QBittorrentError

log = get_logger()

# qBittorrent states: seeding (download complete)
SEEDING_STATES = frozenset({
    "uploading",
    "stalledUP",
    "forcedUP",
    "queuedUP",
    "checkingUP",
})

# States where download has actually started (data is flowing)
ACTIVE_DOWNLOAD_STATES = frozenset({
    "downloading",
    "forcedDL",
})

# All download-side states (including stalled/queued — for detection only)
ALL_DOWNLOAD_STATES = frozenset({
    "downloading",
    "stalledDL",
    "forcedDL",
    "queuedDL",
    "metaDL",
    "allocating",
    "checkingDL",
})


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.2f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.1f} MB"
    return f"{size_bytes / 1024:.0f} KB"


class VaultService:
    """Main service orchestrator."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._db = TrackerDB(cfg.DB_PATH)
        self._qb = QBittorrentClient(cfg)
        self._known_hashes: Set[str] = set()       # all torrents we've seen
        self._saved_hashes: Set[str] = set()        # torrents whose trackers are saved in DB
        self._stripped_hashes: Set[str] = set()     # torrents whose trackers have been stripped
        self._last_torrent_count: int = -1          # for log dedup
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _: Any) -> None:
        log.info("🛑 Received signal %s — shutting down gracefully…", signum)
        self._running = False

    # ── connection ────────────────────────────────────────────────────

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
                    log.error("⚠️  qBittorrent unreachable after %d attempts — giving up", attempt)
                    sys.exit(1)
                log.warning(
                    "⚠️  qBittorrent unreachable → retrying in %ds (attempt %d)",
                    self._cfg.RETRY_DELAY, attempt,
                )
                time.sleep(self._cfg.RETRY_DELAY)

    # ── startup recovery ──────────────────────────────────────────────

    def _recover_pending(self) -> None:
        """On restart, check DB for pending reinjections and process
        any whose torrents are already seeding."""
        pending = self._db.get_all_pending()
        if not pending:
            log.info("🔁 Container restart → no pending reinjections in database")
            return

        by_hash: Dict[str, list] = {}
        for thash, tname, turl, tier in pending:
            by_hash.setdefault(thash, []).append((tname, turl, tier))

        log.info(
            "🔁 Container restart → resuming from database state, "
            "%d torrent(s) with pending reinjections:",
            len(by_hash),
        )
        for thash, items in by_hash.items():
            log.info("    ├── %s [hash: %s] — %d tracker(s)", items[0][0], thash[:8], len(items))

        try:
            torrents = self._qb.get_torrents()
        except QBittorrentError:
            log.warning("⚠️  Cannot fetch torrent list for recovery — will retry in main loop")
            return

        torrent_map = {t["hash"]: t for t in torrents}
        for thash, items in by_hash.items():
            self._known_hashes.add(thash)
            self._saved_hashes.add(thash)
            self._stripped_hashes.add(thash)
            t = torrent_map.get(thash)
            if t and t.get("state") in SEEDING_STATES:
                self._reinject_trackers(thash, t.get("name", "?"))

    # ── save trackers to DB (on detection) ────────────────────────────

    def _save_trackers(self, torrent: Dict[str, Any]) -> bool:
        """Save all tracker URLs for a torrent to the database.
        Returns True if trackers were saved, False otherwise."""
        thash = torrent["hash"]
        tname = torrent.get("name", "unknown")
        size = torrent.get("size", 0) or torrent.get("total_size", 0)

        # Size filter
        if size < self._cfg.MIN_SIZE_BYTES:
            log.info(
                '🔍 Torrent "%s" [%s] — skipped (size %s < %s)',
                tname, thash[:8], _format_size(size), self._cfg.min_size_display,
            )
            return False

        log.info(
            '🆕 New torrent detected: "%s" [hash: %s] [size: %s]',
            tname, thash[:8], _format_size(size),
        )

        # Already in DB?
        if self._db.has_records(thash):
            log.info('   ↳ Already processed in database — skipping')
            self._saved_hashes.add(thash)
            return True

        # Fetch trackers
        try:
            all_trackers = self._qb.get_torrent_trackers(thash)
        except QBittorrentError as exc:
            log.error('❌ Failed to fetch trackers for "%s": %s', tname, exc)
            return False

        real_trackers = QBittorrentClient.get_real_trackers(all_trackers)

        if not real_trackers:
            log.info('   ↳ No tracker URLs found — nothing to do')
            return False

        # Save each tracker URL
        for tracker in real_trackers:
            url = tracker["url"]
            tier = tracker.get("tier", 0)
            masked = QBittorrentClient.mask_url(url)

            saved = self._db.save_tracker(thash, tname, url, tier)
            if saved:
                log.info("💾 Tracker URL saved: %s → ✅ OK", masked)
            else:
                log.info("💾 Tracker URL already in database: %s → ✅ OK (duplicate)", masked)

        log.info('   ↳ %d tracker URL(s) saved — waiting for active download to strip', len(real_trackers))
        self._saved_hashes.add(thash)
        return True

    # ── strip trackers (when download starts) ─────────────────────────

    def _strip_trackers(self, torrent: Dict[str, Any]) -> None:
        """Strip all saved tracker URLs from a torrent that has started downloading."""
        thash = torrent["hash"]
        tname = torrent.get("name", "unknown")

        pending = self._db.get_pending(thash)
        if not pending:
            return

        urls_to_strip = [url for url, _tier in pending]
        try:
            self._qb.remove_trackers(thash, urls_to_strip)
            self._stripped_hashes.add(thash)
            log.info('✂️  Tracker(s) stripped from "%s" → ✅ OK (%d removed)', tname, len(urls_to_strip))
        except QBittorrentError as exc:
            log.error('✂️  Failed to strip tracker(s) from "%s": %s → ❌ ERROR', tname, exc)

    # ── reinject trackers (on seeding) ────────────────────────────────

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
        """Main entry point."""
        log.info("=" * 60)
        log.info("🚀 Vault-Tracker v2.0.1-beta starting")
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

            count = len(torrents)

            # Only log torrent count when it changes
            if count != self._last_torrent_count:
                log.info("🔄 Polling cycle — %d torrent(s) detected", count)
                self._last_torrent_count = count

            for torrent in torrents:
                thash = torrent["hash"]
                tname = torrent.get("name", "unknown")
                state = torrent.get("state", "unknown")

                # ── Step 1: New torrent → save trackers to DB ──
                if thash not in self._known_hashes:
                    self._known_hashes.add(thash)
                    if state in ALL_DOWNLOAD_STATES:
                        self._save_trackers(torrent)

                # ── Step 2: Active download → strip trackers ──
                if (
                    state in ACTIVE_DOWNLOAD_STATES
                    and thash in self._saved_hashes
                    and thash not in self._stripped_hashes
                ):
                    self._strip_trackers(torrent)

                # ── Step 3: Seeding → reinject trackers ──
                if state in SEEDING_STATES and self._db.get_pending(thash):
                    self._reinject_trackers(thash, tname)

            time.sleep(self._cfg.POLL_INTERVAL)

        log.info("🛑 Vault-Tracker stopped.")
        self._db.close()
