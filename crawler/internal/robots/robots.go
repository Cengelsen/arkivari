package robots

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/temoto/robotstxt"
)

const defaultCrawlDelay = time.Second

type Policy struct {
	baseURL    string
	userAgent  string
	client     *http.Client
	mu         sync.Mutex
	lastFetch  time.Time
	crawlDelay time.Duration
	data       *robotstxt.RobotsData
	group      *robotstxt.Group
	fetched    bool
}

func New(baseURL, userAgent string, client *http.Client) *Policy {
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	return &Policy{
		baseURL:    strings.TrimRight(baseURL, "/"),
		userAgent:  userAgent,
		client:     client,
		crawlDelay: defaultCrawlDelay,
	}
}

func (p *Policy) Load() error {
	robotsURL := p.baseURL + "/robots.txt"
	req, err := http.NewRequest(http.MethodGet, robotsURL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", p.userAgent)
	resp, err := p.client.Do(req)
	if err != nil {
		p.fetched = true
		p.group = nil
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		p.fetched = true
		return nil
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	data, err := robotstxt.FromBytes(body)
	if err != nil {
		p.fetched = true
		return nil
	}
	p.data = data
	p.group = data.FindGroup(p.userAgent)
	if p.group == nil {
		p.group = data.FindGroup("*")
	}
	if p.group != nil {
		if d := p.group.CrawlDelay; d > 0 {
			p.crawlDelay = d
		}
	}
	p.fetched = true
	return nil
}

func (p *Policy) WaitForCrawl() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.lastFetch.IsZero() {
		p.lastFetch = time.Now()
		return
	}
	elapsed := time.Since(p.lastFetch)
	if elapsed < p.crawlDelay {
		time.Sleep(p.crawlDelay - elapsed)
	}
	p.lastFetch = time.Now()
}

func (p *Policy) CanFetch(rawURL string) bool {
	if !p.fetched {
		_ = p.Load()
	}
	if p.group == nil {
		return true
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		return false
	}
	path := u.Path
	if path == "" {
		path = "/"
	}
	return p.group.Test(path)
}

func (p *Policy) DefaultSitemapURLs() []string {
	if !p.fetched {
		_ = p.Load()
	}
	seen := make(map[string]struct{})
	var out []string
	add := func(s string) {
		if s == "" {
			return
		}
		if _, ok := seen[s]; ok {
			return
		}
		seen[s] = struct{}{}
		out = append(out, s)
	}
	if p.data != nil {
		for _, s := range p.data.Sitemaps {
			add(s)
		}
	}
	conventional := p.baseURL + "/sitemap.xml"
	add(conventional)
	return out
}

func (p *Policy) BaseURL() string {
	return p.baseURL
}

func (p *Policy) String() string {
	return fmt.Sprintf("robots{%s}", p.baseURL)
}