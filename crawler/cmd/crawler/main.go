package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/arkivari/crawler/internal/domainsync"
	"github.com/arkivari/crawler/internal/httpclient"
	"github.com/arkivari/crawler/internal/scheduler"
	"github.com/arkivari/crawler/internal/store"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		log.Fatal("DATABASE_URL is required")
	}
	listPath := os.Getenv("DOMAIN_LIST_PATH")
	if listPath == "" {
		listPath = "domains.json"
	}
	userAgent := os.Getenv("CRAWLER_USER_AGENT")
	if userAgent == "" {
		userAgent = "arkivari-crawler/1.0"
	}
	syncInterval := 10 * time.Minute
	if raw := os.Getenv("DOMAIN_SYNC_INTERVAL"); raw != "" {
		if d, err := time.ParseDuration(raw); err == nil {
			syncInterval = d
		}
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	pool, err := store.WaitForDB(ctx, dsn)
	if err != nil {
		log.Fatalf("database: %v", err)
	}
	defer pool.Close()

	owner := os.Getenv("HOSTNAME")
	if owner == "" {
		owner = "crawler"
	}
	st := store.New(pool, owner)
	client := httpclient.Default()

	sync := func() error {
		return domainsync.Apply(ctx, st, listPath)
	}
	if err := sync(); err != nil {
		log.Printf("initial domain sync: %v", err)
	}

	sched := &scheduler.Scheduler{
		Store:     st,
		UserAgent: userAgent,
		Client:    client,
	}
	log.Printf("crawler daemon started (sync every %s)", syncInterval)
	sched.Loop(ctx, sync, syncInterval)
}