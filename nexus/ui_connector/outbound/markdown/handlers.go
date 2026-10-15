package markdown

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func fieldHint(f protocol.Field) string {
	switch f.Kind {
	case protocol.KindEnum:
		return strings.Join(f.Options, "|")
	case protocol.KindBoolean:
		return "true|false"
	case protocol.KindInteger, protocol.KindNumber:
		return "number"
	case protocol.KindDate:
		return "YYYY-MM-DD"
	case protocol.KindJSON:
		return "json"
	default:
		return "text"
	}
}

// Usage formats a handler command and its fields as Markdown.
// Optional fields are bracketed.
func Usage(h protocol.Handler) string {
	fields := h.Fields
	command := "/" + h.Name
	if len(fields) == 0 {
		return "`" + command + "`"
	}
	if len(fields) == 1 && fields[0].Kind == protocol.KindString {
		f := fields[0]
		desc := "text"
		if f.Description != "" {
			desc = f.Description
		}
		return fmt.Sprintf("`%s <%s>`\n• %s — %s", command, f.Name, f.Name, desc)
	}
	usage := make([]string, len(fields))
	lines := make([]string, len(fields))
	for i, f := range fields {
		if f.Required {
			usage[i] = fmt.Sprintf("%s=<%s>", f.Name, fieldHint(f))
		} else {
			usage[i] = fmt.Sprintf("[%s=<%s>]", f.Name, fieldHint(f))
		}
		line := fmt.Sprintf("• %s (%s", f.Name, fieldHint(f))
		if !f.Required {
			line += ", optional"
		}
		line += ")"
		if f.Description != "" {
			line += " — " + f.Description
		}
		if f.JSONReason != "" {
			line += " — pass JSON: " + f.JSONReason
		}
		lines[i] = line
	}
	return "`" + command + " " + strings.Join(usage, " ") + "`\n" + strings.Join(lines, "\n")
}

// HandlerOutput uses the return value's "text" field if available.
// It formats other values as JSON.
func HandlerOutput(output map[string]any) string {
	if output == nil {
		return ""
	}
	if text, ok := output["text"].(string); ok {
		return text
	}
	if len(output) == 0 {
		return "_Done._"
	}
	b, err := json.MarshalIndent(output, "", "  ")
	if err != nil {
		return ""
	}
	return "```json\n" + string(b) + "\n```"
}

// HandlerHelp formats argument errors and the handler's usage.
func HandlerHelp(help protocol.HandlerHelp) string {
	h := help.Handler
	if len(help.Errors) == 0 {
		return fmt.Sprintf("`/%s` takes arguments:\n%s", h.Name, Usage(h))
	}
	var problems []string
	for _, f := range h.Fields {
		if msg, ok := help.Errors[f.Name]; ok {
			problems = append(problems, fmt.Sprintf("• %s: %s", f.Name, msg))
		}
	}
	return fmt.Sprintf("Could not run `%s`:\n%s\n\n%s", h.Name, strings.Join(problems, "\n"), Usage(h))
}
