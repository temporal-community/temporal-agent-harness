package markdown

import (
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/presentation"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

// Render formats one chunk as Markdown. Each tool phase gets a separate line.
// Thought summaries use block quotes.
func Render(c protocol.Chunk) string {
	switch {
	case c.Tool != nil:
		switch c.Tool.Phase {
		case protocol.ToolRunning:
			return "\n_🔧 " + c.Tool.Name + "…_\n"
		case protocol.ToolComplete:
			return "_✅ " + c.Tool.Name + "_\n"
		case protocol.ToolErrored:
			return "_⚠️ " + c.Tool.Name + " failed: " + presentation.Clip(c.Tool.Error, 200) + "_\n"
		}
		return ""
	case c.Thought != nil:
		var out string
		if c.Thought.Delta != "" {
			out = strings.ReplaceAll(c.Thought.Delta, "\n", "\n> ")
			if c.Thought.Delta == c.Thought.Text {
				// First chunk of the block.
				out = "\n> 💭 " + out
			}
		}
		if c.Thought.Done {
			out += "\n\n"
		}
		return out
	default:
		return c.Text
	}
}

// RenderChunks joins Render over chunks.
func RenderChunks(chunks []protocol.Chunk) string {
	var b strings.Builder
	for _, c := range chunks {
		b.WriteString(Render(c))
	}
	return b.String()
}

// Guide renders the handler guide.
func Guide(handlers []protocol.Handler) string {
	var b strings.Builder
	b.WriteString("*What I accept.* Mention me to chat, or send a specific request:\n")
	for _, h := range handlers {
		b.WriteString("\n")
		b.WriteString(GuideEntry(h))
		b.WriteString("\n")
	}
	return b.String()
}

// GuideEntry renders one handler: name, description, and usage.
func GuideEntry(h protocol.Handler) string {
	return HandlerDescription(h) + "\n" + Usage(h)
}

// HandlerDescription formats the handler name, description, and active-turn policy.
func HandlerDescription(h protocol.Handler) string {
	line := "*" + h.Name + "*"
	if h.Description != "" {
		line += " — " + h.Description
	}
	if h.MidTurn == "accept" {
		line += " · usable mid-turn"
	}
	return line
}
