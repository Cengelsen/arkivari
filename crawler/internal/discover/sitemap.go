package discover

import (
	"encoding/xml"
	"io"
	"log"
	"net/http"
	"strings"

	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
)

const sitemapTimeout = 60

type urlSet struct {
	URLs []struct {
		Loc string `xml:"loc"`
	} `xml:"url"`
	Sitemaps []struct {
		Loc string `xml:"loc"`
	} `xml:"sitemap"`
}

func ParseSitemap(
	sitemapURL, targetNetloc, userAgent string,
	client *http.Client,
	policy *robots.Policy,
	depth, maxDepth int,
	failures *[]string,
) map[string]struct{} {
	if depth > maxDepth {
		return nil
	}
	if !policy.CanFetch(sitemapURL) {
		*failures = append(*failures, sitemapURL+": disallowed by robots.txt")
		return nil
	}
	policy.WaitForCrawl()
	req, _ := http.NewRequest(http.MethodGet, sitemapURL, nil)
	req.Header.Set("User-Agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		*failures = append(*failures, sitemapURL+": "+err.Error())
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		*failures = append(*failures, sitemapURL+": HTTP "+resp.Status)
		return nil
	}
	body, err := io.ReadAll(resp.Body)
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
		nested := ParseSitemap(loc, targetNetloc, userAgent, client, policy, depth+1, maxDepth, failures)
		for u := range nested {
			out[u] = struct{}{}
		}
	}
	for _, u := range doc.URLs {
		candidate := urlnorm.NormalizeURL(strings.TrimSpace(u.Loc))
		if candidate != "" && urlnorm.SameDomain(candidate, targetNetloc) {
			out[candidate] = struct{}{}
		}
	}
	if len(out) > 0 {
		log.Printf("sitemap %s: %d URLs", sitemapURL, len(out))
	}
	return out
}

func FromSitemaps(policy *robots.Policy, targetNetloc, userAgent string, client *http.Client) (map[string]struct{}, []string) {
	urls := make(map[string]struct{})
	var failures []string
	for _, sm := range policy.DefaultSitemapURLs() {
		found := ParseSitemap(sm, targetNetloc, userAgent, client, policy, 0, 5, &failures)
		for u := range found {
			urls[u] = struct{}{}
		}
	}
	return urls, failures
}