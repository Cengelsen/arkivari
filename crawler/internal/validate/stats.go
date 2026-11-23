package validate

// ThrottleSummary counts rate-limit class responses from crawl-check results.
type ThrottleSummary struct {
	RateLimited   int
	TransientHTTP int
	Total         int
	ByStatus      map[string]int
}

func isThrottleLike(r LinkResult) bool {
	if r.Status == "rate_limited" {
		return true
	}
	switch r.HTTPCode {
	case 429, 502, 503:
		return true
	default:
		return false
	}
}

// ThrottleCounts aggregates 429-like outcomes from link check results.
func ThrottleCounts(results []LinkResult) ThrottleSummary {
	s := ThrottleSummary{ByStatus: map[string]int{}}
	for _, r := range results {
		s.ByStatus[r.Status]++
		if r.Status == "rate_limited" {
			s.RateLimited++
		}
		switch r.HTTPCode {
		case 429, 502, 503:
			s.TransientHTTP++
		}
		if isThrottleLike(r) {
			s.Total++
		}
	}
	return s
}