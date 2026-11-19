package discover

import (
	"context"
	"log"
	"net/http"
	"sort"

	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
)

type Result struct {
	URLs   []string
	Stats  Stats
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
	base, err := urlnorm.NormalizeDomain(domain)
	if err != nil {
		return Result{}, err
	}
	target := urlnorm.TargetNetloc(base)
	policy := robots.New(base, userAgent, client)
	if err := policy.Load(); err != nil {
		log.Printf("robots load warning for %s: %v", base, err)
	}

	sitemapURLs, _ := FromSitemaps(policy, target, userAgent, client)
	sitemapList := mapKeysSet(sitemapURLs)
	sort.Strings(sitemapList)

	var sitemapSelected, crawled []string
	unlimited := maxPages <= 0
	if unlimited {
		sitemapSelected = sitemapList
	} else {
		if len(sitemapList) > maxPages {
			sitemapSelected = sitemapList[:maxPages]
		} else {
			sitemapSelected = sitemapList
		}
	}
	crawlBudget := -1
	if !unlimited {
		crawlBudget = maxPages - len(sitemapSelected)
	}
	exclude := make(map[string]struct{})
	for _, u := range sitemapSelected {
		exclude[u] = struct{}{}
	}
	if crawlBudget != 0 {
		seed := base + "/"
		cr := CrawlSite(ctx, seed, target, userAgent, policy, client, crawlBudget, exclude)
		crawled = cr.URLs
	}

	combined := append(append([]string{}, sitemapSelected...), crawled...)
	unique, _ := dedupe(combined)
	if !unlimited && len(unique) > maxPages {
		unique = unique[:maxPages]
	}
	log.Printf("discovered %d URLs for %s (sitemap %d, crawl %d)", len(unique), base, len(sitemapSelected), len(crawled))
	return Result{
		URLs: unique,
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