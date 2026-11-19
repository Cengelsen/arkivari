package domainsync

import (
	"context"
	"encoding/json"
	"os"

	"github.com/arkivari/crawler/internal/store"
	"github.com/arkivari/crawler/internal/urlnorm"
)

type ConfigFile struct {
	Version  int            `json:"version"`
	Defaults Defaults       `json:"defaults"`
	Domains  []DomainEntry  `json:"domains"`
}

type Defaults struct {
	MaxPages         int  `json:"max_pages"`
	RecrawlAfterDays int  `json:"recrawl_after_days"`
	Enabled          bool `json:"enabled"`
}

type DomainEntry struct {
	Host             string `json:"host"`
	Enabled          *bool  `json:"enabled"`
	MaxPages         *int   `json:"max_pages"`
	RecrawlAfterDays *int   `json:"recrawl_after_days"`
	Note             string `json:"note"`
}

func Load(path string) (*ConfigFile, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg ConfigFile
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	if cfg.Defaults.RecrawlAfterDays == 0 {
		cfg.Defaults.RecrawlAfterDays = 90
	}
	return &cfg, nil
}

func Apply(ctx context.Context, st *store.Store, path string) error {
	cfg, err := Load(path)
	if err != nil {
		return err
	}
	active := make(map[string]struct{})
	for _, entry := range cfg.Domains {
		base, err := urlnorm.NormalizeDomain(entry.Host)
		if err != nil {
			continue
		}
		host := urlnorm.TargetNetloc(base)
		active[host] = struct{}{}
		enabled := cfg.Defaults.Enabled
		if entry.Enabled != nil {
			enabled = *entry.Enabled
		}
		maxPages := cfg.Defaults.MaxPages
		if entry.MaxPages != nil {
			maxPages = *entry.MaxPages
		}
		recrawl := cfg.Defaults.RecrawlAfterDays
		if entry.RecrawlAfterDays != nil {
			recrawl = *entry.RecrawlAfterDays
		}
		if err := st.SyncDomain(ctx, host, base, enabled, maxPages, recrawl, entry.Note); err != nil {
			return err
		}
	}
	return st.DisableMissingHosts(ctx, active)
}