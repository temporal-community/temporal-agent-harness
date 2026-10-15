package slackoutbound

import (
	"fmt"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/timeline"
)

// Slack wording for timeline output.

const thinkingTitle = "Thinking"

// maxStatusMessages limits the status texts that Slack rotates through.
const maxStatusMessages = 10

// statusText returns the status line text and the texts to rotate through.
func statusText(s timeline.Status) (string, []string) {
	switch {
	case s.Clear:
		return "", nil
	case len(s.Running) == 0:
		return "Thinking…", []string{"Thinking…"}
	case len(s.Running) == 1:
		text := "Running " + s.Running[0] + "…"
		return text, []string{text}
	}
	var messages []string
	seen := map[string]bool{}
	for _, name := range s.Running {
		if !seen[name] && len(messages) < maxStatusMessages {
			seen[name] = true
			messages = append(messages, "Running "+name+"…")
		}
	}
	return fmt.Sprintf("Running %d tools…", len(s.Running)), messages
}

func allowListedNote(toolName, reason string) string {
	if reason != "" {
		return fmt.Sprintf("_`%s` is allowed for the rest of this session — %s_", toolName, reason)
	}
	return fmt.Sprintf("_`%s` is allowed for the rest of this session_", toolName)
}

func errorText(msg string) string {
	return "_Something went wrong handling that: " + msg + "_"
}
