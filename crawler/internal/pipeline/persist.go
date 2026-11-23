package pipeline

import (
	"context"

	"github.com/arkivari/crawler/internal/config"
	"github.com/arkivari/crawler/internal/discover"
	"github.com/arkivari/crawler/internal/store"
	"github.com/arkivari/crawler/internal/urlnorm"
	"github.com/arkivari/crawler/internal/validate"
)

// PersistStats holds counts from storing crawl-as-validation results.
type PersistStats struct {
	Validated         int
	StoredRows        int
	ValidationBatches int
}

// PersistDiscovery stores includable rows from discovery crawl checks (no separate validation pass).
func PersistDiscovery(
	ctx context.Context,
	st *store.Store,
	domainID int64,
	result discover.Result,
) (PersistStats, error) {
	var stats PersistStats
	checked := result.Checked
	if len(checked) == 0 {
		return stats, nil
	}
	var includable []validate.LinkResult
	for _, r := range checked {
		if validate.Includable(r) {
			includable = append(includable, r)
		}
	}
	stats.Validated = len(includable)
	for i := 0; i < len(includable); i += config.ValidationBatchSize {
		end := i + config.ValidationBatchSize
		if end > len(includable) {
			end = len(includable)
		}
		chunk := includable[i:end]
		stats.ValidationBatches++
		bySource := splitBySource(chunk, result.SitemapURLs)
		for source, rows := range bySource {
			if len(rows) == 0 {
				continue
			}
			n, err := st.UpsertValidatedURLs(ctx, domainID, source, rows)
			if err != nil {
				return stats, err
			}
			stats.StoredRows += n
		}
	}
	return stats, nil
}

func splitBySource(rows []validate.LinkResult, sitemap map[string]struct{}) map[string][]validate.LinkResult {
	out := map[string][]validate.LinkResult{
		"sitemap": {},
		"crawl":   {},
	}
	for _, r := range rows {
		n := urlnorm.NormalizeURL(r.URL)
		if n == "" {
			n = r.URL
		}
		if _, ok := sitemap[n]; ok {
			out["sitemap"] = append(out["sitemap"], r)
		} else {
			out["crawl"] = append(out["crawl"], r)
		}
	}
	return out
}