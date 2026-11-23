package discover

import (
	"context"
	"io"
	"log"
	"net/http"
	"sort"
	"sync"

	"github.com/PuerkitoBio/goquery"
	"github.com/arkivari/crawler/internal/config"
	"github.com/arkivari/crawler/internal/httpclient"
	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
	"github.com/arkivari/crawler/internal/validate"
)

// CrawlResult holds URLs found by BFS crawl and GET classification from those fetches.
type CrawlResult struct {
	URLs        []string
	Checked     map[string]validate.LinkResult
	CrawlCapped bool
	Interrupted bool
}

type pageResult struct {
	url   string
	links []string
	ok    bool // HTTP 200 HTML parsed successfully
	check validate.LinkResult
}

// CrawlSite runs concurrent BFS with one coordinator goroutine and a fixed worker pool.
func CrawlSite(
	ctx context.Context,
	seedURL, targetNetloc, userAgent string,
	policy *robots.Policy,
	client *http.Client,
	maxPages int,
	exclude map[string]struct{},
	errorPage string,
	abortOn429 bool,
) CrawlResult {
	if exclude == nil {
		exclude = make(map[string]struct{})
	}
	seed := urlnorm.NormalizeURL(seedURL)
	if seed == "" {
		seed = seedURL
	}

	crawlCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	hc := httpclient.ForCrawl(client, targetNetloc, policy)

	jobs := make(chan string, config.CrawlWorkers)
	results := make(chan pageResult, config.CrawlWorkers*2)

	var wg sync.WaitGroup
	for range config.CrawlWorkers {
		wg.Add(1)
		go crawlWorker(crawlCtx, &wg, jobs, results, userAgent, policy, hc, errorPage)
	}
	go func() {
		wg.Wait()
		close(results)
	}()

	var closeJobs sync.Once
	shutdownWorkers := func() {
		closeJobs.Do(func() { close(jobs) })
	}

	queue := []string{seed}
	seen := make(map[string]struct{})
	discovered := make(map[string]struct{})
	checked := make(map[string]validate.LinkResult)
	unlimited := maxPages <= 0
	inFlight := 0
	capped := false
	interrupted := false

	finish := func() CrawlResult {
		shutdownWorkers()
		for range results {
		}
		return finishCrawl(discovered, checked, capped, interrupted)
	}

	for {
		for len(queue) > 0 && inFlight < config.CrawlWorkers {
			if !unlimited && len(discovered)+inFlight >= maxPages {
				capped = true
				cancel()
				return finish()
			}
			current := queue[0]
			queue = queue[1:]
			normalized := urlnorm.NormalizeURL(current)
			if normalized == "" {
				continue
			}
			if _, dup := seen[normalized]; dup {
				continue
			}
			seen[normalized] = struct{}{}
			if !urlnorm.SameDomain(normalized, targetNetloc) || !policy.CanFetch(normalized) {
				continue
			}
			select {
			case <-crawlCtx.Done():
				return finish()
			case jobs <- normalized:
				inFlight++
			}
		}

		if inFlight == 0 && len(queue) == 0 {
			return finish()
		}

		select {
		case <-ctx.Done():
			interrupted = true
			cancel()
			return finish()
		case res, ok := <-results:
			if !ok {
				return finishCrawl(discovered, checked, capped, interrupted)
			}
			inFlight--
			if res.check.URL != "" {
				checked[res.url] = res.check
			}
			if abortOn429 && res.check.HTTPCode == http.StatusTooManyRequests {
				cancel()
				return finish()
			}
			if res.ok {
				if _, ex := exclude[res.url]; !ex {
					discovered[res.url] = struct{}{}
				}
				for _, link := range res.links {
					if urlnorm.SameDomain(link, targetNetloc) {
						queue = append(queue, link)
					}
				}
			}
			if !unlimited && len(discovered) >= maxPages && (len(queue) > 0 || inFlight > 0) {
				capped = true
				cancel()
				return finish()
			}
		}
	}
}

func finishCrawl(discovered map[string]struct{}, checked map[string]validate.LinkResult, capped, interrupted bool) CrawlResult {
	if capped {
		log.Printf("crawl capped (%d URLs discovered by crawl)", len(discovered))
	}
	return CrawlResult{
		URLs:        sortedKeys(discovered),
		Checked:     checked,
		CrawlCapped: capped,
		Interrupted: interrupted,
	}
}

func crawlWorker(
	ctx context.Context,
	wg *sync.WaitGroup,
	jobs <-chan string,
	results chan<- pageResult,
	userAgent string,
	policy *robots.Policy,
	client *http.Client,
	errorPage string,
) {
	defer wg.Done()
	for pageURL := range jobs {
		if ctx.Err() != nil {
			return
		}
		res := fetchPage(ctx, pageURL, userAgent, policy, client, errorPage)
		select {
		case results <- res:
		case <-ctx.Done():
			return
		}
	}
}

func fetchPage(
	ctx context.Context,
	pageURL, userAgent string,
	policy *robots.Policy,
	client *http.Client,
	errorPage string,
) pageResult {
	policy.WaitForCrawl()
	statusCode := 0
	defer func() { policy.CrawlDone(statusCode) }()
	reqCtx, cancel := context.WithTimeout(ctx, config.CrawlTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, pageURL, nil)
	if err != nil {
		return pageResult{url: pageURL}
	}
	req.Header.Set("User-Agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		return pageResult{
			url:   pageURL,
			check: validate.LinkResult{URL: pageURL, Status: "network_error", Error: err.Error()},
		}
	}
	defer resp.Body.Close()
	statusCode = resp.StatusCode
	final := pageURL
	if resp.Request != nil && resp.Request.URL != nil {
		final = resp.Request.URL.String()
	}
	check := validate.ResultFromGET(pageURL, statusCode, final, errorPage)
	if resp.StatusCode != http.StatusOK || !isHTML(resp.Header.Get("Content-Type")) {
		_, _ = io.CopyN(io.Discard, resp.Body, config.MaxResponseBodyBytes)
		return pageResult{url: pageURL, check: check}
	}
	body := io.LimitReader(resp.Body, config.MaxResponseBodyBytes)
	doc, err := goquery.NewDocumentFromReader(body)
	if err != nil {
		return pageResult{url: pageURL, check: check}
	}
	var links []string
	doc.Find("a[href]").Each(func(_ int, s *goquery.Selection) {
		href, ok := s.Attr("href")
		if !ok {
			return
		}
		full := ResolveLink(pageURL, href)
		if full == "" {
			return
		}
		if normalized := urlnorm.NormalizeURL(full); normalized != "" {
			links = append(links, normalized)
		}
	})
	return pageResult{url: pageURL, links: links, ok: true, check: check}
}

func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}