package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/arkivari/crawler/internal/urlnorm"
	"github.com/arkivari/crawler/internal/validate"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Store struct {
	pool   *pgxpool.Pool
	owner  string
	notify bool
}

func New(pool *pgxpool.Pool, owner string) *Store {
	return &Store{pool: pool, owner: owner, notify: true}
}

func WaitForDB(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
	for {
		pool, err := pgxpool.New(ctx, dsn)
		if err == nil {
			if err := pool.Ping(ctx); err == nil {
				return pool, nil
			}
			pool.Close()
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(3 * time.Second):
		}
	}
}

type DomainRow struct {
	ID               int64
	Host             string
	BaseURL          string
	MaxPages         int
	RecrawlAfterDays int
	CrawlStatus      string
}

func (s *Store) AcquireDomain(ctx context.Context) (*DomainRow, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	row := tx.QueryRow(ctx, `
		SELECT id, host, base_url, max_pages, recrawl_after_days, crawl_status::text
		FROM domains
		WHERE enabled = TRUE
		  AND crawl_status IN ('idle', 'scheduled', 'failed')
		  AND (next_crawl_at IS NULL OR next_crawl_at <= NOW())
		  AND (crawl_lease_until IS NULL OR crawl_lease_until < NOW())
		ORDER BY next_crawl_at NULLS FIRST, id
		FOR UPDATE SKIP LOCKED
		LIMIT 1
	`)
	var d DomainRow
	if err := row.Scan(&d.ID, &d.Host, &d.BaseURL, &d.MaxPages, &d.RecrawlAfterDays, &d.CrawlStatus); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, pgx.ErrNoRows
		}
		return nil, err
	}
	lease := time.Now().Add(30 * time.Minute)
	_, err = tx.Exec(ctx, `
		UPDATE domains SET crawl_status = 'running',
			crawl_lease_until = $2, crawl_lease_owner = $3,
			last_crawl_started_at = NOW(), updated_at = NOW()
		WHERE id = $1
	`, d.ID, lease, s.owner)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &d, nil
}

func (s *Store) CompleteCrawl(ctx context.Context, domainID int64, recrawlDays int, crawlErr error) error {
	status := "idle"
	var lastErr *string
	if crawlErr != nil {
		status = "failed"
		msg := crawlErr.Error()
		lastErr = &msg
	}
	next := time.Now().Add(time.Duration(recrawlDays) * 24 * time.Hour)
	_, err := s.pool.Exec(ctx, `
		UPDATE domains SET crawl_status = $2::crawl_status,
			crawl_lease_until = NULL, crawl_lease_owner = NULL,
			last_crawl_completed_at = NOW(), next_crawl_at = $3,
			last_error = $4, updated_at = NOW()
		WHERE id = $1
	`, domainID, status, next, lastErr)
	return err
}

func (s *Store) UpsertValidatedURLs(ctx context.Context, domainID int64, source string, results []validate.LinkResult) (int, error) {
	if len(results) == 0 {
		return 0, nil
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)

	inserted := 0
	for _, r := range results {
		norm := urlnorm.NormalizeURL(r.URL)
		if norm == "" {
			norm = r.URL
		}
		tag, err := tx.Exec(ctx, `
			INSERT INTO urls (
				domain_id, url, normalized_url, source, discovered_at,
				link_status, http_code, final_url, validated_at,
				archive_status
			) VALUES (
				$1, $2, $3, $4::url_source, NOW(),
				$5::link_status, $6, $7, NOW(), 'pending'
			)
			ON CONFLICT (normalized_url) DO UPDATE SET
				link_status = EXCLUDED.link_status,
				http_code = EXCLUDED.http_code,
				final_url = EXCLUDED.final_url,
				validated_at = EXCLUDED.validated_at,
				archive_status = CASE
					WHEN urls.archive_status IN ('archived', 'cached') THEN urls.archive_status
					ELSE 'pending'
				END,
				updated_at = NOW()
		`, domainID, r.URL, norm, source, r.Status, r.HTTPCode, nullString(r.FinalURL))
		if err != nil {
			return inserted, err
		}
		inserted += int(tag.RowsAffected())
	}
	if s.notify && inserted > 0 {
		_, _ = tx.Exec(ctx, `SELECT pg_notify('arkivari_pending', $1)`, fmt.Sprint(domainID))
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return inserted, nil
}

func nullString(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func (s *Store) SyncDomain(ctx context.Context, host, baseURL string, enabled bool, maxPages, recrawlDays int, note string) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO domains (host, base_url, enabled, max_pages, recrawl_after_days, note, crawl_status, next_crawl_at)
		VALUES ($1, $2, $3, $4, $5, $6, 'scheduled', NOW())
		ON CONFLICT (host) DO UPDATE SET
			base_url = EXCLUDED.base_url,
			enabled = EXCLUDED.enabled,
			max_pages = EXCLUDED.max_pages,
			recrawl_after_days = EXCLUDED.recrawl_after_days,
			note = EXCLUDED.note,
			updated_at = NOW()
	`, host, baseURL, enabled, maxPages, recrawlDays, note)
	return err
}

func (s *Store) DisableMissingHosts(ctx context.Context, activeHosts map[string]struct{}) error {
	rows, err := s.pool.Query(ctx, `SELECT host FROM domains WHERE enabled = TRUE`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var host string
		if err := rows.Scan(&host); err != nil {
			return err
		}
		if _, ok := activeHosts[host]; !ok {
			_, err := s.pool.Exec(ctx, `UPDATE domains SET enabled = FALSE, updated_at = NOW() WHERE host = $1`, host)
			if err != nil {
				return err
			}
		}
	}
	return rows.Err()
}