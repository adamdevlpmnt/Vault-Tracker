"""Core Vault-Tracker service — real-time sync loop.

Logic (v3.0.1-dev):
    - Uses /api/v2/sync/maindata (rid-based delta sync) for real-time detection.
    - Initial scan: only processes torrents in DOWNLOAD states (ignores seeding).
    - New torrent detected → save tracker URLs + metadata to DB (.torrent exported later).
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

# All download-side states (torrent is in the download pipeline)
ALL_DOWNLOAD_STATES = frozenset({
    "downloading",
    "stalledDL",
    "forcedDL",
    "queuedDL",
    "metaDL",
    "allocating",
    "checkingDL",
    "pausedDL",
    "moving",
})

# Minimal sleep between sync calls (100ms)
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
        self._torrent_states: Dict[str, str] = {}   # last known state per hash
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
            "🔁 Container restart → %d torrent(s) with pending completions",
            len(by_hash),
        )

        try:
            torrents = self._qb.get_torrents()
        except QBittorrentError:
            log.warning("⚠️  Cannot fetch torrent list for recovery — will retry in main loop")
            return

        torrent_map = {t["hash"]: t for t in torrents}
        for thash, items in by_hash.items():
            self._known_hashes.add(thash)
            self._saved_hashes.add(thash)

            t = torrent_map.get(thash)
            if not t:
                log.info("    ├── %s [%s] — torrent no longer in qBittorrent", items[0]["torrent_name"], thash[:8])
                continue

            state = t.get("state", "unknown")
            log.info("    ├── %s [%s] [state: %s]", items[0]["torrent_name"], thash[:8], state)

            # Check if trackers are already stripped
            try:
                current_trackers = self._qb.get_torrent_trackers(thash)
                real = QBittorrentClient.get_real_trackers(current_trackers)
                if not real:
                    self._stripped_hashes.add(thash)
            except QBittorrentError:
                pass

            if state in SEEDING_STATES and thash in self._stripped_hashes:
                self._complete_torrent(thash, t.get("name", "?"))
            elif state in ACTIVE_DOWNLOAD_STATES and thash not in self._stripped_hashes:
                pending_trackers = self._db.get_pending(thash)
                if pending_trackers:
                    urls = [url for url, _ in pending_trackers]
                    try:
                        self._qb.remove_trackers(thash, urls)
                        self._stripped_hashes.add(thash)
                        log.info('    ↳ Recovery: stripped %d tracker(s)', len(urls))
                    except QBittorrentError as exc:
                        log.error('    ↳ Recovery: failed to strip trackers: %s', exc)

    # ── initial scan (first sync, rid=0) ──────────────────────────────

    def _initial_scan(self, torrents: Dict[str, Any]) -> None:
        """Process torrents from the first sync/maindata snapshot (rid=0).

        IMPORTANT: Only processes torrents in download states.
        Torrents already seeding are registered as known but NOT processed
        (they already have their tracker URLs and are fine).
        """
        if not torrents:
            log.info("🔍 Initial scan — no torrents in qBittorrent")
            return

        # Count states for summary
        download_count = 0
        seeding_count = 0
        other_count = 0

        for thash, tinfo in torrents.items():
            state = tinfo.get("state", "unknown")
            self._known_hashes.add(thash)
            self._torrent_states[thash] = state

            if state in ALL_DOWNLOAD_STATES:
                download_count += 1
            elif state in SEEDING_STATES:
                seeding_count += 1
            else:
                other_count += 1

        log.info(
            "🔍 Initial scan — %d torrent(s): %d downloading, %d seeding, %d other",
            len(torrents), download_count, seeding_count, other_count,
        )

        if download_count == 0:
            log.info("🔍 No torrents in download states — nothing to process")
            return

        # Only process torrents in download states
        processed = 0
        for thash, tinfo in torrents.items():
            state = tinfo.get("state", "unknown")

            if state not in ALL_DOWNLOAD_STATES:
                continue

            # Skip if already in DB (already handled by recovery)
            if self._db.has_records(thash):
                self._saved_hashes.add(thash)
                # Check if needs stripping
                if state in ACTIVE_DOWNLOAD_STATES and thash not in self._stripped_hashes:
                    torrent_info = self._fetch_torrent_info(thash)
                    if torrent_info:
                        self._strip_trackers(torrent_info)
                continue

            # Fetch full info and save
            torrent_info = self._fetch_torrent_info(thash)
            if not torrent_info:
                continue

            saved = self._save_trackers(torrent_info)
            if saved and state in ACTIVE_DOWNLOAD_STATES:
                self._strip_trackers(torrent_info)

            processed += 1

        log.info("🔍 Initial scan complete — processed %d downloading torrent(s)", processed)

    # ── save trackers + metadata to DB ────────────────────────────────

    def _save_trackers(self, torrent: Dict[str, Any]) -> bool:
        """Save all tracker URLs and metadata for a torrent to the database.
        Returns True if trackers were saved, False otherwise."""
        thash = torrent["hash"]
        tname = torrent.get("name", "unknown")
        size = torrent.get("size", 0) or torrent.get("total_size", 0)
        state = torrent.get("state", "unknown")

        # Size filter (0 = no filter)
        if self._cfg.MIN_SIZE_BYTES > 0 and size < self._cfg.MIN_SIZE_BYTES:
            log.info(
                '⏭️  Skipped: "%s" [%s] — size %s < %s',
                tname, thash[:8], _format_size(size), self._cfg.min_size_display,
            )
            return False

        log.info(
            '🆕 New torrent: "%s" [%s] [size: %s] [state: %s]',
            tname, thash[:8], _format_size(size), state,
        )

        # Already in DB?
        if self._db.has_records(thash):
            log.info('   ↳ Already in database — skipping')
            self._saved_hashes.add(thash)
            return True

        # Fetch trackers
        try:
            all_trackers = self._qb.get_torrent_trackers(thash)
        except QBittorrentError as exc:
            log.error('   ↳ Failed to fetch trackers: %s', exc)
            return False

        real_trackers = QBittorrentClient.get_real_trackers(all_trackers)

        if not real_trackers:
            log.info('   ↳ No tracker URLs found — skipping')
            return False

        # Export .torrent file (non-critical: save without it if export fails)
        torrent_file: Optional[bytes] = None
        try:
            torrent_file = self._qb.export_torrent(thash)
            log.info('   ↳ .torrent exported (%d bytes)', len(torrent_file))
        except (QBittorrentError, Exception) as exc:
            log.warning('   ↳ .torrent export failed: %s (will retry at completion)', exc)

        # Metadata
        save_path = torrent.get("save_path", "")
        content_path = torrent.get("content_path", "")
        category = torrent.get("category", "")
        tags = torrent.get("tags", "")

        # Save each tracker URL
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
                log.info("   💾 Saved: %s", masked)
            else:
                log.info("   💾 Already saved: %s", masked)

        log.info('   ↳ %d tracker(s) saved — waiting for active download to strip', len(real_trackers))
        self._saved_hashes.add(thash)
        return True

    # ── strip trackers (when active download starts) ──────────────────

    def _strip_trackers(self, torrent: Dict[str, Any]) -> None:
        """Strip all saved tracker URLs from a torrent in active download."""
        thash = torrent["hash"]
        tname = torrent.get("name", "unknown")

        pending = self._db.get_pending(thash)
        if not pending:
            return

        urls_to_strip = [url for url, _tier in pending]
        try:
            self._qb.remove_trackers(thash, urls_to_strip)
            self._stripped_hashes.add(thash)
            log.info('✂️  Stripped %d tracker(s) from "%s" → downloading without tracker', len(urls_to_strip), tname)
        except QBittorrentError as exc:
            log.error('✂️  Failed to strip trackers from "%s": %s', tname, exc)

    # ── completion workflow (seeding → delete → re-add .torrent) ──────

    def _complete_torrent(self, thash: str, tname: str) -> None:
        """Run the v3 completion workflow for a seeding torrent."""
        metadata = self._db.get_torrent_metadata(thash)
        if not metadata:
            log.warning('⚠️  No metadata in DB for "%s" [%s] — cannot complete', tname, thash[:8])
            return

        log.info('✅ Torrent completed: "%s" [%s] — starting re-add workflow', tname, thash[:8])

        # Step 1: Ensure we have the .torrent file
        torrent_file = metadata.get("torrent_file")
        if not torrent_file:
            try:
                torrent_file = self._qb.export_torrent(thash)
                self._db.update_torrent_file(thash, torrent_file)
                log.info('   ↳ .torrent exported (%d bytes)', len(torrent_file))
            except QBittorrentError as exc:
                log.error('   ↳ Failed to export .torrent: %s — cannot complete', exc)
                return

        save_path = metadata.get("save_path", "")
        category = metadata.get("category", "")
        tags = metadata.get("tags", "")

        # Step 2: Delete torrent (keep files)
        try:
            self._qb.delete_torrent(thash, delete_files=False)
            log.info('   🗑️  Deleted from qBittorrent (files kept)')
        except QBittorrentError as exc:
            log.error('   ↳ Failed to delete torrent: %s — cannot complete', exc)
            return

        # Step 3: Re-add .torrent with original metadata
        try:
            self._qb.add_torrent_file(
                torrent_bytes=torrent_file,
                save_path=save_path,
                category=category,
                tags=tags,
            )
            log.info('   📥 Re-added .torrent (save_path: %s, category: %s)', save_path, category or "none")
        except QBittorrentError as exc:
            log.error('   ↳ Failed to re-add .torrent: %s', exc)
            return

        # Step 4: Mark completed
        self._db.mark_completed(thash)
        self._completed_hashes.add(thash)
        log.info('   🎉 Done — torrent will check files and resume seeding with tracker')

    # ── main loop (real-time sync) ────────────────────────────────────

    def run(self) -> None:
        """Main entry point using real-time sync/maindata."""
        log.info("=" * 60)
        log.info("🚀 Vault-Tracker v3.0.1-dev starting")
        log.info("   qb_url:    %s", self._cfg.qb_url)
        log.info("   min_size:  %s", self._cfg.min_size_display)
        log.info("   log_level: %s", self._cfg.LOG_LEVEL)
        log.info("   db_path:   %s", self._cfg.DB_PATH)
        log.info("=" * 60)

        self._connect()
        self._recover_pending()

        # First sync: full snapshot
        log.info("⏳ Initial sync with qBittorrent…")
        try:
            data = self._qb.sync_maindata(rid=0)
            self._rid = data.get("rid", 0)
            initial_torrents = data.get("torrents", {})
            self._initial_scan(initial_torrents)
        except QBittorrentError as exc:
            log.warning("⚠️  Initial sync failed: %s — will retry in main loop", exc)

        log.info("👁️  Real-time monitoring active — watching for changes…")

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

            # Clean up removed torrents
            for rhash in removed:
                self._known_hashes.discard(rhash)
                self._saved_hashes.discard(rhash)
                self._stripped_hashes.discard(rhash)
                self._completed_hashes.discard(rhash)
                self._torrent_states.pop(rhash, None)
                log.info("🗑️  Torrent removed [%s]", rhash[:8])

            # Process delta updates
            for thash, tinfo in torrents_delta.items():
                new_state = tinfo.get("state")
                if new_state:
                    old_state = self._torrent_states.get(thash)
                    self._torrent_states[thash] = new_state
                    if old_state and old_state != new_state:
                        log.debug("   [%s] state: %s → %s", thash[:8], old_state, new_state)

                current_state = self._torrent_states.get(thash, "unknown")

                # ── New torrent ──
                if thash not in self._known_hashes:
                    self._known_hashes.add(thash)
                    tname = tinfo.get("name", thash[:8])
                    log.info("🔔 New torrent: \"%s\" [%s] [state: %s]", tname, thash[:8], current_state)

                    # Save trackers immediately (fetch full info from API)
                    torrent_info = self._fetch_torrent_info(thash)
                    if torrent_info:
                        saved = self._save_trackers(torrent_info)
                        if saved and current_state in ACTIVE_DOWNLOAD_STATES:
                            self._strip_trackers(torrent_info)
                    continue

                # ── State change on known torrent ──
                if not new_state:
                    continue  # no state change, just progress/speed update

                # Skip already completed
                if thash in self._completed_hashes:
                    continue

                # Saved but not stripped → check for active download
                if thash in self._saved_hashes and thash not in self._stripped_hashes:
                    if new_state in ACTIVE_DOWNLOAD_STATES:
                        log.info('🔔 Download started [%s] [state: %s] → stripping tracker', thash[:8], new_state)
                        torrent_info = self._fetch_torrent_info(thash)
                        if torrent_info:
                            self._strip_trackers(torrent_info)
                    continue

                # Stripped, waiting for seeding → check for completion
                if thash in self._stripped_hashes and thash not in self._completed_hashes:
                    if new_state in SEEDING_STATES:
                        tname = tinfo.get("name") or "?"
                        if tname == "?":
                            ti = self._fetch_torrent_info(thash)
                            if ti:
                                tname = ti.get("name", "?")
                        self._complete_torrent(thash, tname)

            time.sleep(_SYNC_SLEEP)

        log.info("🛑 Vault-Tracker stopped.")
        self._db.close()

    def _fetch_torrent_info(self, thash: str) -> Optional[Dict[str, Any]]:
        """Fetch full torrent info for a specific hash."""
        try:
            return self._qb.get_torrent_info(thash)
        except QBittorrentError as exc:
            log.warning("⚠️  Failed to fetch info for [%s]: %s", thash[:8], exc)
        return None
