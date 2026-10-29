package interaction

import (
	slackapi "github.com/slack-go/slack"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/presentation"
)

// Shared Block Kit limits for messages and forms.
const (
	SectionTextMax = 3000
	MaxBlocks      = 50
)

// MarkdownText creates a Markdown text object within the section text limit.
func MarkdownText(text string) *slackapi.TextBlockObject {
	return slackapi.NewTextBlockObject(slackapi.MarkdownType, presentation.Clip(text, SectionTextMax), false, false)
}

// PlainText creates a plain text object with at most max runes.
func PlainText(text string, max int) *slackapi.TextBlockObject {
	return slackapi.NewTextBlockObject(slackapi.PlainTextType, presentation.Clip(text, max), false, false)
}

// Section creates a section with Markdown text.
func Section(text string) *slackapi.SectionBlock {
	return slackapi.NewSectionBlock(MarkdownText(text), nil, nil)
}
