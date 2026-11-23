package robots

import (
	"context"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/arkivari/crawler/internal/config"
	"github.com/temoto/robotstxt"
)

type Policy struct {
	baseURL    string
	userAgent  string
	client     *http.Client
	mu         sync.Mutex
	lastStart  time.Time
	cooldown   time.Time
	crawlDelay time.Duration
	slots      chan struct{}
	data       *robotstxt.RobotsData
	group      *robotstxt.Group
	fetched    bool
}

func New(baseURL, userAgent string, client *http.Client) *Policy {
	if client == nil {
		client = http.DefaultClient
	}
	max := config.OriginMaxConcurrency
	if max < 1 {
		max = 1
	}
	return &Policy{
		baseURL:    strings.TrimRight(baseURL, "/"),
		userAgent:  userAgent,
		client:     client,
		slots:      make(chan struct{}, max),
		crawlDelay: config.DefaultCrawlDelay,
	}
}

func (p *Policy) Load(ctx context.Context) error {
	robotsURL := p.baseURL + "/robots.txt"
	reqCtx, cancel := context.WithTimeout(ctx, config.ValidationTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, robotsURL, nil)
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
	body, err := io.ReadAll(io.LimitReader(resp.Body, config.MaxRobotsBodyBytes))
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
	if p.group != nil && p.group.CrawlDelay > 0 {
		p.crawlDelay = p.group.CrawlDelay
	}
	p.fetched = true
	return nil
}

// Load without context uses Background (tests).
func (p *Policy) loadLegacy() error {
	return p.Load(context.Background())
}

func (p *Policy) ensureLoaded() {
	if !p.fetched {
		_ = p.loadLegacy()
	}
}

func (p *Policy) WaitForCrawl() {
	p.slots <- struct{}{}
	for {
		p.mu.Lock()
		now := time.Now()
		var wait time.Duration
		if now.Before(p.cooldown) {
			wait = p.cooldown.Sub(now)
		} else if p.crawlDelay > 0 {
			if w := p.crawlDelay - now.Sub(p.lastStart); w > 0 {
				wait = w
			}
		}
		if wait <= 0 {
			p.lastStart = now
			p.mu.Unlock()
			return
		}
		p.mu.Unlock()
		time.Sleep(wait)
	}
}

// CrawlDone releases an origin slot; shared cooldown on rate-limit responses.
func (p *Policy) CrawlDone(statusCode int) {
	if statusCode == 429 || statusCode == 502 || statusCode == 503 {
		p.mu.Lock()
		until := time.Now().Add(config.OriginBackoff)
		if until.After(p.cooldown) {
			p.cooldown = until
		}
		p.mu.Unlock()
	}
	<-p.slots
}

func (p *Policy) CanFetch(rawURL string) bool {
	p.ensureLoaded()
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
	p.ensureLoaded()
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
	add(p.baseURL + "/sitemap.xml")
	return out
}

// SetCrawlDelayForTest adjusts robots crawl-delay in unit tests.
func (p *Policy) SetCrawlDelayForTest(d time.Duration) {
	p.SetCrawlDelay(d)
}

// SetCrawlDelay overrides crawl spacing between origin requests.
func (p *Policy) SetCrawlDelay(d time.Duration) {
	p.mu.Lock()
	p.crawlDelay = d
	p.mu.Unlock()
}