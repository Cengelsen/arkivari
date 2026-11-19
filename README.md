# arkivari

Discover public pages on a domain and archive them to the [Internet Archive](https://archive.org/) via Save Page Now.

## Features

- Discovers URLs from `robots.txt` sitemaps, `/sitemap.xml`, and HTML link crawling (unlimited by default)
- Respects `robots.txt` (`Allow`/`Disallow`, `Crawl-delay`, declared sitemaps)
- Archives pages via Save Page Now (POST + async job polling, same flow as the web UI)
- **Multi-day scheduling** — queues all archivable URLs and archives up to 4,000 per UTC calendar day (aligned with Save Page Now)
- Persistent queue file for resuming across daily runs
- Writes a JSON report after each archive batch

## Installation

```bash
pip install -r requirements.txt
```

## Docker Compose (modular pipeline)

Postgres + Go **crawler** daemon + Python **archiver** daemon. The legacy CLI (`python -m arkivari`) still works with JSON queue files.

### Quick start

```bash
cp .env.example .env
# Optional: add SAVEPAGENOW_ACCESS_KEY / SAVEPAGENOW_SECRET_KEY for authenticated limits

# Edit domains.json — set "enabled": true on domains to crawl
docker compose up -d --build
```

Services:

| Service | Role |
|---------|------|
| `postgres` | URL/domain state, archive queue, UTC quota |
| `migrate` | Applies SQL migrations (one-shot) |
| `crawler` | Syncs `domains.json`, discovers URLs, validates, writes `pending` rows |
| `archiver` | `LISTEN arkivari_pending`, claims URLs, Save Page Now |

### Environment

See [`.env.example`](.env.example). In Compose, `DATABASE_URL` is set automatically for app containers. Mount secrets via `.env` for the archiver only.

### Import legacy JSON queues

```bash
export DATABASE_URL=postgres://arkivari:arkivari@localhost:5432/arkivari?sslmode=disable
python -m arkivari.db.import_queue arkivari-queue-example-com.json
```

Expose Postgres locally: copy `compose.override.yaml.example` to `compose.override.yaml`.

### Smoke checklist

1. `docker compose ps` — `postgres`, `crawler`, `archiver` running; `migrate` exited 0.
2. Enable a domain in `domains.json` and `docker compose restart crawler`.
3. `docker compose exec postgres psql -U arkivari -c "SELECT COUNT(*) FROM urls WHERE archive_status='pending';"`
4. Archiver logs show capture activity (or quota wait when empty).
5. Legacy CLI still works: `python -m arkivari example.com --dry-run`.

## Usage

```bash
# Discover all pages, build queue, confirm, archive first 4k batch
python -m arkivari example.com

# Skip confirmation prompt
python -m arkivari example.com --yes

# Preview discovery and schedule without archiving or saving queue
python -m arkivari example.com --dry-run

# Resume tomorrow (picks up pending queue automatically)
python -m arkivari example.com --yes

# Run to completion — uses UTC daily quota and waits after 00:00 UTC when needed
python -m arkivari example.com --until-complete --yes

# Show queue progress without archiving
python -m arkivari example.com --status

# Re-discover URLs (keeps already-archived pages)
python -m arkivari example.com --rediscover

# Cap discovery for testing
python -m arkivari example.com --max-pages 500 --dry-run
```

### Multi-day workflow

For a site with 80,000 archivable pages:

1. **Day 1:** `python -m arkivari example.com` discovers all URLs, creates `arkivari-queue-example-com.json` with 80k pending, archives 4,000.
2. **Day 2–19:** Run the same command; discovery is skipped and the next 4,000 are archived.
3. **Day 20:** Final batch completes the queue.

The queue file tracks completed URLs, pending URLs, and per-date quota usage (`archives_by_date`) so you cannot accidentally exceed the daily limit by re-running the same UTC day.

Use `--until-complete` to let a single process run until the queue is empty: it archives while UTC quota remains, waits until **00:00 UTC** (plus 60 seconds) when the daily cap is reached, then continues. Long runs can span multiple UTC days without manual restarts. Press Ctrl+C to stop safely — progress is saved periodically and on interrupt. In-flight captures that haven't completed at interrupt time may need to be re-archived on the next run.

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--max-pages` | 0 (unlimited) | Max total pages to discover (sitemap + crawl) |
| `--daily-limit` | 4000 | Max URLs to archive per UTC calendar day |
| `--queue-file` | `arkivari-queue-<domain>.json` | Persistent queue path |
| `--rediscover` | off | Re-run discovery and refresh pending URLs |
| `--until-complete` | off | Archive until done; waits when daily UTC quota is used |
| `--status` | off | Show queue status and exit |
| `--dry-run` | off | Discover and show schedule only |
| `--yes`, `-y` | off | Archive without confirmation prompt |
| `--output` | `arkivari-results.json` | JSON report path per batch |
| `--user-agent` | `arkivari/1.0 (...)` | User-Agent for crawl and archive requests |
| `--verbose` | off | Per-URL logging |

Exit codes: `0` success, `1` partial failure (some archives failed), `2` fatal error.

## Internet Archive limits

arkivari loads optional credentials from a `.env` file (gitignored) or the environment. Authenticated mode uses the [SPN2 JSON API](https://archive.org/account/s3.php) with either:

- **S3 keys** — `SAVEPAGENOW_ACCESS_KEY` and `SAVEPAGENOW_SECRET_KEY` (`Authorization: LOW access:secret`), or
- **Browser session cookies** — `SAVEPAGENOW_LOGGED_IN_SIG` and `SAVEPAGENOW_LOGGED_IN_USER` (same names as on [archive.org](https://archive.org); copy values from DevTools → Application → Cookies while logged in). Cookie values expire after a few days; refresh them when auth fails.

If both S3 keys and cookies are set, S3 is used unless `SAVEPAGENOW_PREFER_COOKIES=1` is set (useful when S3 keys fail but the web UI still works). Use `--anonymous` to force anonymous mode.

`.env` is searched in order: current working directory, the arkivari repository root, then up to five parent directories of the cwd. Pass `--env-file PATH` to load a specific file. All `SAVEPAGENOW_*` variables in `.env` **override** the same names already exported in your shell (other keys in `.env` only apply when not already set in the environment).

Before archiving (and for `--status` / `--dry-run` when keys are present), arkivari checks [IA-S3 `check_auth`](https://archive.org/developers/ias3.html) and a minimal Save Page Now submit probe. Invalid keys exit with code `2` instead of re-queuing thousands of URLs.

Create S3 keys at [archive.org/account/s3.php](https://archive.org/account/s3.php) (same keys as IA-S3 uploads). Never commit keys to git.

| Mode | Captures / minute | Captures / UTC day | Notes |
|------|-------------------|--------------------|--------|
| Anonymous | 3 | 4,000 | Default without keys |
| Authenticated | 7 | 100,000 | Auto when keys are set |

arkivari submits captures **concurrently** (see `AUTHENTICATED_MAX_CONCURRENT` / `ANONYMOUS_MAX_CONCURRENT` in `arkivari/spn_config.py`), rate-limited by `AUTHENTICATED_CAPTURES_PER_MINUTE` / `ANONYMOUS_CAPTURES_PER_MINUTE`. Status polls are spaced globally (~2s minimum across all workers) so concurrent jobs do not flood IA's Save Page Now API. Internet Archive also limits **10 captures per URL per UTC day**.

Authenticated submits also use `skip_first_archive=1` and `if_not_archived_within=30m` so recent captures count as cached without a full re-capture when possible.

### Save Page Now capture options

Edit `SPN_CAPTURE_OPTIONS` in `arkivari/spn_config.py` to turn Save Page Now form options on or off (`True` / `False`). Enabled options are sent on every capture; keys match the [Save Page Now](https://web.archive.org/save/) checkboxes (for example `capture_outlinks`, `wacz`). Screenshot, personal web archive, and email options require S3 keys tied to your Internet Archive account.

Resuming a queue file that still has `daily_limit: 4000` will upgrade to 100,000 when S3 keys are present (unless you pass `--daily-limit`).

Recently archived URLs are returned as cached snapshots rather than re-captured. This counts toward your daily quota.

## robots.txt behavior

- Fetches `https://{domain}/robots.txt`, falling back to HTTP
- Checks `can_fetch()` before every crawl request and archive submission
- Honors `Crawl-delay` (falls back to `*` rule, then 1 second default)
- Uses `Sitemap:` directives from robots.txt for discovery
- Only robots-allowed URLs are added to the archive queue
- If robots.txt is missing or unreachable, no restrictions are applied (with a warning)

## Output

### Queue file (`arkivari-queue-<domain>.json`)

Persists pending URLs, completed archive results, daily limit, and `archives_by_date` (quota used per UTC date). Queue file version is `2`.

### Batch report (`arkivari-results.json`)

```json
{
  "domain": "https://example.com",
  "timestamp": "2026-09-18T20:00:00+00:00",
  "queue": {
    "total": 80000,
    "completed": 4000,
    "pending": 76000,
    "daily_limit": 4000,
    "days_remaining": 19
  },
  "archived_count": 3800,
  "cached_count": 200,
  "skipped_robots": 0,
  "failed_count": 0,
  "results": [
    {"url": "https://example.com/", "status": "archived", "archive_url": "https://web.archive.org/web/..."}
  ]
}
```

## Limitations

- **Static HTML only** — JavaScript-rendered SPAs won't be fully discovered
- **Anonymous IA limits** — quota is per UTC day; use `--until-complete` for continuous multi-day runs or invoke once per UTC day manually
- **robots.txt is advisory for archiving** — arkivari complies on its side; Internet Archive's own fetch may still be blocked by the target site
- **No CDX pre-check** — discovers pages first rather than re-archiving URLs Internet Archive already knows about
- **Failed captures are re-queued** — transient IA errors are retried on a later run

## License

MIT
