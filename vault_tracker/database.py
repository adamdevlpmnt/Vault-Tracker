"""SQLite persistence layer for saved tracker URLs.

Schema:
    trackers
    ├── id            INTEGER PRIMARY KEY
    ├── torrent_hash  TEXT    (info-hash of the torrent)
    ├── torrent_name  TEXT
    ├── tracker_url   TEXT    (full URL including passkey)
    ├── tier          INTEGER (tracker tier in qBittorrent)
    ├── stripped_at   TEXT    (ISO timestamp when the tracker was removed)
    ├── reinjected_at TEXT    (ISO timestamp when it was put back; NULL while pending)
    └── UNIQUE(torrent_hash, tracker_url)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from vault_tracker.logger import get_logger

log = get_logger()


class TrackerDB:
    """Thread-safe SQLite wrapper (single-writer, WAL mode)."""

    DDL = """
    CREATE TABLE IF NOT EXISTS trackers (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        torrent_hash  TEXT    NOT NULL,
        torrent_name  TEXT    NOT NULL,
        tracker_url   TEXT    NOT NULL,
        tier          INTEGER NOT NULL DEFAULT 0,
        stripped_at   TEXT    NOT NULL,
        reinjected_at TEXT,
        UNIQUE(torrent_hash, tracker_url)
    );
    CREATE INDEX IF NOT EXISTS idx_hash ON trackers(torrent_hash);
    CREATE INDEX IF NOT EXISTS idx_pending ON trackers(reinjected_at) WHERE reinjected_at IS NULL;
    """

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(self.DDL)
        self._conn.commit()

    # ── writes ────────────────────────────────────────────────────────

    def save_tracker(
        self,
        torrent_hash: str,
        torrent_name: str,
        tracker_url: str,
        tier: int = 0,
    ) -> bool:
        """Store a tracker URL. Returns True on success, False if duplicate."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                """INSERT INTO trackers
                   (torrent_hash, torrent_name, tracker_url, tier, stripped_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (torrent_hash, torrent_name, tracker_url, tier, now),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # duplicate – already saved

    def mark_reinjected(self, torrent_hash: str, tracker_url: str) -> None:
        """Mark a tracker as reinjected."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE trackers SET reinjected_at = ?
               WHERE torrent_hash = ? AND tracker_url = ? AND reinjected_at IS NULL""",
            (now, torrent_hash, tracker_url),
        )
        self._conn.commit()

    def mark_all_reinjected(self, torrent_hash: str) -> None:
        """Mark every pending tracker for a given torrent as reinjected."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE trackers SET reinjected_at = ?
               WHERE torrent_hash = ? AND reinjected_at IS NULL""",
            (now, torrent_hash),
        )
        self._conn.commit()

    # ── reads ─────────────────────────────────────────────────────────

    def get_pending(self, torrent_hash: str) -> List[Tuple[str, int]]:
        """Return [(tracker_url, tier), ...] not yet reinjected for a torrent."""
        cur = self._conn.execute(
            """SELECT tracker_url, tier FROM trackers
               WHERE torrent_hash = ? AND reinjected_at IS NULL
               ORDER BY tier""",
            (torrent_hash,),
        )
        return cur.fetchall()

    def get_all_pending(self) -> List[Tuple[str, str, str, int]]:
        """Return all pending reinjections across every torrent.

        Returns [(torrent_hash, torrent_name, tracker_url, tier), ...]
        """
        cur = self._conn.execute(
            """SELECT torrent_hash, torrent_name, tracker_url, tier
               FROM trackers WHERE reinjected_at IS NULL
               ORDER BY torrent_hash, tier"""
        )
        return cur.fetchall()

    def has_records(self, torrent_hash: str) -> bool:
        """Return True if we already processed this torrent (stripped or reinjected)."""
        cur = self._conn.execute(
            "SELECT 1 FROM trackers WHERE torrent_hash = ? LIMIT 1",
            (torrent_hash,),
        )
        return cur.fetchone() is not None

    def close(self) -> None:
        self._conn.close()
