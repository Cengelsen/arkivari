// Package crawl is the production path: discover URLs, GET each for status, store includable rows.
package crawl

import (
	"context"
	"fmt"
	"net/http"

	"github.com/arkivari/crawler/internal/discover"
	"github.com/arkivari/crawler/internal/httpclient"
	"github.com/arkivari/crawler/internal/pipeline"
	"github.com/arkivari/crawler/internal/store"
	"github.com/arkivari/crawler/internal/validate"
)

// Runner holds shared resources for domain crawl sessions (daemon and tests).
type Runner struct {
	Store     *store.Store
	UserAgent string
	Client    *http.Client
}

// Result summarizes one completed crawl session.
type Result struct {
	Discovery discover.Result
	Persist   pipeline.PersistStats
	Throttle  validate.ThrottleSummary
}

// URLsDiscovered is the number of unique URLs after discover dedupe.
func (r Result) URLsDiscovered() int {
	return len(r.Discovery.URLs)
}

// Run executes discover → crawl-check → batched DB upsert for one domain lease.
func (r *Runner) Run(ctx context.Context, domainID int64, baseURL string, maxPages int) (Result, error) {
	if r.Store == nil {
		return Result{}, fmt.Errorf("crawl: store is required")
	}
	client := r.Client
	if client == nil {
		client = httpclient.Default()
	}
	ua := r.UserAgent
	if ua == "" {
		ua = "arkivari-crawler/1.0"
	}

	disc, err := discover.Discover(ctx, baseURL, ua, client, maxPages)
	if err != nil {
		return Result{}, err
	}
	out := Result{
		Discovery: disc,
		Throttle:  validate.ThrottleCounts(disc.Checked),
	}
	if len(disc.Checked) == 0 {
		return out, nil
	}
	pstats, err := pipeline.PersistDiscovery(ctx, r.Store, domainID, disc)
	out.Persist = pstats
	return out, err
}

// RunAcquired runs a crawl for a row returned from store.AcquireDomain.
func (r *Runner) RunAcquired(ctx context.Context, d *store.DomainRow) (Result, error) {
	if d == nil {
		return Result{}, fmt.Errorf("crawl: domain row is nil")
	}
	return r.Run(ctx, d.ID, d.BaseURL, d.MaxPages)
}