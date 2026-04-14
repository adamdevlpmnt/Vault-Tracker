<div align="center">

<img src="https://img.shields.io/badge/version-3.0.2-blue?style=for-the-badge" alt="Version">
<img src="https://img.shields.io/badge/python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
<img src="https://img.shields.io/badge/docker-alpine-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker">
<img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="License">
<img src="https://img.shields.io/badge/qBittorrent-API%20v2-orange?style=for-the-badge" alt="qBittorrent">

# 🔐 Vault-Tracker

**Private tracker passkey protector for qBittorrent**

*Automatically strips, vaults, and reinjects your private tracker URLs — download anonymously, seed normally.*

</div>

---

## ✨ What is Vault-Tracker?

When you add a torrent from a private tracker, the `.torrent` file contains tracker URLs with **your unique passkey** embedded directly in them. During the download phase, your client announces to those trackers — consuming precious ratio and exposing your passkey to every peer in the swarm.

**Vault-Tracker** is a lightweight, zero-config Docker sidecar that runs alongside qBittorrent to solve this automatically:

| Phase | Without Vault-Tracker | With Vault-Tracker |
|---|---|---|
| **Download** | Announces to tracker, burns ratio | ✅ Silent — no tracker, no ratio loss |
| **Seeding** | Normal | ✅ Tracker reinjected — seeds normally |
| **Passkey exposure** | Exposed to peers during download | ✅ Never exposed during download |

---

## 🧠 How it works

```
                        ┌─────────────────────┐
                        │  qBittorrent WebUI   │
                        │   (existing server)  │
                        └──────────┬──────────┘
                                   │ API v2  (sync/maindata — real-time delta)
                                   ▼
┌──────────────────────────────────────────────────────────┐
│                      Vault-Tracker                       │
│                                                          │
│  ┌─────────────┐    ┌──────────────────┐                 │
│  │ Sync Loop   │───▶│  Event Dispatch  │                 │
│  │ (100ms tick)│    │                  │                 │
│  └─────────────┘    └────────┬─────────┘                 │
│                              │                           │
│              ┌───────────────┼───────────────┐           │
│              ▼               ▼               ▼           │
│       ┌────────────┐ ┌─────────────┐ ┌────────────────┐  │
│       │ New torrent│ │ DL started  │ │ Seeding detect │  │
│       │ → Save DB  │ │ → Strip URL │ │ → Delete+Re-add│  │
│       └────────────┘ └─────────────┘ └────────────────┘  │
│                                             │             │
│                              ┌──────────────┘             │
│                              ▼                            │
│                    ┌─────────────────┐                    │
│                    │   SQLite (WAL)  │                    │
│                    │  /data/*.db     │                    │
│                    └─────────────────┘                    │
└──────────────────────────────────────────────────────────┘
```

### Step-by-step flow

```
1.  New torrent detected (any state)
     ├─ Has private tracker? ──── No ──▶ Ignored
     └─ Yes ──▶ Export .torrent file
                Save tracker URL(s) + metadata to DB

2.  Torrent enters active download (state: downloading / forcedDL)
     └──▶ Strip saved tracker URLs from qBittorrent

3.  Download completes → torrent enters seeding state
     └──▶ Export current .torrent (if not already exported)
          Delete torrent from qBittorrent (files kept on disk)
          Re-add .torrent with original save_path / category / tags
          qBittorrent checks existing files → resumes seeding with tracker

4.  Mark as completed in DB
```

> **Key design choice (v3):** Instead of injecting tracker URLs back via API (which is flaky), Vault-Tracker does a **delete + re-add** with the original `.torrent` file. This guarantees the tracker is present from day one of seeding, with no API edge cases.

---

## 🚀 Quick start

### Prerequisites

- **Docker** + **Docker Compose** on your server
- A running **qBittorrent** instance with the **WebUI enabled**
- Network access from the Vault-Tracker container to qBittorrent's WebUI

### 1 — Clone

```bash
git clone https://github.com/adamdevlpmnt/Vault-Tracker.git
cd Vault-Tracker
```

### 2 — Configure

Edit `docker-compose.yml` with your qBittorrent connection details:

```yaml
environment:
  - QB_HOST=http://192.168.1.100   # Your qBittorrent host (include http:// or https://)
  - QB_PORT=8080                    # WebUI port
  - QB_USERNAME=admin               # WebUI username
  - QB_PASSWORD=your-password       # WebUI password
  - MIN_SIZE_BYTES=4294967296       # Optional: only protect torrents ≥ 4 GB (0 = disabled)
```

### 3 — Start

```bash
docker compose up -d
```

The image is pulled automatically from `ghcr.io/adamdevlpmnt/vault-tracker:latest`.

### 4 — Verify

```bash
docker logs -f vault-tracker
```

Expected output on a healthy startup:

```
[2026-04-14 16:00:00] [INFO ] ============================================================
[2026-04-14 16:00:00] [INFO ] 🚀 Vault-Tracker v3.0.2 starting
[2026-04-14 16:00:00] [INFO ]    qb_url:    http://192.168.1.100:8080
[2026-04-14 16:00:00] [INFO ]    min_size:  4.0 GB
[2026-04-14 16:00:00] [INFO ]    log_level: INFO
[2026-04-14 16:00:00] [INFO ]    db_path:   /data/vault-tracker.db
[2026-04-14 16:00:00] [INFO ] ============================================================
[2026-04-14 16:00:00] [INFO ] 🔌 Connected to qBittorrent WebUI → ✅ OK
[2026-04-14 16:00:00] [INFO ] 🔁 Container restart → no pending completions in database
[2026-04-14 16:00:01] [INFO ] 🔍 Initial scan — 24 torrent(s): 2 downloading, 22 seeding, 0 other
[2026-04-14 16:00:01] [INFO ] 👁️  Real-time monitoring active — watching for changes…
```

---

## ⚙️ Configuration reference

All configuration is done through **environment variables** — no config files, no mounts required beyond `/data`.

| Variable | Default | Description |
|---|---|---|
| `QB_HOST` | `http://localhost` | qBittorrent WebUI host — include `http://` or `https://` |
| `QB_PORT` | `8080` | qBittorrent WebUI port |
| `QB_USERNAME` | `admin` | WebUI login username |
| `QB_PASSWORD` | `adminadmin` | WebUI login password |
| `RETRY_DELAY` | `30` | Seconds to wait before retrying when qBittorrent is unreachable |
| `MAX_RETRIES` | `0` | Max connection attempts — `0` means retry forever |
| `DB_PATH` | `/data/vault-tracker.db` | SQLite database path inside the container |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `MIN_SIZE_BYTES` | `0` | Only protect torrents ≥ this size in bytes — `0` disables the filter |

### Using an `.env` file

Instead of editing `docker-compose.yml` directly, you can use a `.env` file:

```env
QB_HOST=http://192.168.1.100
QB_PORT=8080
QB_USERNAME=admin
QB_PASSWORD=your-secret-password
MIN_SIZE_BYTES=4294967296
RETRY_DELAY=30
MAX_RETRIES=0
LOG_LEVEL=INFO
```

Then reference it in your compose file:

```yaml
services:
  vault-tracker:
    image: ghcr.io/adamdevlpmnt/vault-tracker:latest
    container_name: vault-tracker
    restart: unless-stopped
    env_file: .env
    volumes:
      - vault-tracker-data:/data

volumes:
  vault-tracker-data:
```

---

## 🔍 Private tracker detection

Vault-Tracker identifies private tracker URLs by scanning for common authentication query parameters:

| Parameter | Example |
|---|---|
| `passkey=` | `https://tracker.example.com/announce?passkey=abc123` |
| `authkey=` | `https://tracker.example.com/announce?authkey=xyz789` |
| `torrent_pass=` | `https://tracker.example.com/announce?torrent_pass=secret` |
| `pid=` | `https://tracker.example.com/announce?pid=12345` |
| `secure=` | `https://tracker.example.com/announce?secure=token` |
| `auth=` | `https://tracker.example.com/announce?auth=value` |
| `key=` | `https://tracker.example.com/announce?key=abc` |
| `user=` | `https://tracker.example.com/announce?user=name` |

**Public trackers** (e.g. `udp://tracker.opentrackr.org:1337/announce`) are never touched.

All passkeys are **partially masked** in logs: `?passkey=abc***456` — safe to share without exposing credentials.

---

## 📋 Log reference

Every action produces a structured, timestamped log line visible via `docker logs vault-tracker`.

| Emoji | Event |
|---|---|
| 🚀 | Service startup |
| 🔌 | qBittorrent WebUI connection attempt |
| 🔁 | Container restart — pending recovery check |
| 🔍 | Initial torrent scan on startup |
| 👁️ | Real-time monitoring active |
| 🔔 | New torrent detected |
| ⏭️ | Torrent skipped (size filter) |
| 💾 | Tracker URL saved to database |
| ✂️ | Tracker URL stripped from active torrent |
| 🗑️ | Torrent deleted from qBittorrent (pre re-add) |
| 📥 | Torrent re-added with original metadata |
| ✅ | Download complete — seeding state detected |
| 🎉 | Torrent fully completed and seeding with tracker |
| ⚠️ | qBittorrent unreachable — retrying |
| 🔄 | Database schema migration applied |
| 🛑 | Graceful shutdown |

### Example log — full lifecycle

```
[2026-04-14 16:01:00] [INFO ] 🔔 New torrent: "My.Show.S01E01.2160p" [a1b2c3d4] [state: metaDL]
[2026-04-14 16:01:00] [INFO ] 🆕 New torrent: "My.Show.S01E01.2160p" [a1b2c3d4] [size: 12.40 GB] [state: metaDL]
[2026-04-14 16:01:00] [INFO ]    ↳ .torrent exported (45312 bytes)
[2026-04-14 16:01:00] [INFO ]    💾 Saved: https://tracker.example.com/announce?passkey=abc***456
[2026-04-14 16:01:00] [INFO ]    ↳ 1 tracker(s) saved — waiting for active download to strip
[2026-04-14 16:01:05] [INFO ] 🔔 Download started [a1b2c3d4] [state: downloading] → stripping tracker
[2026-04-14 16:01:05] [INFO ] ✂️  Stripped 1 tracker(s) from "My.Show.S01E01.2160p" → downloading without tracker
...
[2026-04-14 16:45:12] [INFO ] ✅ Torrent completed: "My.Show.S01E01.2160p" [a1b2c3d4] — starting re-add workflow
[2026-04-14 16:45:12] [INFO ]    🗑️  Deleted from qBittorrent (files kept)
[2026-04-14 16:45:12] [INFO ]    📥 Re-added .torrent (save_path: /media/shows, category: tv)
[2026-04-14 16:45:12] [INFO ]    🎉 Done — torrent will check files and resume seeding with tracker
```

---

## 💾 Data persistence

The SQLite database is stored in a named Docker volume and **persists across container restarts, updates, and rebuilds**.

### Database schema (v3)

```sql
CREATE TABLE trackers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    torrent_hash  TEXT    NOT NULL,
    torrent_name  TEXT    NOT NULL,
    tracker_url   TEXT    NOT NULL,       -- full URL including passkey
    tier          INTEGER NOT NULL DEFAULT 0,
    save_path     TEXT,                  -- original download path
    content_path  TEXT,
    category      TEXT    DEFAULT '',
    tags          TEXT    DEFAULT '',
    torrent_file  BLOB,                  -- exported .torrent binary
    stripped_at   TEXT    NOT NULL,      -- ISO timestamp when stripped
    completed_at  TEXT,                  -- NULL while pending
    UNIQUE(torrent_hash, tracker_url)
);
```

### Automatic schema migration

Vault-Tracker automatically migrates databases from previous versions (v2 → v3) on first startup — no manual steps required.

### Restart recovery

On every startup, Vault-Tracker scans the database for torrents that were pending completion (stripped but not yet re-added). For each one:

- If it's **still downloading** → re-strips the tracker if needed
- If it's **already seeding** → immediately runs the delete + re-add workflow

This means a container restart or crash never leaves a torrent stranded without its tracker.

---

## 🏗️ Architecture

```
Vault-Tracker/
├── vault_tracker/
│   ├── __init__.py        # Package — exposes __version__ = "3.0.2"
│   ├── __main__.py        # Entry point: python -m vault_tracker
│   ├── config.py          # Typed config from environment variables
│   ├── database.py        # SQLite persistence layer (WAL mode)
│   ├── logger.py          # Custom structured formatter [YYYY-MM-DD HH:MM:SS] [LEVEL]
│   ├── qbittorrent.py     # qBittorrent WebUI API v2 client
│   └── service.py         # Core event loop — sync/maindata + state machine
├── .github/
│   └── workflows/
│       └── docker-publish.yml  # CI: push to branch dev → :dev tag; git tag v* → :latest
├── Dockerfile             # python:3.12-alpine, VOLUME /data
├── docker-compose.yml     # Reference deployment template
├── requirements.txt       # requests>=2.31,<3
├── .dockerignore
└── .gitignore
```

### Tech stack

| Component | Technology | Rationale |
|---|---|---|
| Runtime | **Python 3.12** | Lightweight, readable, I/O-bound polling |
| HTTP client | **requests ≥ 2.31** | Battle-tested, stable API surface |
| Database | **SQLite (WAL mode)** | Zero-dependency, single-writer, crash-safe |
| Base image | **python:3.12-alpine** | Minimal attack surface, ~50 MB image |
| Transport | **qBittorrent API v2** | `sync/maindata` delta endpoint for real-time events |

### Real-time sync

Vault-Tracker uses the `sync/maindata` endpoint with **request ID (rid) delta tracking** — polling only the diff since the last call, at 100ms intervals. This means:

- **Zero CPU overhead** between events
- **Near-instant** detection of new torrents and state changes
- **No missed events** — the rid chain guarantees full coverage

---

## 🐛 Troubleshooting

### Cannot connect to qBittorrent

- Ensure `QB_HOST` includes the protocol (`http://` or `https://`)
- Verify the qBittorrent WebUI is enabled: *Settings → Web UI → Enable the Web UI*
- If both services run in Docker, ensure they share a Docker network or use the host IP
- Use `QB_HOST=http://host.docker.internal` when qBittorrent runs directly on the host (Windows/macOS)
- Check firewall rules on `QB_PORT`

### Trackers not being stripped

- Set `LOG_LEVEL=DEBUG` and look for the tracker URL parsing output
- Confirm the tracker URL contains a [recognized auth parameter](#-private-tracker-detection)
- Verify the qBittorrent API user has *Bypass authentication for clients on localhost* disabled (or has the right permissions)

### Torrent not completing (not being re-added)

- Check that `/data` is writable — the volume must be mounted correctly
- Set `LOG_LEVEL=DEBUG` to see the full state machine transitions
- Inspect the DB: `docker exec vault-tracker sqlite3 /data/vault-tracker.db "SELECT torrent_name, stripped_at, completed_at FROM trackers;"`

### Database operations

```bash
# View all entries
docker exec vault-tracker sqlite3 /data/vault-tracker.db "SELECT torrent_name, stripped_at, completed_at FROM trackers;"

# Reset (deletes ALL saved tracker data — use with caution)
docker volume rm vault-tracker-data

# Full raw dump
docker exec vault-tracker sqlite3 /data/vault-tracker.db ".dump"
```

---

## 🐳 Docker images

Images are published automatically to the **GitHub Container Registry** via GitHub Actions on every push.

| Tag | Source | Description |
|---|---|---|
| `latest` | Git tag `v*` | Latest stable release |
| `3.x.x` | Git tag `v3.x.x` | Specific version (semver) |
| `dev` | Branch `dev` | Latest development build |

```bash
# Pull stable
docker pull ghcr.io/adamdevlpmnt/vault-tracker:latest

# Pull dev (bleeding edge)
docker pull ghcr.io/adamdevlpmnt/vault-tracker:dev
```

---

## 🤝 Contributing

1. Fork this repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Commit your changes
4. Open a Pull Request against the `dev` branch

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

---

<div align="center">
Made with ❤️ by <a href="https://github.com/adamdevlpmnt">adamdevlpmnt</a>
</div>
