package discover

import "testing"

func TestResolveLink(t *testing.T) {
	base := "https://example.com/page"
	got := ResolveLink(base, "/child")
	if got != "https://example.com/child" {
		t.Fatalf("relative: got %q", got)
	}
	if ResolveLink(base, "mailto:x@y") != "" {
		t.Fatal("expected skip mailto")
	}
	if ResolveLink(base, "#section") != "" {
		t.Fatal("expected skip fragment")
	}
}