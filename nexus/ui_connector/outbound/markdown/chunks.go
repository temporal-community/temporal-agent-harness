package markdown

import (
	"fmt"
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

// SpliceChunks renders chunks and inserts citation markers. It adjusts reply text
// offsets for the tool and thought text added by rendering.
func SpliceChunks(chunks []protocol.Chunk, cs []protocol.Citation, link func(url, title string) string) string {
	return Splice(RenderChunks(chunks), retarget(chunks, cs), link)
}

func retarget(chunks []protocol.Chunk, cs []protocol.Citation) []protocol.Citation {
	if len(cs) == 0 {
		return cs
	}
	out := make([]protocol.Citation, len(cs))
	for i, c := range cs {
		c.EndIndex = retargetEndIndex(chunks, c.EndIndex)
		out[i] = c
	}
	return out
}

func retargetEndIndex(chunks []protocol.Chunk, endIndex int) int {
	if endIndex < 0 {
		return -1
	}
	var replyOffset, renderedOffset int
	for _, c := range chunks {
		if c.Text != "" {
			n := len([]rune(c.Text))
			if endIndex <= replyOffset+n {
				return renderedOffset + (endIndex - replyOffset)
			}
			replyOffset += n
		}
		renderedOffset += len([]rune(Render(c)))
	}
	return renderedOffset
}

// SourcesFooter lists unique source URLs as numbered links. Empty means no sources.
func SourcesFooter(cs []protocol.Citation, link func(url, title string) string) string {
	seen := map[string]bool{}
	var parts []string
	for _, c := range cs {
		if c.URL == "" || seen[c.URL] {
			continue
		}
		seen[c.URL] = true
		parts = append(parts, fmt.Sprintf("%d. %s", len(parts)+1, link(c.URL, c.Title)))
	}
	if len(parts) == 0 {
		return ""
	}
	return "\n\n*Sources*\n" + strings.Join(parts, "\n")
}
