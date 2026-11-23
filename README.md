# arkivari

Postgres-backed pipeline: a **Go crawler** discovers and validates URLs, then a **Python archiver** sends them to [Internet Archive](https://archive.org/) Save Page Now.

## Architecture

```
domains.json ──► Go crawler ──► Postgres (urls, domains, quota)
                                      │
                                      │ NOTIFY arkivari_pending
                                      ▼
                               Python archiver ──► web.archive.org
```

| Component | Path | Role |
|-----------|------|------|
| Postgres | `migrations/`, Compose `postgres` + `migrate` | Schema, crawl state, archive queue |
| Go crawler | [`crawler/`](crawler/) | Sync `domains.json`, discover, GET crawl-check, upsert includable URLs |
| Python archiver | [`arkivari/`](arkivari/) | `LISTEN`/`NOTIFY`, claim pending rows, SPN capture |

Go entrypoint: `crawler/cmd/crawler` → [`internal/crawl`](crawler/internal/crawl/runner.go) (discover → validate → [`internal/pipeline`](crawler/internal/pipeline/persist.go) → Postgres).

Python entrypoint: `python -m arkivari` or `python -m arkivari.daemon` → [`arkivari/db/store.py`](arkivari/db/store.py) + [`arkivari/session.py`](arkivari/session.py) + [`arkivari/archive.py`](arkivari/archive.py).

## Quick start (Docker Compose)

```bash
cp .env.example .env
cp domains.json.example domains.json
# Optional: SAVEPAGENOW_ACCESS_KEY / SAVEPAGENOW_SECRET_KEY in .env

# Edit domains.json — set "enabled": true on domains to crawl
docker compose up -d --build
```

Expose Postgres on the host (optional): copy `compose.override.yaml.example` to `compose.override.yaml`.

### Smoke checklist

1. `docker compose ps` — `postgres`, `crawler`, `archiver` running; `migrate` exited 0.
2. Enable a domain in `domains.json` and `docker compose restart crawler`.
3. `docker compose exec postgres psql -U arkivari -c "SELECT COUNT(*) FROM urls WHERE archive_status='pending';"`
4. Archiver logs show Save Page Now activity when pending rows exist.

## Local development

Start Postgres and apply migrations (once), e.g. with Compose:

```bash
cp compose.override.yaml.example compose.override.yaml   # exposes 5432 on the host
docker compose up -d postgres
docker compose run --rm migrate
```

Copy `domains.json.example` to `domains.json` at the repo root (same path Compose uses).

### Go crawler

From `crawler/`, the default `DOMAIN_LIST_PATH` is `domains.json` in that directory — point at the repo-root file:

```bash
export DATABASE_URL=postgres://arkivari:arkivari@localhost:5432/arkivari?sslmode=disable
export DOMAIN_LIST_PATH=../domains.json
cd crawler && go run ./cmd/crawler
```

### Python archiver

```bash
pip install -r requirements.txt
export DATABASE_URL=postgres://arkivari:arkivari@localhost:5432/arkivari?sslmode=disable
python -m arkivari
```

## Configuration

- **Crawler:** `DATABASE_URL`, `DOMAIN_LIST_PATH`, `DOMAIN_SYNC_INTERVAL`, `CRAWLER_USER_AGENT` (see `compose.yaml`).
- **Archiver:** `DATABASE_URL`, `ARCHIVER_IDLE_POLL_SECONDS`, `ARKIVER_USER_AGENT`, SPN keys in `.env`.
- **Go crawl defaults:** 0.1s origin delay, abort on first HTTP 429, 16 concurrent origin slots ([`crawler/internal/config`](crawler/internal/config/limits.go)).

## domains.json

Create from `domains.json.example` (gitignored; not committed). Each entry: `host`, optional `max_pages` (0 = unlimited), `recrawl_after_days`, `enabled`, `note`. The crawler syncs every listed host into `domains` (with its `enabled` flag) and only schedules crawls for enabled rows.

## Limitations

- Discovery from HTML links and sitemaps only (no JS rendering).
- Internet Archive daily capture limits apply (see archiver quota in `quota_daily`).
- robots.txt respected on crawl and archive (archiver uses per-domain cache).

## License

Copyright © 2026 Christian K. Engelsen

This project is licensed under the [GNU General Public License v3.0](LICENSE).