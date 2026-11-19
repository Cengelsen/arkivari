-- Arkivari modular pipeline schema v1

CREATE TYPE crawl_status AS ENUM ('idle', 'scheduled', 'running', 'failed');
CREATE TYPE frontier_state AS ENUM ('queued', 'in_flight', 'done');
CREATE TYPE url_source AS ENUM ('sitemap', 'crawl');
CREATE TYPE link_status AS ENUM (
    'ok',
    'redirected',
    'not_found',
    'gone',
    'forbidden',
    'auth_required',
    'rate_limited',
    'server_error',
    'network_error',
    'soft_404',
    'pending'
);
CREATE TYPE archive_status AS ENUM (
    'pending',
    'leased',
    'archived',
    'cached',
    'failed',
    'skipped_robots',
    'deferred',
    'skipped'
);

CREATE TABLE domains (
    id BIGSERIAL PRIMARY KEY,
    host TEXT NOT NULL,
    base_url TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    max_pages INTEGER NOT NULL DEFAULT 0,
    recrawl_after_days INTEGER NOT NULL DEFAULT 90,
    note TEXT NOT NULL DEFAULT '',
    crawl_status crawl_status NOT NULL DEFAULT 'idle',
    crawl_lease_until TIMESTAMPTZ,
    crawl_lease_owner TEXT,
    last_crawl_started_at TIMESTAMPTZ,
    last_crawl_completed_at TIMESTAMPTZ,
    next_crawl_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT domains_host_unique UNIQUE (host)
);

CREATE INDEX domains_scheduler_idx ON domains (enabled, crawl_status, next_crawl_at)
    WHERE enabled = TRUE;

CREATE TABLE crawl_frontier (
    id BIGSERIAL PRIMARY KEY,
    domain_id BIGINT NOT NULL REFERENCES domains (id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    state frontier_state NOT NULL DEFAULT 'queued',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT crawl_frontier_domain_url_unique UNIQUE (domain_id, url)
);

CREATE INDEX crawl_frontier_resume_idx ON crawl_frontier (domain_id, state)
    WHERE state IN ('queued', 'in_flight');

CREATE TABLE urls (
    id BIGSERIAL PRIMARY KEY,
    domain_id BIGINT NOT NULL REFERENCES domains (id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    source url_source,
    discovered_at TIMESTAMPTZ,
    link_status link_status NOT NULL DEFAULT 'pending',
    http_code INTEGER NOT NULL DEFAULT 0,
    final_url TEXT,
    validated_at TIMESTAMPTZ,
    archive_status archive_status NOT NULL DEFAULT 'pending',
    archive_url TEXT,
    last_error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    archived_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    lease_owner TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT urls_normalized_url_unique UNIQUE (normalized_url)
);

CREATE INDEX urls_pending_archive_idx ON urls (domain_id, created_at)
    WHERE archive_status = 'pending'
      AND link_status IN (
          'ok', 'redirected', 'forbidden', 'auth_required'
      );

CREATE INDEX urls_archive_lease_idx ON urls (lease_until)
    WHERE archive_status = 'leased';

CREATE TABLE quota_daily (
    date_utc DATE PRIMARY KEY,
    success_count INTEGER NOT NULL DEFAULT 0
);