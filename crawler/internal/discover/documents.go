package discover

import (
	"mime"
	"net/url"
	"path"
	"strings"
)

// archiveDocumentExtensions lists file types we queue for Save Page Now when
// detected from URL hints or response headers during crawl.
var archiveDocumentExtensions = map[string]struct{}{
	".pdf":  {},
	".doc":  {},
	".docx": {},
	".xls":  {},
	".xlsx": {},
	".ppt":  {},
	".pptx": {},
	".odt":  {},
	".ods":  {},
	".odp":  {},
	".csv":  {},
	".zip":  {},
	".rtf":  {},
	".epub": {},
	".tif":  {},
	".tiff": {},
}

// documentMIMEs is exact Content-Type media types (lowercase, no params).
var documentMIMEs = map[string]struct{}{
	"application/pdf": {},
	"application/msword": {},
	"application/vnd.openxmlformats-officedocument.wordprocessingml.document": {},
	"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":         {},
	"application/vnd.openxmlformats-officedocument.presentationml.presentation": {},
	"application/vnd.ms-excel": {},
	"application/vnd.ms-powerpoint": {},
	"application/vnd.oasis.opendocument.text":         {},
	"application/vnd.oasis.opendocument.spreadsheet":  {},
	"application/vnd.oasis.opendocument.presentation": {},
	"text/csv":                  {},
	"application/csv":           {},
	"application/zip":           {},
	"application/x-zip-compressed": {},
	"application/rtf":           {},
	"application/epub+zip":      {},
	"image/tiff":                {},
}

const documentSniffLen = 512

// URLLooksLikeDocument reports whether the URL path or query suggests an allowlisted file.
func URLLooksLikeDocument(raw string) bool {
	return fileExtensionOfURL(raw) != ""
}

// ResponseIsDocument reports whether HTTP response metadata indicates an allowlisted file.
func ResponseIsDocument(contentType, contentDisposition string, prefix []byte) bool {
	ct := normalizeMediaType(contentType)
	if ct != "" {
		if _, ok := documentMIMEs[ct]; ok {
			return true
		}
		if strings.HasPrefix(ct, "application/vnd.openxmlformats-officedocument.") {
			return true
		}
		if strings.HasPrefix(ct, "application/vnd.oasis.opendocument.") {
			return true
		}
	}
	if ext := extensionFromContentDisposition(contentDisposition); ext != "" {
		if _, ok := archiveDocumentExtensions[ext]; ok {
			return true
		}
	}
	return sniffDocumentMagic(prefix)
}

func fileExtensionOfURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	if ext := allowedExtension(u.Path); ext != "" {
		return ext
	}
	for _, key := range []string{"file", "filename", "download", "attachment"} {
		if v := u.Query().Get(key); v != "" {
			if ext := allowedExtension(v); ext != "" {
				return ext
			}
		}
	}
	rawQuery := strings.ToLower(u.RawQuery)
	for ext := range archiveDocumentExtensions {
		if strings.Contains(rawQuery, ext) {
			return ext
		}
	}
	return ""
}

func allowedExtension(name string) string {
	ext := strings.ToLower(path.Ext(path.Base(name)))
	if ext == "" {
		return ""
	}
	if _, ok := archiveDocumentExtensions[ext]; ok {
		return ext
	}
	return ""
}

func normalizeMediaType(contentType string) string {
	contentType = strings.TrimSpace(contentType)
	if contentType == "" {
		return ""
	}
	mediaType, _, err := mime.ParseMediaType(contentType)
	if err != nil {
		mediaType = strings.ToLower(strings.TrimSpace(strings.Split(contentType, ";")[0]))
	}
	return strings.ToLower(mediaType)
}

func extensionFromContentDisposition(header string) string {
	header = strings.TrimSpace(header)
	if header == "" {
		return ""
	}
	_, params, err := mime.ParseMediaType(header)
	if err != nil {
		return ""
	}
	if fn := params["filename"]; fn != "" {
		return allowedExtension(fn)
	}
	if fn := params["filename*"]; fn != "" {
		// RFC 5987: charset'lang'value
		if i := strings.LastIndex(fn, "'"); i >= 0 && i < len(fn)-1 {
			fn = fn[i+1:]
		}
		return allowedExtension(fn)
	}
	return ""
}

func sniffDocumentMagic(prefix []byte) bool {
	if len(prefix) >= 4 && string(prefix[:4]) == "%PDF" {
		return true
	}
	// ZIP local file header (Office Open XML, ODF, EPUB).
	if len(prefix) >= 2 && prefix[0] == 'P' && prefix[1] == 'K' {
		return true
	}
	return false
}