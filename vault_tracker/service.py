"""Core Vault-Tracker service — real-time sync loop.

Logic (v3.0.0-dev):
    - Uses /api/v2/sync/maindata (rid-based delta sync) for real-time detection.
    - New torrent detected → save tracker URLs + metadata + .torrent file to DB.
    - Torrent enters "downloading" or "forcedDL" → strip tracker URLs instantly.
    - Torrent enters seeding state → delete torrent (keep files) → re-add .torrent
      with original save_path/category/tags → qBittorrent checks files → seeds
      with tracker intact.
    - On restart → recover pending torrents from DB.
"""

from __future__ import annotations

import signal
import sys
import time
from typing import Any, Dict, Optional, Set

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

# States where download is actively running (data is flowing)
ACTIVE_DOWNLOAD_STATES = frozenset({
    "downloading",
    "forcedDL",
})

# All download-side states (for initial detection only)
ALL_DOWNLOAD_STATES = frozenset({
    "downloading",
    "stalledDL",
    "forcedDL",
    "queuedDL",
    "metaDL",
    "allocating",
    "checkingDL",
})

# Minimal sleep between sync calls to avoid CPU spin (100ms)
_SYNC_SLEEP: float = 0.1


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.2f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.1f} MB"
    return f"{size_bytes / 1024:.0f} KB"


class VaultService:
    """Main service orchestrator using real-time sync."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._db = TrackerDB(cfg.DB_PATH)
        self._qb = QBittorrentClient(cfg)
        self._known_hashes: Set[str] = set()       # all torrents we've seen
        self._saved_hashes: Set[str] = set()        # torrents whose trackers are saved in DB
        self._stripped_hashes: Set[str] = set()     # torrents whose trackers have been stripped
        self._completed_hashes: Set[str] = set()    # torrents that have been deleted and re-added
        self._rid: int = 0                          # sync/maindata request ID
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
        """On restart, recover pending torrents from DB and process them."""
        pending = self._db.get_all_pending()
        if not pending:
            log.info("🔁 Container restart → no pending completions in database")
            return

        by_hash: Dict[str, list] = {}
        for row in pending:
            by_hash.setdefault(row["torrent_hash"], []).append(row)

        log.info(
            "🔁 Container restart → resuming from database state, "
            "%d torrent(s) with pending completions:",
            len(by_hash),
        )
        for thash, items in by_hash.items():
            log.info("    ├── %s [hash: %s] — %d tracker(s)", items[0]["torrent_name"], thash[:8], len(items))

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
            if not t:
                continue

            state = t.get("state", "unknown")

            # Torrent is seeding → run completion workflow
            if state in SEEDING_STATES:
                self._complete_torrent(thash, t.get("name", "?"))
            # Torrent is actively downloading but trackers still present → strip them
            elif state in ACTIVE_DOWNLOAD_STATES:
                pending_trackers = self._db.get_pending(thash)
                if pending_trackers:
                    urls = [url for url, _ in pending_trackers]
                    try:
                        self._qb.remove_trackers(thash, urls)
                        log.info('✂️  Recovery: stripped %d tracker(s) from "%s"', len(urls), t.get("name", "?"))
                    except QBittorrentError as exc:
                        log.error('✂️  Recovery: failed to strip trackers from "%s": %s', t.get("name", "?"), exc)

    # ── save trackers + metadata to DB ────────────────────────────────

    def _save_trackers(self, torrent: Dict[str, Any]) -> bool:
        """Save all tracker URLs and metadata for a torrent to the database.
        Returns True if trackers were saved, False otherwise."""
        thash = torrent["hash"]
        tname = torrent.get("name", "unknown")
        size = torrent.get("size", 0) or torrent.get("total_size", 0)

        # Size filter (0 = no filter)
        if self._cfg.MIN_SIZE_BYTES > 0 and size < self._cfg.MIN_SIZE_BYTES:
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

        # Export .torrent file
        torrent_file: Optional[bytes] = None
        try:
            torrent_file = self._qb.export_torrent(thash)
            log.info('   ↳ .torrent file exported (%d bytes)', len(torrent_file))
        except QBittorrentError as exc:
            log.warning('   ↳ Failed to export .torrent file: %s (will retry later)', exc)

        # Extract metadata from torrent info
        save_path = torrent.get("save_path", "")
        content_path = torrent.get("content_path", "")
        category = torrent.get("category", "")
        tags = torrent.get("tags", "")

        # Save each tracker URL with metadata
        for tracker in real_trackers:
            url = tracker["url"]
            tier = tracker.get("tier", 0)
            masked = QBittorrentClient.mask_url(url)

            saved = self._db.save_tracker(
                thash, tname, url, tier,
                save_path=save_path,
                content_path=content_path,
                category=category,
                tags=tags,
                torrent_file=torrent_file,
            )
            if saved:
                log.info("💾 Tracker URL saved: %s → ✅ OK", masked)
            else:
                log.info("💾 Tracker URL already in database: %s → ✅ OK (duplicate)", masked)

        log.info('   ↳ %d tracker URL(s) saved — waiting for active download to strip', len(real_trackers))
        self._saved_hashes.add(thash)
        return True

    # ── strip trackers (when active download starts) ──────────────────

    def _strip_trackers(self, torrent: Dict[str, Any]) -> None:
        """Strip all saved tracker URLs from a torrent that has entered active download."""
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

    # ── completion workflow (seeding → delete → re-add .torrent) ──────

    def _complete_torrent(self, thash: str, tname: str) -> None:
        """Run the v3 completion workflow for a seeding torrent.

        1. Export .torrent if not already stored in DB.
        2. Delete torrent from qBittorrent (keep files).
        3. Re-add .torrent with original save_path/category/tags.
        4. Mark as completed in DB.
        """
        metadata = self._db.get_torrent_metadata(thash)
        if not metadata:
            return

        log.info('✅ Torrent completed, seeding state detected: "%s"', tname)

        # Step 1: Ensure we have the .torrent file
        torrent_file = metadata.get("torrent_file")
        if not torrent_file:
            try:
                torrent_file = self._qb.export_torrent(thash)
                self._db.update_torrent_file(thash, torrent_file)
                log.info('   ↳ .torrent file exported (%d bytes)', len(torrent_file))
            except QBittorrentError as exc:
                log.error('❌ Failed to export .torrent for "%s": %s — cannot complete', tname, exc)
                return

        save_path = metadata.get("save_path", "")
        category = metadata.get("category", "")
        tags = metadata.get("tags", "")

        # Step 2: Delete torrent (keep files)
        try:
            self._qb.delete_torrent(thash, delete_files=False)
            log.info('🗑️  Torrent deleted (files kept): "%s"', tname)
        except QBittorrentError as exc:
            log.error('❌ Failed to delete torrent "%s": %s — cannot complete', tname, exc)
            return

        # Step 3: Re-add .torrent with original metadata
        try:
            self._qb.add_torrent_file(
                torrent_bytes=torrent_file,
                save_path=save_path,
                category=category,
                tags=tags,
            )
            log.info('📥 .torrent re-added with original metadata: "%s" → ✅ OK', tname)
        except QBittorrentError as exc:
            log.error('❌ Failed to re-add .torrent for "%s": %s', tname, exc)
            return

        # Step 4: Mark completed in DB
        self._db.mark_completed(thash)
        self._completed_hashes.add(thash)
        log.info('🎉 Completion workflow finished for "%s" [hash: %s]', tname, thash[:8])

    # ── main loop (real-time sync) ────────────────────────────────────

    def run(self) -> None:
        """Main entry point using real-time sync/maindata."""
        log.info("=" * 60)
        log.info("🚀 Vault-Tracker v3.0.0-dev starting")
        log.info("   qb_url:    %s", self._cfg.qb_url)
        log.info("   min_size:  %s", self._cfg.min_size_display)
        log.info("   log_level: %s", self._cfg.LOG_LEVEL)
        log.info("   db_path:   %s", self._cfg.DB_PATH)
        log.info("=" * 60)

        self._connect()
        self._recover_pending()

        while self._running:
            try:
                data = self._qb.sync_maindata(rid=self._rid)
            except QBittorrentError:
                log.warning(
                    "⚠️  qBittorrent unreachable → retrying in %ds",
                    self._cfg.RETRY_DELAY,
                )
                time.sleep(self._cfg.RETRY_DELAY)
                self._connect()
                self._rid = 0
                continue

            self._rid = data.get("rid", self._rid)
            torrents_delta = data.get("torrents", {})
            removed = data.get("torrents_removed", [])

            # Clean up removed torrents from tracking sets
            for rhash in removed:
                self._known_hashes.discard(rhash)
                self._saved_hashes.discard(rhash)
                self._stripped_hashes.discard(rhash)
                self._completed_hashes.discard(rhash)

            # Process torrent updates
            for thash, tinfo in torrents_delta.items():
                state = tinfo.get("state")

                # New torrent detection
                if thash not in self._known_hashes:
                    self._known_hashes.add(thash)
                    # Need full torrent info for metadata — fetch it
                    if state and state in ALL_DOWNLOAD_STATES:
                        torrent_info = self._fetch_torrent_info(thash)
                        if torrent_info:
                            self._save_trackers(torrent_info)

                # Active download → strip trackers
                if (
                    state in ACTIVE_DOWNLOAD_STATES
                    and thash in self._saved_hashes
                    and thash not in self._stripped_hashes
                ):
                    torrent_info = self._fetch_torrent_info(thash)
                    if torrent_info:
                        self._strip_trackers(torrent_info)

                # Seeding → completion workflow
                if (
                    state in SEEDING_STATES
                    and thash in self._stripped_hashes
                    and thash not in self._completed_hashes
                    and self._db.get_pending(thash)
                ):
                    tname = tinfo.get("name", "?")
                    self._complete_torrent(thash, tname)

            time.sleep(_SYNC_SLEEP)

        log.info("🛑 Vault-Tracker stopped.")
        self._db.close()

    def _fetch_torrent_info(self, thash: str) -> Optional[Dict[str, Any]]:
        """Fetch full torrent info for a specific hash."""
        try:
            return self._qb.get_torrent_info(thash)
        except QBittorrentError as exc:
            log.warning("⚠️  Failed to fetch torrent info for %s: %s", thash[:8], exc)
        return None
