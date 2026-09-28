package markdown

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

var toolThenReply = []protocol.Chunk{
	{Tool: &protocol.ToolChunk{Name: "search", Phase: protocol.ToolRunning}},  // "\n_🔧 search…_\n"
	{Tool: &protocol.ToolChunk{Name: "search", Phase: protocol.ToolComplete}}, // "_✅ search_\n"
	{Text: "hello world"},
}

func TestRetargetEndIndex(t *testing.T) {
	prefix := len([]rune("\n_🔧 search…_\n_✅ search_\n"))
	assert.Equal(t, prefix+5, retargetEndIndex(toolThenReply, 5), "offset skips the tool lines")
	assert.Equal(t, prefix+11, retargetEndIndex(toolThenReply, 11))
	assert.Equal(t, -1, retargetEndIndex(toolThenReply, -1))
	assert.Equal(t, 5, retargetEndIndex([]protocol.Chunk{{Text: "hello world"}}, 5))
}

func TestSpliceChunks(t *testing.T) {
	got := SpliceChunks(toolThenReply, []protocol.Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 5}}, CommonMarkLink)
	assert.Equal(t, "\n_🔧 search…_\n_✅ search_\nhello [[1]](https://example.com/doc) world", got)
}

func TestSourcesFooter(t *testing.T) {
	assert.Equal(t, "", SourcesFooter(nil, CommonMarkLink))
	assert.Equal(t, "", SourcesFooter([]protocol.Citation{{Title: "no url"}}, CommonMarkLink))
	assert.Equal(t,
		"\n\n*Sources*\n1. [A](https://a)\n2. [B](https://b)",
		SourcesFooter([]protocol.Citation{{URL: "https://a", Title: "A"}, {URL: "https://b", Title: "B"}, {URL: "https://a", Title: "A"}}, CommonMarkLink))
}
