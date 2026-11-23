package scheduler

import (
	"context"
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/arkivari/crawler/internal/crawl"
	"github.com/arkivari/crawler/internal/store"
	"github.com/jackc/pgx/v5"
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

	runner := &crawl.Runner{
		Store:     s.Store,
		UserAgent: s.UserAgent,
		Client:    s.Client,
	}
	res, err := runner.RunAcquired(crawlCtx, domain)
	var crawlErr error
	if err != nil {
		crawlErr = err
	} else if res.URLsDiscovered() == 0 {
		log.Printf("no URLs discovered for %s", domain.Host)
	} else {
		log.Printf(
			"stored %d rows for %s (discovered=%d validated=%d batches=%d throttle=%d)",
			res.Persist.StoredRows,
			domain.Host,
			res.URLsDiscovered(),
			res.Persist.Validated,
			res.Persist.ValidationBatches,
			res.Throttle.Total,
		)
	}

	if err := s.Store.CompleteCrawl(ctx, domain.ID, domain.RecrawlAfterDays, crawlErr); err != nil {
		return err
	}
	return crawlErr
}

func (s *Scheduler) acquireOrIdle(ctx context.Context) error {
	err := s.RunOnce(ctx)
	if err != nil && isPgNoRows(err) {
		return errNoDomain
	}
	return err
}

var errNoDomain = errors.New("no domain to crawl")

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
				case <-time.After(30 * time.Second):
				}
				continue
			}
			log.Printf("crawl error: %v", err)
			time.Sleep(10 * time.Second)
		}
	}
}