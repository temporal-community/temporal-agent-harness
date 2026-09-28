package markdown

import (
	"testing"

	"github.com/stretchr/testify/assert"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func TestRender(t *testing.T) {
	tool := func(phase protocol.ToolPhase) protocol.Chunk {
		return protocol.Chunk{Tool: &protocol.ToolChunk{Name: "search", Phase: phase, Error: "oops"}}
	}
	thought := func(delta, text string, done bool) protocol.Chunk {
		return protocol.Chunk{Thought: &protocol.ThoughtChunk{Delta: delta, Text: text, Done: done}}
	}
	cases := []struct {
		name  string
		chunk protocol.Chunk
		want  string
	}{
		{"text", protocol.Chunk{Text: "hello"}, "hello"},
		{"citations", protocol.Chunk{Citations: []protocol.Citation{{URL: "u"}}}, ""},
		{"tool running", tool(protocol.ToolRunning), "\n_🔧 search…_\n"},
		{"tool complete", tool(protocol.ToolComplete), "_✅ search_\n"},
		{"tool error", tool(protocol.ToolErrored), "_⚠️ search failed: oops_\n"},
		{"thought first chunk", thought("a\nb", "a\nb", false), "\n> 💭 a\n> b"},
		{"thought later chunk", thought(" c", "a\nb c", false), " c"},
		{"thought done", thought("", "a", true), "\n\n"},
		{"thought merged whole", thought("a", "a", true), "\n> 💭 a\n\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, Render(tc.chunk))
		})
	}
}

func TestGuideEntry(t *testing.T) {
	h := protocol.Handler{Name: "set_model", Description: "Set the model.", MidTurn: "accept", Fields: []protocol.Field{{Name: "model", Kind: protocol.KindEnum, Options: []string{"a", "b"}, Required: true}}}
	assert.Equal(t, "*set_model* — Set the model. · usable mid-turn\n`/set_model model=<a|b>`\n• model (a|b)", GuideEntry(h))
	assert.Contains(t, Guide([]protocol.Handler{h}), "*What I accept.*")
}

func TestRenderHandlerOutput(t *testing.T) {
	assert.Equal(t, "", HandlerOutput(nil))
	assert.Equal(t, "hi", HandlerOutput(map[string]any{"text": "hi"}))
	assert.Equal(t, "_Done._", HandlerOutput(map[string]any{}))
	assert.Equal(t, "```json\n{\n  \"n\": 1\n}\n```", HandlerOutput(map[string]any{"n": 1}))
}

func TestUsage(t *testing.T) {
	assert.Equal(t, "`/stop`", Usage(protocol.Handler{Name: "stop"}))
	assert.Equal(t, "`/ask <text>`\n• text — What to ask", Usage(protocol.Handler{Name: "ask", Fields: []protocol.Field{{Name: "text", Kind: protocol.KindString, Description: "What to ask"}}}))

	fields := []protocol.Field{
		{Name: "model", Kind: protocol.KindEnum, Options: []string{"a", "b"}, Required: true},
		{Name: "n", Kind: protocol.KindInteger, Description: "How many"},
		{Name: "extra", Kind: protocol.KindJSON, JSONReason: "list"},
	}
	assert.Equal(t,
		"`/cfg model=<a|b> [n=<number>] [extra=<json>]`\n"+
			"• model (a|b)\n"+
			"• n (number, optional) — How many\n"+
			"• extra (json, optional) — pass JSON: list",
		Usage(protocol.Handler{Name: "cfg", Fields: fields}))
}

func TestHandlerHelp(t *testing.T) {
	h := protocol.Handler{Name: "set_model", Fields: []protocol.Field{{Name: "model", Kind: protocol.KindEnum, Options: []string{"a", "b"}, Required: true}}}
	assert.Equal(t, "`/set_model` takes arguments:\n`/set_model model=<a|b>`\n• model (a|b)", HandlerHelp(protocol.HandlerHelp{Handler: h}))
	assert.Equal(t, "Could not run `set_model`:\n• model: Must be one of: a, b.\n\n`/set_model model=<a|b>`\n• model (a|b)", HandlerHelp(protocol.HandlerHelp{Handler: h, Errors: map[string]string{"model": "Must be one of: a, b."}}))
}
