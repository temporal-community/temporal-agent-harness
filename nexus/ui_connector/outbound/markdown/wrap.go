package markdown

import (
	"strings"
	"unicode"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

// PrefixText reopens inline formatting after leading whitespace. Code fences
// keep whitespace inside the block instead.
func PrefixText(text, prefix string) string {
	if strings.HasSuffix(prefix, "\n") {
		return prefix + text
	}
	tail := strings.TrimLeftFunc(text, unicode.IsSpace)
	return text[:len(text)-len(tail)] + prefix + tail
}

// WrapChunks adds display delimiters and adjusts citation offsets for their
// position. It does not change the source chunks or citations.
func WrapChunks(chunks []protocol.Chunk, citations []protocol.Citation, prefix, suffix string) ([]protocol.Chunk, []protocol.Citation) {
	out := append([]protocol.Chunk(nil), chunks...)
	position := 0
	for i := range out {
		if out[i].Text == "" || prefix == "" {
			continue
		}
		text := out[i].Text
		if strings.TrimSpace(text) == "" && !strings.HasSuffix(prefix, "\n") {
			position += len([]rune(text))
			continue
		}
		if !strings.HasSuffix(prefix, "\n") {
			position += len([]rune(text)) - len([]rune(strings.TrimLeftFunc(text, unicode.IsSpace)))
		}
		out[i].Text = PrefixText(text, prefix)
		break
	}
	if suffix != "" {
		out = append(out, protocol.Chunk{Text: suffix})
	}
	cs := append([]protocol.Citation(nil), citations...)
	for i := range cs {
		if cs[i].EndIndex >= position {
			cs[i].EndIndex += len([]rune(prefix))
		}
	}
	return out, cs
}
