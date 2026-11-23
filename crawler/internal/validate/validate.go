package validate

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"io"
	"net/http"
	"strings"

	"github.com/arkivari/crawler/internal/config"
	"github.com/arkivari/crawler/internal/urlnorm"
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

// ProbeErrorPage detects a site-wide error page URL used for soft-404 classification.
func ProbeErrorPage(ctx context.Context, baseURL, userAgent string, client *http.Client) string {
	return probeErrorPage(ctx, baseURL, userAgent, client)
}

func probeErrorPage(ctx context.Context, baseURL, userAgent string, client *http.Client) string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	token := hex.EncodeToString(b[:])
	probe := strings.TrimRight(baseURL, "/") + "/arkivari-probe-404-" + token
	reqCtx, cancel := context.WithTimeout(ctx, config.ValidationTimeout)
	defer cancel()
	req, _ := http.NewRequestWithContext(reqCtx, http.MethodGet, probe, nil)
	req.Header.Set("User-Agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	_, _ = io.CopyN(io.Discard, resp.Body, config.MaxResponseBodyBytes)
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

func doRequest(ctx context.Context, method, u, userAgent string, client *http.Client) (int, string, error) {
	reqCtx, cancel := context.WithTimeout(ctx, config.ValidationTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, method, u, nil)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("User-Agent", userAgent)
	hc := client
	if hc == nil {
		hc = http.DefaultClient
	}
	resp, err := hc.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	_, _ = io.CopyN(io.Discard, resp.Body, config.MaxResponseBodyBytes)
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