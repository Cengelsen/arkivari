# arkivari

Discover public pages on a domain and archive them to the [Internet Archive](https://archive.org/) via Save Page Now.

## Features

- Discovers URLs from `robots.txt` sitemaps, `/sitemap.xml`, and bounded HTML link crawling
- Respects `robots.txt` (`Allow`/`Disallow`, `Crawl-delay`, declared sitemaps)
- Archives pages via Save Page Now (POST + async job polling, same flow as the web UI)
- Writes a JSON report of discovered and archived URLs

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Discover and archive all public pages on a domain
python -m arkivari example.com

# Discover only (no archiving)
python -m arkivari example.com --dry-run --verbose

# Limit discovery and archiving
python -m arkivari example.com --max-pages 100 --max-archives 50

# Custom report path
python -m arkivari example.com --output results.json
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--max-pages` | 500 | Max pages to discover via crawling |
| `--max-archives` | 4000 | Max URLs to submit to Internet Archive |
| `--dry-run` | off | Discover only, don't archive |
| `--output` | `arkivari-results.json` | JSON report path |
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
- If robots.txt is missing or unreachable, no restrictions are applied (with a warning)

## Output

The JSON report includes:

```json
{
  "domain": "https://example.com",
  "timestamp": "2026-09-18T20:00:00+00:00",
  "discovered_count": 42,
  "archived_count": 30,
  "cached_count": 10,
  "skipped_robots": 1,
  "failed_count": 1,
  "dry_run": false,
  "results": [
    {"url": "https://example.com/", "status": "archived", "archive_url": "https://web.archive.org/web/..."}
  ],
  "urls": ["https://example.com/", "..."]
}
```

## Limitations

- **Static HTML only** — JavaScript-rendered SPAs won't be fully discovered
- **Anonymous IA limits** — large sites may need multiple daily runs
- **robots.txt is advisory for archiving** — arkivari complies on its side; Internet Archive's own fetch may still be blocked by the target site
- **No CDX pre-check** — discovers pages first rather than re-archiving URLs Internet Archive already knows about

## License

MIT
