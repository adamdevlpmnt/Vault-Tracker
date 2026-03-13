# 🔐 Vault-Tracker

**Private tracker passkey protector for qBittorrent.**

Vault-Tracker is a lightweight Docker service that automatically protects your private tracker passkeys during the download phase. It strips tracker URLs (and their embedded passkeys) from new private torrents as soon as they are added, stores them safely in a local database, and reinjects them once the torrent is ready to seed — keeping your ratio intact and your credentials unexposed.

---

## Why does this matter?

When you add a torrent from a private tracker, the `.torrent` file contains tracker URLs with **your unique passkey** embedded in them. During the download phase, your client announces to those trackers — consuming ratio and exposing your passkey to every peer.

Vault-Tracker solves this by:

1. **Immediately stripping** private tracker URLs from new torrents (zero ratio consumed during download).
2. **Safely storing** the tracker URLs + passkeys in an encrypted-at-rest SQLite database.
3. **Automatically reinjecting** them when the torrent transitions to seeding — so you seed normally without manual intervention.

---

## How it works

```
                        ┌─────────────────────┐
                        │  qBittorrent WebUI   │
                        │   (existing server)  │
                        └──────────┬──────────┘
                                   │ API v2
                                   ▼
┌──────────────────────────────────────────────────────────┐
│                     Vault-Tracker                        │
│                                                          │
│  ┌──────────┐    ┌───────────────┐    ┌──────────────┐  │
│  │  Poller  │───▶│ Tracker Logic │───▶│  SQLite DB   │  │
│  │ (loop)   │    │               │    │  /data/*.db  │  │
│  └──────────┘    └───────┬───────┘    └──────────────┘  │
│       │                  │                    ▲          │
│       │                  ▼                    │          │
│       │         ┌────────────────┐            │          │
│       └────────▶│ State Monitor  │────────────┘          │
│                 │ (seed detect)  │                       │
│                 └────────────────┘                       │
└──────────────────────────────────────────────────────────┘

Flow:
  1. New torrent detected
  2. Private tracker? ─── No ──▶ Skip
                      └── Yes ──▶ Save URLs to DB
                                  Strip URLs from torrent
  3. Torrent finishes downloading, enters seeding state
  4. Retrieve saved URLs from DB
  5. Reinject URLs into torrent
  6. Torrent seeds normally with full tracker connectivity
```

---

## Prerequisites

- **Docker** and **Docker Compose** installed on your server.
- A running **qBittorrent** instance with the **WebUI API enabled**.
- Network connectivity between the Vault-Tracker container and qBittorrent's WebUI.

---

## Quick start

### 1. Clone the repository

```bash
git clone https://github.com/adamdevlpmnt/Vault-Tracker.git
cd Vault-Tracker
```

### 2. Configure environment variables

Edit `docker-compose.yml` and set your qBittorrent connection details:

```yaml
environment:
  QB_HOST: "http://192.168.1.100"   # Your qBittorrent host
  QB_PORT: "8080"                    # WebUI port
  QB_USERNAME: "admin"               # WebUI username
  QB_PASSWORD: "your-password"       # WebUI password
```

### 3. Start

```bash
docker compose up -d
```

The image is pulled automatically from `ghcr.io/adamdevlpmnt/vault-tracker:latest`.

### 4. Check logs

```bash
docker logs -f vault-tracker
```

You should see:

```
[2026-03-13 13:00:00] [INFO ] 🚀 Vault-Tracker v1.0.0 starting
[2026-03-13 13:00:00] [INFO ] 🔌 Connected to qBittorrent WebUI → ✅ OK
[2026-03-13 13:00:00] [INFO ] 🔁 Container restart → no pending reinjections in database
[2026-03-13 13:00:01] [INFO ] 🔄 Polling cycle — 12 torrent(s) detected
```

---

## Configuration reference

All configuration is done through **environment variables** — no config files to manage.

| Variable | Default | Description |
|---|---|---|
| `QB_HOST` | `http://localhost` | qBittorrent WebUI host (include `http://` or `https://`) |
| `QB_PORT` | `8080` | qBittorrent WebUI port |
| `QB_USERNAME` | `admin` | WebUI username |
| `QB_PASSWORD` | `adminadmin` | WebUI password |
| `POLL_INTERVAL` | `10` | Seconds between polling cycles |
| `RETRY_DELAY` | `30` | Seconds to wait before retrying when qBittorrent is unreachable |
| `MAX_RETRIES` | `0` | Maximum retry attempts (`0` = retry forever) |
| `DB_PATH` | `/data/vault-tracker.db` | Path to the SQLite database inside the container |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Using an `.env` file

Instead of editing `docker-compose.yml` directly, you can create a `.env` file:

```env
QB_HOST=http://192.168.1.100
QB_PORT=8080
QB_USERNAME=admin
QB_PASSWORD=your-secret-password
POLL_INTERVAL=10
RETRY_DELAY=30
MAX_RETRIES=0
LOG_LEVEL=INFO
```

Then update `docker-compose.yml` to use it:

```yaml
services:
  vault-tracker:
    image: ghcr.io/adamdevlpmnt/vault-tracker:latest
    container_name: vault-tracker
    restart: unless-stopped
    env_file: .env
    volumes:
      - /mnt/TANK/Config/Vault-Tracker:/data
```

---

## Log events

Every action produces a structured, timestamped log entry visible via `docker logs vault-tracker`.

| Emoji | Event | Description |
|---|---|---|
| 🔌 | Connection | Connecting to qBittorrent WebUI (OK / ERROR) |
| 🔄 | Poll cycle | Start of a polling cycle with torrent count |
| 🆕 | New torrent | A torrent was detected for the first time |
| 🔍 | Privacy check | Whether the torrent has private trackers |
| 💾 | Tracker saved | Tracker URL persisted to database |
| ✂️ | Tracker stripped | Tracker URL removed from active torrent |
| ⏳ | Downloading | Torrent download in progress with state |
| ✅ | Seeding | Torrent completed and entered seeding state |
| 💉 | Reinjected | Tracker URL added back to the torrent |
| ⚠️ | Unreachable | qBittorrent connection lost, retrying |
| 🔁 | Recovery | Container restarted, listing pending reinjections |
| 🛑 | Shutdown | Graceful shutdown initiated |

### Example log output

```
[2026-03-13 13:00:01] [INFO ] 🆕 New torrent detected: "Ubuntu 24.04" [hash: a1b2c3d4]
[2026-03-13 13:00:01] [INFO ] 🔍 Private tracker check for "Ubuntu 24.04" → private (1 tracker(s))
[2026-03-13 13:00:01] [INFO ] 💾 Tracker URL saved: https://tracker.example.com/ann?passkey=abc***456 → ✅ OK
[2026-03-13 13:00:02] [INFO ] ✂️  Tracker(s) stripped from "Ubuntu 24.04" → ✅ OK
...
[2026-03-13 13:05:12] [INFO ] ✅ Torrent completed, seeding state detected: "Ubuntu 24.04"
[2026-03-13 13:05:12] [INFO ] 💉 Tracker URL reinjected: https://tracker.example.com/ann?passkey=abc***456 → ✅ OK
```

---

## Data persistence

The SQLite database is stored in a Docker named volume (`vault-tracker-data`), which persists across container restarts and rebuilds. On startup, the service:

1. Reads all pending reinjections from the database.
2. Checks if any of those torrents are already in a seeding state.
3. Immediately reinjects trackers for completed torrents.

This means you can safely restart, update, or rebuild the container without losing any tracker data.

---

## How private trackers are detected

Vault-Tracker identifies private trackers by scanning tracker URLs for common authentication parameters:

- `passkey=`
- `authkey=`
- `torrent_pass=`
- `pid=`
- `secure=`
- `auth=`
- `key=`
- `user=`

Any tracker URL containing one of these parameters is treated as a private tracker. Public trackers (e.g., `udp://tracker.opentrackr.org:1337/announce`) are left untouched.

---

## Architecture

```
vault-tracker/
├── vault_tracker/
│   ├── __init__.py        # Package metadata
│   ├── __main__.py        # Entry point (python -m vault_tracker)
│   ├── config.py          # Environment variable configuration
│   ├── database.py        # SQLite persistence layer
│   ├── logger.py          # Structured emoji logging
│   ├── qbittorrent.py     # qBittorrent WebUI API client
│   └── service.py         # Core polling loop & tracker logic
├── docker-compose.yml     # Deployment configuration
├── Dockerfile             # Container build
├── requirements.txt       # Python dependencies (requests)
├── .dockerignore
├── .gitignore
└── README.md
```

**Tech stack:**

- **Python 3.12** — lightweight, readable, well-suited for I/O-bound polling.
- **requests** — battle-tested HTTP client for the qBittorrent API.
- **SQLite (WAL mode)** — zero-dependency embedded database, perfect for single-writer persistence.
- **Docker** — isolated, reproducible deployment.

---

## Troubleshooting

### Cannot connect to qBittorrent

- Verify that `QB_HOST` includes the protocol (`http://` or `https://`).
- Ensure the qBittorrent WebUI is enabled and listening on the configured port.
- If qBittorrent runs in Docker, make sure both containers share a network or use the host IP.
- Check your firewall rules allow traffic on `QB_PORT`.

### Trackers not being stripped

- Set `LOG_LEVEL=DEBUG` for more verbose output.
- Ensure the tracker URL contains a recognized authentication parameter (see detection section above).
- Verify the qBittorrent API user has permission to modify trackers.

### Database issues

- The database is stored at `DB_PATH` inside the container (default: `/data/vault-tracker.db`).
- To reset: `docker volume rm vault-tracker-data` — this deletes all saved tracker data.
- To inspect: `docker exec vault-tracker sqlite3 /data/vault-tracker.db ".dump"`

---

## License

MIT
