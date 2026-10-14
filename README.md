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

Use `--until-complete` to let a single process run until the queue is empty: it archives while UTC quota remains, waits until **00:00 UTC** (plus 60 seconds) when the daily cap is reached, then continues. Long runs can span multiple UTC days without manual restarts. Press Ctrl+C to stop safely — progress is saved after each capture.

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

arkivari loads optional credentials from a `.env` file (gitignored) or the environment. When both `SAVEPAGENOW_ACCESS_KEY` and `SAVEPAGENOW_SECRET_KEY` are set, it uses the [SPN2 API](https://archive.org/account/s3.php) automatically (`Authorization: LOW access:secret`). Use `--anonymous` to force anonymous mode.

Create S3 keys at [archive.org/account/s3.php](https://archive.org/account/s3.php) (same keys as IA-S3 uploads). Never commit keys to git.

| Mode | Captures / minute | Captures / UTC day | Notes |
|------|-------------------|--------------------|--------|
| Anonymous | 3 | 4,000 | Default without keys |
| Authenticated | 7 | 100,000 | Auto when keys are set |

arkivari runs captures **sequentially** (one at a time). Internet Archive also limits concurrent pending jobs (12 authenticated) and **10 captures per URL per UTC day**.

Authenticated submits use `capture_all=1`, `skip_first_archive=1`, and `if_not_archived_within=30m` so recent captures count as cached without a full re-capture when possible.

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
