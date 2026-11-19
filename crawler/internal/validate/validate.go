package validate

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
)

const (
	maxRetries    = 3
	timeout       = 20 * time.Second
	workers       = 5
)

var includable = map[string]struct{}{
	"ok":            {},
	"redirected":    {},
	"forbidden":     {},
	"auth_required": {},
}

type LinkResult struct {
	URL        string
	Status     string
	HTTPCode   int
	FinalURL   string
	Error      string
}

func FilterIncludable(ctx context.Context, baseURL, userAgent string, client *http.Client, policy *robots.Policy, urls []string) []LinkResult {
	if len(urls) == 0 {
		return nil
	}
	errorPage := probeErrorPage(ctx, baseURL, userAgent, client)
	jobs := make(chan string)
	results := make(chan LinkResult, len(urls))
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for u := range jobs {
				policy.WaitForCrawl()
				results <- checkLink(ctx, u, userAgent, client, errorPage)
			}
		}()
	}
	go func() {
		wg.Wait()
		close(results)
	}()
	go func() {
		for _, u := range urls {
			select {
			case <-ctx.Done():
				close(jobs)
				return
			case jobs <- u:
			}
		}
		close(jobs)
	}()
	var out []LinkResult
	for r := range results {
		if _, ok := includable[r.Status]; ok {
			out = append(out, r)
		}
	}
	return out
}

func probeErrorPage(ctx context.Context, baseURL, userAgent string, client *http.Client) string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	token := hex.EncodeToString(b[:])
	probe := strings.TrimRight(baseURL, "/") + "/arkivari-probe-404-" + token
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, probe, nil)
	req.Header.Set("User-Agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return ""
	}
	if resp.StatusCode >= 200 && resp.StatusCode < 300 && resp.Request.URL.String() != probe {
		return normalizeFinal(resp.Request.URL.String())
	}
	return ""
}

func normalizeFinal(raw string) string {
	n := urlnorm.NormalizeURL(raw)
	if n == "" {
		return raw
	}
	return n
}

func checkLink(ctx context.Context, u, userAgent string, client *http.Client, errorPage string) LinkResult {
	backoff := 2.0
	var lastErr string
	for attempt := 1; attempt <= maxRetries; attempt++ {
		code, final, err := doRequest(ctx, http.MethodHead, u, userAgent, client)
		if err != nil {
			lastErr = err.Error()
			if attempt < maxRetries {
				time.Sleep(time.Duration(backoff) * time.Second)
				backoff *= 2
				continue
			}
			return LinkResult{URL: u, Status: "network_error", Error: lastErr}
		}
		if code == 405 || code == 501 {
			code, final, err = doRequest(ctx, http.MethodGet, u, userAgent, client)
			if err != nil {
				lastErr = err.Error()
				continue
			}
		}
		if code == 429 || code == 502 || code == 503 {
			lastErr = fmt.Sprintf("HTTP %d", code)
			if attempt < maxRetries {
				time.Sleep(time.Duration(backoff) * time.Second)
				backoff *= 2
				continue
			}
			return LinkResult{URL: u, Status: classify(code), HTTPCode: code, FinalURL: final, Error: lastErr}
		}
		status := classify(code)
		if status == "ok" && final != "" && final != u {
			if errorPage != "" && normalizeFinal(final) == errorPage {
				return LinkResult{URL: u, Status: "soft_404", HTTPCode: code, FinalURL: final}
			}
			return LinkResult{URL: u, Status: "redirected", HTTPCode: code, FinalURL: final}
		}
		return LinkResult{URL: u, Status: status, HTTPCode: code, FinalURL: final}
	}
	return LinkResult{URL: u, Status: "network_error", Error: lastErr}
}

func doRequest(ctx context.Context, method, u, userAgent string, client *http.Client) (int, string, error) {
	req, err := http.NewRequestWithContext(ctx, method, u, nil)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("User-Agent", userAgent)
	c := client
	if c == nil {
		c = &http.Client{Timeout: timeout}
	}
	resp, err := c.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	final := u
	if resp.Request != nil && resp.Request.URL != nil {
		final = resp.Request.URL.String()
	}
	return resp.StatusCode, final, nil
}

func classify(code int) string {
	switch {
	case code >= 200 && code < 300:
		return "ok"
	case code == 401:
		return "auth_required"
	case code == 403:
		return "forbidden"
	case code == 404:
		return "not_found"
	case code == 410:
		return "gone"
	case code == 429:
		return "rate_limited"
	case code >= 500:
		return "server_error"
	default:
		return "server_error"
	}
}