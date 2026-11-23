package discover

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/arkivari/crawler/internal/httpclient"
	"github.com/arkivari/crawler/internal/robots"
	"github.com/arkivari/crawler/internal/urlnorm"
)

func TestFetchPageHTTPLocal(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte(`<html><a href="/a">a</a></html>`))
	}))
	defer srv.Close()
	policy := robots.New(srv.URL, "test", srv.Client())
	_ = policy.Load(context.Background())
	policy.SetCrawlDelayForTest(0)
	target := urlnorm.TargetNetloc(srv.URL)
	hc := httpclient.ForCrawl(srv.Client(), target, policy)
	page := urlnorm.NormalizeURL(srv.URL + "/")
	res := fetchPage(context.Background(), page, "test", policy, hc, "")
	if !res.ok {
		t.Fatalf("expected ok fetch for %s", page)
	}
}

func TestCrawlSiteCappedNoPanic(t *testing.T) {
	var fetches atomic.Int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fetches.Add(1)
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte(`<html><a href="/a">a</a><a href="/b">b</a></html>`))
	}))
	defer srv.Close()

	policy := robots.New(srv.URL, "test", srv.Client())
	_ = policy.Load(context.Background())
	policy.SetCrawlDelayForTest(0)

	target := urlnorm.TargetNetloc(srv.URL)
	res := CrawlSite(
		context.Background(),
		srv.URL+"/",
		target,
		"test",
		policy,
		srv.Client(),
		1,
		nil,
		"",
		false,
	)
	if !res.CrawlCapped {
		t.Fatalf("expected capped, got %+v", res)
	}
	if len(res.URLs) != 1 {
		t.Fatalf("expected 1 discovered URL, got %d", len(res.URLs))
	}
}