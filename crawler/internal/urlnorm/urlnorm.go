package urlnorm

import (
	"net/url"
	"strings"
)

var indexPaths = map[string]struct{}{
	"/index.html": {},
	"/index.htm":  {},
	"/index.php":  {},
}

func NormalizeDomain(domain string) (string, error) {
	domain = strings.TrimSpace(domain)
	if domain == "" {
		return "", errEmptyDomain
	}
	if !strings.Contains(domain, "://") {
		domain = "https://" + domain
	}
	u, err := url.Parse(domain)
	if err != nil || u.Host == "" {
		return "", errInvalidDomain
	}
	host := strings.ToLower(u.Host)
	if strings.HasPrefix(host, "www.") {
		host = host[4:]
	}
	return "https://" + host, nil
}

var (
	errEmptyDomain   = errorsNew("domain cannot be empty")
	errInvalidDomain = errorsNew("invalid domain")
)

type stringError string

func (e stringError) Error() string { return string(e) }

func errorsNew(s string) error { return stringError(s) }

func TargetNetloc(baseURL string) string {
	u, _ := url.Parse(baseURL)
	host := strings.ToLower(u.Host)
	if strings.HasPrefix(host, "www.") {
		host = host[4:]
	}
	return host
}

func NormalizeURL(raw string) string {
	raw = strings.TrimSpace(raw)
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return ""
	}
	host := strings.ToLower(u.Host)
	if strings.HasPrefix(host, "www.") {
		host = host[4:]
	}
	if strings.HasSuffix(host, ":80") {
		host = strings.TrimSuffix(host, ":80")
	} else if strings.HasSuffix(host, ":443") {
		host = strings.TrimSuffix(host, ":443")
	}
	path := u.Path
	if path == "" {
		path = "/"
	}
	lowerPath := strings.ToLower(path)
	if _, ok := indexPaths[lowerPath]; ok {
		path = "/"
	} else if path != "/" && strings.HasSuffix(path, "/") {
		path = strings.TrimSuffix(path, "/")
	}
	out := url.URL{Scheme: "https", Host: host, Path: path, RawQuery: u.RawQuery}
	return out.String()
}

func SameDomain(normalizedURL, targetNetloc string) bool {
	u, err := url.Parse(normalizedURL)
	if err != nil {
		return false
	}
	host := strings.ToLower(u.Host)
	if strings.HasPrefix(host, "www.") {
		host = host[4:]
	}
	return host == targetNetloc
}