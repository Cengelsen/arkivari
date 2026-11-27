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
	url             string
	links           []string
	ok              bool // HTTP 200 HTML parsed successfully
	isDocument      bool // HTTP 200 allowlisted file (for archive queue)
	crossOriginOnly bool // job was a cross-origin document probe (no foreign HTML BFS)
	check           validate.LinkResult
}

type crawlJob struct {
	url         string
	crossOrigin bool // linked from site HTML; verify file, do not BFS foreign HTML
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

	jobs := make(chan crawlJob, config.CrawlWorkers)
	results := make(chan pageResult, config.CrawlWorkers*2)

	var wg sync.WaitGroup
	for range config.CrawlWorkers {
		wg.Add(1)
		go crawlWorker(crawlCtx, &wg, jobs, results, userAgent, policy, client, hc, errorPage)
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
	crossDocQueue := []string{}
	seen := make(map[string]struct{})
	discovered := make(map[string]struct{})
	checked := make(map[string]validate.LinkResult)
	unlimited := maxPages <= 0
	inFlight := 0
	capped := false
	interrupted := false

	enqueueCrossDocument := func(raw string) {
		normalized := urlnorm.NormalizeURL(raw)
		if normalized == "" {
			return
		}
		if urlnorm.SameDomain(normalized, targetNetloc) {
			return
		}
		if _, ok := seen[normalized]; ok {
			return
		}
		crossDocQueue = append(crossDocQueue, normalized)
	}

	dispatch := func(url string, crossOrigin bool) bool {
		normalized := urlnorm.NormalizeURL(url)
		if normalized == "" {
			return false
		}
		if _, dup := seen[normalized]; dup {
			return false
		}
		seen[normalized] = struct{}{}
		if !crossOrigin {
			if !urlnorm.SameDomain(normalized, targetNetloc) || !policy.CanFetch(normalized) {
				delete(seen, normalized)
				return false
			}
		}
		select {
		case <-crawlCtx.Done():
			delete(seen, normalized)
			return false
		case jobs <- crawlJob{url: normalized, crossOrigin: crossOrigin}:
			inFlight++
			return true
		}
	}

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
			if !dispatch(current, false) {
				continue
			}
		}
		for len(crossDocQueue) > 0 && inFlight < config.CrawlWorkers {
			if !unlimited && len(discovered)+inFlight >= maxPages {
				capped = true
				cancel()
				return finish()
			}
			current := crossDocQueue[0]
			crossDocQueue = crossDocQueue[1:]
			if !dispatch(current, true) {
				continue
			}
		}

		if inFlight == 0 && len(queue) == 0 && len(crossDocQueue) == 0 {
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
			if res.isDocument || (res.ok && !res.crossOriginOnly) {
				if _, ex := exclude[res.url]; !ex {
					discovered[res.url] = struct{}{}
				}
			}
			if res.ok && !res.crossOriginOnly {
				for _, link := range res.links {
					if urlnorm.SameDomain(link, targetNetloc) {
						queue = append(queue, link)
						continue
					}
					enqueueCrossDocument(link)
				}
			}
			if !unlimited && len(discovered) >= maxPages && (len(queue) > 0 || len(crossDocQueue) > 0 || inFlight > 0) {
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
	jobs <-chan crawlJob,
	results chan<- pageResult,
	userAgent string,
	policy *robots.Policy,
	plainClient, siteClient *http.Client,
	errorPage string,
) {
	defer wg.Done()
	for job := range jobs {
		if ctx.Err() != nil {
			return
		}
		httpClient := siteClient
		if job.crossOrigin {
			httpClient = plainClient
		}
		res := fetchPage(ctx, job.url, userAgent, policy, httpClient, errorPage, job.crossOrigin)
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
	crossOrigin bool,
) pageResult {
	res := pageResult{url: pageURL, crossOriginOnly: crossOrigin}
	statusCode := 0
	if !crossOrigin {
		policy.WaitForCrawl()
		defer func() { policy.CrawlDone(statusCode) }()
	}
	reqCtx, cancel := context.WithTimeout(ctx, config.CrawlTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, pageURL, nil)
	if err != nil {
		return res
	}
	req.Header.Set("User-Agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		res.check = validate.LinkResult{URL: pageURL, Status: "network_error", Error: err.Error()}
		return res
	}
	defer resp.Body.Close()
	statusCode = resp.StatusCode
	final := pageURL
	if resp.Request != nil && resp.Request.URL != nil {
		final = resp.Request.URL.String()
	}
	check := validate.ResultFromGET(pageURL, statusCode, final, errorPage)
	ctype := resp.Header.Get("Content-Type")
	if resp.StatusCode != http.StatusOK || !isHTML(ctype) {
		var prefix []byte
		if resp.StatusCode == http.StatusOK {
			buf := make([]byte, documentSniffLen)
			n, _ := io.ReadFull(io.LimitReader(resp.Body, documentSniffLen), buf)
			if n > 0 {
				prefix = buf[:n]
			}
			_, _ = io.CopyN(io.Discard, resp.Body, config.MaxResponseBodyBytes)
			if ResponseIsDocument(ctype, resp.Header.Get("Content-Disposition"), prefix) {
				res.isDocument = true
				res.check = check
				return res
			}
		} else {
			_, _ = io.CopyN(io.Discard, resp.Body, config.MaxResponseBodyBytes)
		}
		res.check = check
		return res
	}
	body := io.LimitReader(resp.Body, config.MaxResponseBodyBytes)
	doc, err := goquery.NewDocumentFromReader(body)
	if err != nil {
		res.check = check
		return res
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
	res.links = links
	res.ok = true
	res.check = check
	return res
}

func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}