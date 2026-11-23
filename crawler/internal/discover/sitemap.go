package discover

import (
	"context"
	"encoding/xml"
	"io"
	"log"
	"net/http"
	"strings"

	"github.com/arkivari/crawler/internal/config"
	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
)

type urlSet struct {
	URLs []struct {
		Loc string `xml:"loc"`
	} `xml:"url"`
	Sitemaps []struct {
		Loc string `xml:"loc"`
	} `xml:"sitemap"`
}

func ParseSitemap(
	ctx context.Context,
	sitemapURL, targetNetloc, userAgent string,
	client *http.Client,
	policy *robots.Policy,
	visited map[string]struct{},
	depth, maxDepth int,
	failures *[]string,
) map[string]struct{} {
	if depth > maxDepth {
		return nil
	}
	if _, done := visited[sitemapURL]; done {
		return nil
	}
	visited[sitemapURL] = struct{}{}

	if !policy.CanFetch(sitemapURL) {
		*failures = append(*failures, sitemapURL+": disallowed by robots.txt")
		return nil
	}
	policy.WaitForCrawl()
	statusCode := 0
	defer func() { policy.CrawlDone(statusCode) }()
	reqCtx, cancel := context.WithTimeout(ctx, config.SitemapTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, sitemapURL, nil)
	if err != nil {
		*failures = append(*failures, sitemapURL+": "+err.Error())
		return nil
	}
	req.Header.Set("User-Agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		*failures = append(*failures, sitemapURL+": "+err.Error())
		return nil
	}
	defer resp.Body.Close()
	statusCode = resp.StatusCode
	if resp.StatusCode != http.StatusOK {
		*failures = append(*failures, sitemapURL+": HTTP "+resp.Status)
		return nil
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, config.MaxResponseBodyBytes))
	if err != nil {
		*failures = append(*failures, sitemapURL+": "+err.Error())
		return nil
	}
	var doc urlSet
	if err := xml.Unmarshal(body, &doc); err != nil {
		*failures = append(*failures, sitemapURL+": parse error")
		return nil
	}

	out := make(map[string]struct{})
	for _, sm := range doc.Sitemaps {
		loc := strings.TrimSpace(sm.Loc)
		if loc == "" {
			continue
		}
		nested := ParseSitemap(ctx, loc, targetNetloc, userAgent, client, policy, visited, depth+1, maxDepth, failures)
		for u := range nested {
			out[u] = struct{}{}
		}
	}
	pageCount := 0
	for _, u := range doc.URLs {
		candidate := urlnorm.NormalizeURL(strings.TrimSpace(u.Loc))
		if candidate != "" && urlnorm.SameDomain(candidate, targetNetloc) {
			out[candidate] = struct{}{}
			pageCount++
		}
	}
	if pageCount > 0 {
		log.Printf("sitemap %s: %d URLs", sitemapURL, pageCount)
	}
	return out
}

func FromSitemaps(
	ctx context.Context,
	policy *robots.Policy,
	targetNetloc, userAgent string,
	client *http.Client,
) (map[string]struct{}, []string) {
	urls := make(map[string]struct{})
	var failures []string
	visited := make(map[string]struct{})
	for _, sm := range policy.DefaultSitemapURLs() {
		found := ParseSitemap(
			ctx, sm, targetNetloc, userAgent, client, policy, visited,
			0, config.SitemapMaxDepth, &failures,
		)
		for u := range found {
			urls[u] = struct{}{}
		}
	}
	return urls, failures
}