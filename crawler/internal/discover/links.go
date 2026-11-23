package discover

import (
	"net/url"
	"strings"
)

var skipSchemes = map[string]struct{}{
	"mailto": {}, "javascript": {}, "tel": {}, "ftp": {}, "data": {},
}

func isHTML(contentType string) bool {
	ct := strings.ToLower(strings.TrimSpace(strings.Split(contentType, ";")[0]))
	return strings.HasPrefix(ct, "text/html") || strings.HasPrefix(ct, "application/xhtml+xml")
}

// ResolveLink joins base and href like Python urljoin + scheme filtering.
func ResolveLink(baseURL, href string) string {
	href = strings.TrimSpace(href)
	if href == "" || strings.HasPrefix(href, "#") {
		return ""
	}
	if idx := strings.Index(href, ":"); idx > 0 {
		scheme := strings.ToLower(href[:idx])
		if _, skip := skipSchemes[scheme]; skip {
			return ""
		}
	}
	base, err := url.Parse(baseURL)
	if err != nil {
		return ""
	}
	ref, err := url.Parse(href)
	if err != nil {
		return ""
	}
	return base.ResolveReference(ref).String()
}