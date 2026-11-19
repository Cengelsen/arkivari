package scheduler

import (
	"context"
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/arkivari/crawler/internal/discover"
	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/store"
	"github.com/arkivari/crawler/internal/validate"
)

type Scheduler struct {
	Store     *store.Store
	UserAgent string
	Client    *http.Client
}

func (s *Scheduler) RunOnce(ctx context.Context) error {
	domain, err := s.Store.AcquireDomain(ctx)
	if err != nil {
		return err
	}
	log.Printf("crawling domain %s (id=%d)", domain.Host, domain.ID)

	crawlCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	result, err := discover.Discover(crawlCtx, domain.BaseURL, s.UserAgent, s.Client, domain.MaxPages)
	var crawlErr error
	if err != nil {
		crawlErr = err
	} else if len(result.URLs) > 0 {
		policy := robots.New(domain.BaseURL, s.UserAgent, s.Client)
		_ = policy.Load()
		const batch = 50
		for i := 0; i < len(result.URLs); i += batch {
			end := i + batch
			if end > len(result.URLs) {
				end = len(result.URLs)
			}
			chunk := result.URLs[i:end]
			validated := validate.FilterIncludable(crawlCtx, domain.BaseURL, s.UserAgent, s.Client, policy, chunk)
			source := "sitemap"
			if i >= result.Stats.SitemapFound {
				source = "crawl"
			}
			n, err := s.Store.UpsertValidatedURLs(crawlCtx, domain.ID, source, validated)
			if err != nil {
				crawlErr = err
				break
			}
			log.Printf("stored %d validated URLs for %s", n, domain.Host)
		}
	} else {
		log.Printf("no URLs discovered for %s", domain.Host)
	}

	if err := s.Store.CompleteCrawl(ctx, domain.ID, domain.RecrawlAfterDays, crawlErr); err != nil {
		return err
	}
	if crawlErr != nil {
		return crawlErr
	}
	return nil
}

func IsNoWork(err error) bool {
	return err != nil && errors.Is(err, errNoDomain)
}

var errNoDomain = errors.New("no domain to crawl")

func (s *Scheduler) acquireOrIdle(ctx context.Context) error {
	err := s.RunOnce(ctx)
	if err != nil && isPgNoRows(err) {
		return errNoDomain
	}
	return err
}

func isPgNoRows(err error) bool {
	return errors.Is(err, pgx.ErrNoRows)
}

func (s *Scheduler) Loop(ctx context.Context, syncFn func() error, syncInterval time.Duration) {
	ticker := time.NewTicker(syncInterval)
	defer ticker.Stop()
	for {
		if syncFn != nil {
			if err := syncFn(); err != nil {
				log.Printf("domain sync: %v", err)
			}
		}
		if err := s.acquireOrIdle(ctx); err != nil {
			if errors.Is(err, errNoDomain) {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
					continue
				case <-time.After(30 * time.Second):
					continue
				}
			} else {
				log.Printf("crawl error: %v", err)
				time.Sleep(10 * time.Second)
			}
		}
	}
}