package discover

import (
	"context"
	"log"
	"net/http"
	"strings"

	"github.com/PuerkitoBio/goquery"
	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
)

var skipSchemes = map[string]struct{}{
	"mailto": {}, "javascript": {}, "tel": {}, "ftp": {}, "data": {},
}

func isHTML(contentType string) bool {
	ct := strings.ToLower(strings.TrimSpace(strings.Split(contentType, ";")[0]))
	return strings.HasPrefix(ct, "text/html") || strings.HasPrefix(ct, "application/xhtml+xml")
}

type CrawlResult struct {
	URLs        []string
	CrawlCapped bool
}

func CrawlSite(
	ctx context.Context,
	seedURL, targetNetloc, userAgent string,
	policy *robots.Policy,
	client *http.Client,
	maxPages int,
	exclude map[string]struct{},
) CrawlResult {
	unlimited := maxPages <= 0
	discovered := make(map[string]struct{})
	seed := urlnorm.NormalizeURL(seedURL)
	if seed == "" {
		seed = seedURL
	}
	queue := []string{seed}
	seen := make(map[string]struct{})
	crawlCapped := false

	for len(queue) > 0 {
		if !unlimited && len(discovered) >= maxPages {
			crawlCapped = true
			break
		}
		current := queue[0]
		queue = queue[1:]
		if current == "" {
			continue
		}
		if _, ok := seen[current]; ok {
			continue
		}
		seen[current] = struct{}{}
		if !urlnorm.SameDomain(current, targetNetloc) || !policy.CanFetch(current) {
			continue
		}
		select {
		case <-ctx.Done():
			return CrawlResult{URLs: mapKeys(discovered), CrawlCapped: crawlCapped}
		default:
		}
		policy.WaitForCrawl()
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, current, nil)
		if err != nil {
			continue
		}
		req.Header.Set("User-Agent", userAgent)
		resp, err := client.Do(req)
		if err != nil {
			continue
		}
		if resp.StatusCode != http.StatusOK || !isHTML(resp.Header.Get("Content-Type")) {
			resp.Body.Close()
			continue
		}
		doc, err := goquery.NewDocumentFromReader(resp.Body)
		resp.Body.Close()
		if err != nil {
			continue
		}
		if _, ex := exclude[current]; !ex {
			discovered[current] = struct{}{}
		}
		doc.Find("a[href]").Each(func(_ int, s *goquery.Selection) {
			href, ok := s.Attr("href")
			if !ok {
				return
			}
			href = strings.TrimSpace(href)
			if href == "" || strings.HasPrefix(href, "#") {
				return
			}
			if idx := strings.Index(href, ":"); idx > 0 {
				scheme := strings.ToLower(href[:idx])
				if _, skip := skipSchemes[scheme]; skip {
					return
				}
			}
			full := joinURL(current, href)
			link := urlnorm.NormalizeURL(full)
			if link == "" || !urlnorm.SameDomain(link, targetNetloc) {
				return
			}
			if _, ok := seen[link]; !ok {
				queue = append(queue, link)
			}
		})
	}
	if crawlCapped {
		log.Printf("crawl capped at %d pages", maxPages)
	}
	return CrawlResult{URLs: mapKeys(discovered), CrawlCapped: crawlCapped}
}

func mapKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func joinURL(base, href string) string {
	if strings.HasPrefix(href, "http://") || strings.HasPrefix(href, "https://") {
		return href
	}
	if strings.HasPrefix(href, "//") {
		return "https:" + href
	}
	if strings.HasPrefix(href, "/") {
		return strings.TrimRight(base, "/") + href
	}
	last := strings.LastIndex(base, "/")
	if last >= 0 {
		return base[:last+1] + href
	}
	return base + "/" + href
}