package validate

import (
	"context"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"

	"github.com/arkivari/crawler/internal/config"
	"github.com/arkivari/crawler/internal/robots"
)

// CrawlURLs fetches each URL with GET and classifies the response (crawl-as-validation).
// Entries in prefetched are reused (e.g. from BFS) to avoid duplicate GETs.
// When abortOn429 is true, stops immediately when any request receives HTTP 429.
func CrawlURLs(
	ctx context.Context,
	baseURL, userAgent string,
	client *http.Client,
	policy *robots.Policy,
	urls []string,
	prefetched map[string]LinkResult,
	errorPage string,
	abortOn429 bool,
) []LinkResult {
	if len(urls) == 0 {
		return nil
	}
	if prefetched == nil {
		prefetched = map[string]LinkResult{}
	}
	if errorPage == "" {
		errorPage = probeErrorPage(ctx, baseURL, userAgent, client)
	}

	out := make([]LinkResult, 0, len(urls))
	var pending []string
	for _, u := range urls {
		if r, ok := prefetched[u]; ok {
			out = append(out, r)
			continue
		}
		pending = append(pending, u)
	}
	if len(pending) == 0 {
		return out
	}

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var stop429 sync.Once
	maybeAbort429 := func(code int) {
		if abortOn429 && code == http.StatusTooManyRequests {
			stop429.Do(cancel)
		}
	}

	jobs := make(chan string, len(pending))
	fetched := make([]LinkResult, len(pending))
	var wg sync.WaitGroup
	var seq atomic.Uint64

	for i := 0; i < config.ValidationWorkers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for u := range jobs {
				if ctx.Err() != nil {
					return
				}
				policy.WaitForCrawl()
				r := checkLinkByGET(ctx, u, userAgent, client, errorPage)
				policy.CrawlDone(r.HTTPCode)
				maybeAbort429(r.HTTPCode)
				i := int(seq.Add(1) - 1)
				if i < len(fetched) {
					fetched[i] = r
				}
			}
		}()
	}

loop:
	for _, u := range pending {
		select {
		case <-ctx.Done():
			break loop
		case jobs <- u:
		}
	}
	close(jobs)
	wg.Wait()

	out = append(out, fetched[:int(seq.Load())]...)
	return out
}

func checkLinkByGET(ctx context.Context, u, userAgent string, client *http.Client, errorPage string) LinkResult {
	if ctx.Err() != nil {
		return LinkResult{URL: u, Status: "network_error", Error: ctx.Err().Error()}
	}
	code, final, err := doRequest(ctx, http.MethodGet, u, userAgent, client)
	if err != nil {
		return LinkResult{URL: u, Status: "network_error", Error: err.Error()}
	}
	if code == 429 || code == 502 || code == 503 {
		return LinkResult{
			URL:      u,
			Status:   classify(code),
			HTTPCode: code,
			FinalURL: final,
			Error:    fmt.Sprintf("HTTP %d", code),
		}
	}
	return ResultFromGET(u, code, final, errorPage)
}

// ResultFromGET maps a GET response to a link check result.
func ResultFromGET(u string, code int, final, errorPage string) LinkResult {
	status := classify(code)
	if status == "ok" && final != "" && final != u {
		if errorPage != "" && normalizeFinal(final) == errorPage {
			return LinkResult{URL: u, Status: "soft_404", HTTPCode: code, FinalURL: final}
		}
		return LinkResult{URL: u, Status: "redirected", HTTPCode: code, FinalURL: final}
	}
	fu := ""
	if final != "" && final != u && (status == "ok" || status == "redirected") {
		fu = final
	}
	return LinkResult{URL: u, Status: status, HTTPCode: code, FinalURL: fu}
}

// Includable reports whether a crawl-check result may be archived.
func Includable(r LinkResult) bool {
	_, ok := includable[r.Status]
	return ok
}