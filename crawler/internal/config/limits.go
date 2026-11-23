// Package config holds tunables for the Go crawl daemon.
package config

import "time"

const (
	DefaultCrawlDelay      = 100 * time.Millisecond
	OriginMaxConcurrency   = 16
	OriginBackoff          = 15 * time.Second
	CrawlWorkers           = 16
	CrawlTimeout           = 15 * time.Second
	SitemapTimeout         = 60 * time.Second
	SitemapMaxDepth        = 5
	ValidationWorkers      = 16
	ValidationTimeout      = 20 * time.Second
	MaxResponseBodyBytes   = 8 << 20 // 8 MiB cap per HTML/sitemap response
	MaxRobotsBodyBytes     = 512 << 10
	ValidationBatchSize    = 50
	DBBatchSize            = 200
)