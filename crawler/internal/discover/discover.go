package discover

import (
	"context"
	"log"
	"net/http"
	"sort"
	"time"

	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
	"github.com/arkivari/crawler/internal/validate"
)

type discoverOpts struct {
	crawlDelay time.Duration // >0 overrides robots/default after Load
	abortOn429 bool
}

func defaultDiscoverOpts() discoverOpts {
	return discoverOpts{abortOn429: true}
}

type Result struct {
	URLs        []string
	SitemapURLs map[string]struct{}
	Checked     []validate.LinkResult
	Stats       Stats
}

type Stats struct {
	SitemapFound int
	CrawlFound   int
}

func Discover(
	ctx context.Context,
	domain, userAgent string,
	client *http.Client,
	maxPages int,
) (Result, error) {
	return discoverWith(ctx, domain, userAgent, client, maxPages, defaultDiscoverOpts())
}

func discoverWith(
	ctx context.Context,
	domain, userAgent string,
	client *http.Client,
	maxPages int,
	opt discoverOpts,
) (Result, error) {
	base, err := urlnorm.NormalizeDomain(domain)
	if err != nil {
		return Result{}, err
	}
	target := urlnorm.TargetNetloc(base)
	policy := robots.New(base, userAgent, client)
	if err := policy.Load(ctx); err != nil {
		log.Printf("robots load warning for %s: %v", base, err)
	}
	if opt.crawlDelay > 0 {
		policy.SetCrawlDelay(opt.crawlDelay)
	}

	sitemapURLs, _ := FromSitemaps(ctx, policy, target, userAgent, client)
	sitemapList := mapKeysSet(sitemapURLs)
	sort.Strings(sitemapList)

	var sitemapSelected, crawled []string
	unlimited := maxPages <= 0
	if unlimited {
		sitemapSelected = sitemapList
	} else if len(sitemapList) > maxPages {
		sitemapSelected = sitemapList[:maxPages]
	} else {
		sitemapSelected = sitemapList
	}
	crawlBudget := -1
	if !unlimited {
		crawlBudget = maxPages - len(sitemapSelected)
	}
	prefetched := map[string]validate.LinkResult{}
	errorPage := validate.ProbeErrorPage(ctx, base, userAgent, client)
	if crawlBudget != 0 {
		seed := base + "/"
		cr := CrawlSite(ctx, seed, target, userAgent, policy, client, crawlBudget, nil, errorPage, opt.abortOn429)
		crawled = cr.URLs
		for k, v := range cr.Checked {
			prefetched[k] = v
		}
	}

	combined := append(append([]string{}, sitemapSelected...), crawled...)
	unique, _ := dedupe(combined)
	if !unlimited && len(unique) > maxPages {
		unique = unique[:maxPages]
	}
	log.Printf("discovered %d URLs for %s (sitemap %d, crawl %d)", len(unique), base, len(sitemapSelected), len(crawled))

	sitemapSet := make(map[string]struct{}, len(sitemapSelected))
	for _, u := range sitemapSelected {
		sitemapSet[u] = struct{}{}
	}
	checked := validate.CrawlURLs(ctx, base, userAgent, client, policy, unique, prefetched, errorPage, opt.abortOn429)
	return Result{
		URLs:        unique,
		SitemapURLs: sitemapSet,
		Checked:     checked,
		Stats: Stats{
			SitemapFound: len(sitemapSelected),
			CrawlFound:   len(crawled),
		},
	}, nil
}

func mapKeysSet(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func dedupe(urls []string) ([]string, int) {
	seen := make(map[string]struct{})
	var out []string
	dupes := 0
	for _, raw := range urls {
		n := urlnorm.NormalizeURL(raw)
		if n == "" {
			continue
		}
		if _, ok := seen[n]; ok {
			dupes++
			continue
		}
		seen[n] = struct{}{}
		out = append(out, n)
	}
	sort.Strings(out)
	return out, dupes
}