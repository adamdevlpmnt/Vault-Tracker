"""SQLite persistence layer for saved tracker URLs and torrent metadata.

Schema (v3):
    trackers
    ├── id            INTEGER PRIMARY KEY
    ├── torrent_hash  TEXT    (info-hash of the torrent)
    ├── torrent_name  TEXT
    ├── tracker_url   TEXT    (full URL including passkey)
    ├── tier          INTEGER (tracker tier in qBittorrent)
    ├── save_path     TEXT    (torrent save path)
    ├── content_path  TEXT    (torrent content path)
    ├── category      TEXT    (torrent category)
    ├── tags          TEXT    (torrent tags)
    ├── torrent_file  BLOB   (exported .torrent file binary)
    ├── stripped_at   TEXT    (ISO timestamp when the tracker was removed)
    ├── completed_at  TEXT    (ISO timestamp when torrent was re-added; NULL while pending)
    └── UNIQUE(torrent_hash, tracker_url)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
        save_path     TEXT,
        content_path  TEXT,
        category      TEXT    DEFAULT '',
        tags          TEXT    DEFAULT '',
        torrent_file  BLOB,
        stripped_at   TEXT    NOT NULL,
        completed_at  TEXT,
        UNIQUE(torrent_hash, tracker_url)
    );
    CREATE INDEX IF NOT EXISTS idx_hash ON trackers(torrent_hash);
    CREATE INDEX IF NOT EXISTS idx_pending ON trackers(completed_at) WHERE completed_at IS NULL;
    """

    MIGRATION_COLUMNS = {
        "save_path": "TEXT",
        "content_path": "TEXT",
        "category": "TEXT DEFAULT ''",
        "tags": "TEXT DEFAULT ''",
        "torrent_file": "BLOB",
    }

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(self.DDL)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Apply schema migrations from v2 to v3."""
        cursor = self._conn.execute("PRAGMA table_info(trackers)")
        existing = {row[1] for row in cursor.fetchall()}

        # Rename reinjected_at → completed_at if needed
        if "reinjected_at" in existing and "completed_at" not in existing:
            self._conn.execute(
                "ALTER TABLE trackers RENAME COLUMN reinjected_at TO completed_at"
            )
            log.info("🔄 DB migration: renamed reinjected_at → completed_at")

        # Add new columns if missing
        for col, col_type in self.MIGRATION_COLUMNS.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE trackers ADD COLUMN {col} {col_type}")
                log.info("🔄 DB migration: added column %s", col)

        self._conn.commit()

    # ── writes ────────────────────────────────────────────────────────

    def save_tracker(
        self,
        torrent_hash: str,
        torrent_name: str,
        tracker_url: str,
        tier: int = 0,
        save_path: str = "",
        content_path: str = "",
        category: str = "",
        tags: str = "",
        torrent_file: Optional[bytes] = None,
    ) -> bool:
        """Store a tracker URL with metadata. Returns True on success, False if duplicate."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                """INSERT INTO trackers
                   (torrent_hash, torrent_name, tracker_url, tier,
                    save_path, content_path, category, tags, torrent_file,
                    stripped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    torrent_hash, torrent_name, tracker_url, tier,
                    save_path, content_path, category, tags, torrent_file,
                    now,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # duplicate – already saved

    def mark_completed(self, torrent_hash: str) -> None:
        """Mark all pending trackers for a torrent as completed (re-added)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE trackers SET completed_at = ?
               WHERE torrent_hash = ? AND completed_at IS NULL""",
            (now, torrent_hash),
        )
        self._conn.commit()

    def update_torrent_file(self, torrent_hash: str, torrent_file: bytes) -> None:
        """Store the exported .torrent file for a torrent."""
        self._conn.execute(
            """UPDATE trackers SET torrent_file = ?
               WHERE torrent_hash = ? AND completed_at IS NULL""",
            (torrent_file, torrent_hash),
        )
        self._conn.commit()

    # ── reads ─────────────────────────────────────────────────────────

    def get_pending(self, torrent_hash: str) -> List[Tuple[str, int]]:
        """Return [(tracker_url, tier), ...] not yet completed for a torrent."""
        cur = self._conn.execute(
            """SELECT tracker_url, tier FROM trackers
               WHERE torrent_hash = ? AND completed_at IS NULL
               ORDER BY tier""",
            (torrent_hash,),
        )
        return cur.fetchall()

    def get_all_pending(self) -> List[Dict[str, Any]]:
        """Return all pending completions across every torrent.

        Returns list of dicts with keys:
            torrent_hash, torrent_name, tracker_url, tier,
            save_path, content_path, category, tags, torrent_file
        """
        cur = self._conn.execute(
            """SELECT torrent_hash, torrent_name, tracker_url, tier,
                      save_path, content_path, category, tags, torrent_file
               FROM trackers WHERE completed_at IS NULL
               ORDER BY torrent_hash, tier"""
        )
        columns = [
            "torrent_hash", "torrent_name", "tracker_url", "tier",
            "save_path", "content_path", "category", "tags", "torrent_file",
        ]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def get_torrent_metadata(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """Return metadata for a torrent (first pending row) or None."""
        cur = self._conn.execute(
            """SELECT save_path, content_path, category, tags, torrent_file
               FROM trackers
               WHERE torrent_hash = ? AND completed_at IS NULL
               LIMIT 1""",
            (torrent_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "save_path": row[0],
            "content_path": row[1],
            "category": row[2],
            "tags": row[3],
            "torrent_file": row[4],
        }

    def has_records(self, torrent_hash: str) -> bool:
        """Return True if we already processed this torrent."""
        cur = self._conn.execute(
            "SELECT 1 FROM trackers WHERE torrent_hash = ? LIMIT 1",
            (torrent_hash,),
        )
        return cur.fetchone() is not None

    def close(self) -> None:
        self._conn.close()
