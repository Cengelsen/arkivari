package discover

import "testing"

func TestResponseIsDocument(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name   string
		ct     string
		disp   string
		prefix []byte
		want   bool
	}{
		{"pdf type", "application/pdf", "", nil, true},
		{"docx type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "", nil, true},
		{"csv type", "text/csv; charset=utf-8", "", nil, true},
		{"html", "text/html", "", nil, false},
		{"disposition", "", `attachment; filename="rapport.pdf"`, nil, true},
		{"pdf magic", "application/octet-stream", "", []byte("%PDF-1.4\n"), true},
		{"zip magic", "application/octet-stream", "", []byte("PK\x03\x04"), true},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := ResponseIsDocument(tc.ct, tc.disp, tc.prefix)
			if got != tc.want {
				t.Fatalf("got %v want %v", got, tc.want)
			}
		})
	}
}

func TestURLLooksLikeDocument(t *testing.T) {
	t.Parallel()
	if !URLLooksLikeDocument("https://example.com/files/report.PDF") {
		t.Fatal("expected pdf path")
	}
	if !URLLooksLikeDocument("https://example.com/download?file=foo.docx") {
		t.Fatal("expected docx query")
	}
	if URLLooksLikeDocument("https://example.com/news/article") {
		t.Fatal("expected false for article")
	}
}