# arkivari

Discover public pages on a domain and archive them to the [Internet Archive](https://archive.org/) via Save Page Now.

## Features

- Discovers URLs from `robots.txt` sitemaps, `/sitemap.xml`, and HTML link crawling (unlimited by default)
- Respects `robots.txt` (`Allow`/`Disallow`, `Crawl-delay`, declared sitemaps)
- Archives pages via Save Page Now (POST + async job polling, same flow as the web UI)
- **Multi-day scheduling** — queues all archivable URLs and archives up to 4,000 per calendar day
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

# Run to completion — archives 4k/day and waits overnight automatically
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

The queue file tracks completed URLs, pending URLs, and today's quota so you cannot accidentally exceed the daily limit by re-running the same day.

Use `--until-complete` to let a single process handle the full schedule: it archives up to the daily limit, sleeps until the next calendar day, then continues until the queue is empty. Press Ctrl+C to stop safely — progress is saved to the queue file.

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--max-pages` | 0 (unlimited) | Max total pages to discover (sitemap + crawl) |
| `--daily-limit` | 4000 | Max URLs to archive per calendar day |
| `--queue-file` | `arkivari-queue-<domain>.json` | Persistent queue path |
| `--rediscover` | off | Re-run discovery and refresh pending URLs |
| `--until-complete` | off | Archive daily batches until done; waits until midnight between days |
| `--status` | off | Show queue status and exit |
| `--dry-run` | off | Discover and show schedule only |
| `--yes`, `-y` | off | Archive without confirmation prompt |
| `--output` | `arkivari-results.json` | JSON report path per batch |
| `--user-agent` | `arkivari/1.0 (...)` | User-Agent for crawl and archive requests |
| `--verbose` | off | Per-URL logging |

Exit codes: `0` success, `1` partial failure (some archives failed), `2` fatal error.

## Internet Archive limits (anonymous)

arkivari enforces these Save Page Now limits when running without credentials:

| Limit | Value |
|-------|-------|
| Captures per minute | 3 |
| Captures per day | 4,000 |
| Concurrent captures | 1 (sequential) |

Recently archived URLs are returned as cached snapshots rather than re-captured. This counts as success and saves quota.

Authenticated mode (6 captures/min, 100k/day) can be added later via `SAVEPAGENOW_ACCESS_KEY` and `SAVEPAGENOW_SECRET_KEY` environment variables.

## robots.txt behavior

- Fetches `https://{domain}/robots.txt`, falling back to HTTP
- Checks `can_fetch()` before every crawl request and archive submission
- Honors `Crawl-delay` (falls back to `*` rule, then 1 second default)
- Uses `Sitemap:` directives from robots.txt for discovery
- Only robots-allowed URLs are added to the archive queue
- If robots.txt is missing or unreachable, no restrictions are applied (with a warning)

## Output

### Queue file (`arkivari-queue-<domain>.json`)

Persists pending URLs, completed archive results, daily limit, and today's quota usage.

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
- **Anonymous IA limits** — large sites require one run per day until the queue is complete
- **robots.txt is advisory for archiving** — arkivari complies on its side; Internet Archive's own fetch may still be blocked by the target site
- **No CDX pre-check** — discovers pages first rather than re-archiving URLs Internet Archive already knows about
- **Failed captures are re-queued** — transient IA errors are retried on a later run

## License

MIT
