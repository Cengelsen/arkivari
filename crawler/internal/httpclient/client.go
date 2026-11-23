package httpclient

import (
	"net"
	"net/http"
	"time"

	"github.com/arkivari/crawler/internal/config"
	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
)

// Default returns a shared HTTP client with connection pooling.
// Per-request deadlines use context in discover/validate.
func Default() *http.Client {
	return &http.Client{
		Transport: newTransport(),
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return http.ErrUseLastResponse
			}
			return nil
		},
	}
}

// ForCrawl clones the client with same-domain and robots checks on redirects.
func ForCrawl(base *http.Client, targetNetloc string, policy *robots.Policy) *http.Client {
	if base == nil {
		base = Default()
	}
	c := *base
	if c.Transport == nil {
		c.Transport = newTransport()
	}
	c.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return http.ErrUseLastResponse
		}
		raw := req.URL.String()
		normalized := urlnorm.NormalizeURL(raw)
		if normalized == "" || !urlnorm.SameDomain(normalized, targetNetloc) {
			return http.ErrUseLastResponse
		}
		if policy != nil && !policy.CanFetch(normalized) {
			return http.ErrUseLastResponse
		}
		return nil
	}
	return &c
}

func newTransport() *http.Transport {
	return &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   30 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          128,
		MaxIdleConnsPerHost:   config.OriginMaxConcurrency + 2,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	}
}