package markdown

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func TestWrapChunksKeepsSourceOffsets(t *testing.T) {
	original := []protocol.Chunk{{Text: " "}, {Text: "words"}}
	cs := []protocol.Citation{{URL: "u", EndIndex: 6}, {URL: "v", EndIndex: -1}}
	chunks, citations := WrapChunks(original, cs, "**", "**")
	assert.Equal(t, " **words**", RenderChunks(chunks))
	assert.Equal(t, 8, citations[0].EndIndex)
	assert.Equal(t, -1, citations[1].EndIndex)
	assert.Equal(t, " words", RenderChunks(original))
	assert.Equal(t, 6, cs[0].EndIndex)
}
