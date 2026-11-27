package discover

import (
	"context"
	"crypto/tls"
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
	res := fetchPage(context.Background(), page, "test", policy, hc, "", false)
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

func TestCrawlSiteQueuesLinkedDocuments(t *testing.T) {
	const pdfBody = "%PDF-1.4\n"
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<html><a href="/files/rapport">rapport</a><a href="/files/rapport.pdf">pdf</a></html>`))
		case "/files/rapport":
			w.Header().Set("Content-Type", "application/pdf")
			_, _ = w.Write([]byte(pdfBody))
		case "/files/rapport.pdf":
			w.Header().Set("Content-Type", "application/pdf")
			_, _ = w.Write([]byte(pdfBody))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	policy := robots.New(srv.URL, "test", srv.Client())
	_ = policy.Load(context.Background())
	policy.SetCrawlDelayForTest(0)

	target := urlnorm.TargetNetloc(srv.URL)
	res := CrawlSite(context.Background(), srv.URL+"/", target, "test", policy, srv.Client(), 0, nil, "", false)

	want := map[string]struct{}{
		urlnorm.NormalizeURL(srv.URL + "/"):                  {},
		urlnorm.NormalizeURL(srv.URL + "/files/rapport"):     {},
		urlnorm.NormalizeURL(srv.URL + "/files/rapport.pdf"): {},
	}
	if len(res.URLs) < 3 {
		t.Fatalf("expected at least home+2 pdf urls, got %v", res.URLs)
	}
	for _, u := range res.URLs {
		delete(want, u)
	}
	if len(want) != 0 {
		t.Fatalf("missing discovered urls: %v", want)
	}
}

func TestCrawlSiteQueuesCrossDomainDocuments(t *testing.T) {
	const pdfBody = "%PDF-1.4\n"
	docHost := ""
	docSrv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		_, _ = w.Write([]byte(pdfBody))
	}))
	defer docSrv.Close()
	docHost = urlnorm.TargetNetloc(docSrv.URL)
	docURL := urlnorm.NormalizeURL(docSrv.URL + "/rapport.pdf")

	siteSrv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		html := `<html><a href="` + docURL + `">external report</a><a href="https://example.com/">noise</a></html>`
		_, _ = w.Write([]byte(html))
	}))
	defer siteSrv.Close()

	policy := robots.New(siteSrv.URL, "test", siteSrv.Client())
	_ = policy.Load(context.Background())
	policy.SetCrawlDelayForTest(0)

	client := &http.Client{
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		},
	}

	target := urlnorm.TargetNetloc(siteSrv.URL)
	res := CrawlSite(context.Background(), siteSrv.URL+"/", target, "test", policy, client, 0, nil, "", false)

	got := map[string]struct{}{}
	for _, u := range res.URLs {
		got[u] = struct{}{}
	}
	if _, ok := got[urlnorm.NormalizeURL(siteSrv.URL+"/")]; !ok {
		t.Fatalf("expected home page in discovery, got %v", res.URLs)
	}
	if _, ok := got[docURL]; !ok {
		t.Fatalf("expected cross-domain pdf %s in discovery, got %v", docURL, res.URLs)
	}
	if docHost == target {
		t.Fatal("test setup: document host must differ from site host")
	}
}