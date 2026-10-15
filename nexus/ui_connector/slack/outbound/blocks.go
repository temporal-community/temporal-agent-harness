package slackoutbound

import (
	"encoding/json"
	"fmt"
	"strings"

	slackapi "github.com/slack-go/slack"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/markdown"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/timeline"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/presentation"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/interaction"
)

// Block Kit text limits.
const (
	buttonTextMax = 75
	maxActions    = 25
)

// argsBlock shows a tool call's arguments as a code block.
func argsBlock(toolInput string) *slackapi.SectionBlock {
	args := prettyJSON(toolInput)
	if args == "" {
		return interaction.Section("_Called with no arguments._")
	}
	return interaction.Section("```" + presentation.Clip(args, interaction.SectionTextMax-8) + "```")
}

func approvalPromptBlocks(sessionID string, req protocol.ApprovalRequest) []slackapi.Block {
	value := func(approved, remember bool) string {
		b, _ := json.Marshal(interaction.ApprovalButtonValue{
			SessionID: sessionID, ToolID: req.ToolID, ToolName: req.ToolName,
			Approved: approved, Remember: remember,
		})
		return string(b)
	}
	approve := slackapi.NewButtonBlockElement(interaction.ActionApprove, value(true, false), interaction.PlainText("Approve once", buttonTextMax)).
		WithStyle(slackapi.StylePrimary)
	always := slackapi.NewButtonBlockElement(interaction.ActionAlwaysAllow, value(true, true), interaction.PlainText("Always allow", buttonTextMax))
	deny := slackapi.NewButtonBlockElement(interaction.ActionDeny, value(false, false), interaction.PlainText("Deny", buttonTextMax)).
		WithStyle(slackapi.StyleDanger)
	return []slackapi.Block{
		interaction.Section(fmt.Sprintf("🔐 *Run `%s`?*", req.ToolName)),
		argsBlock(req.ToolInputJSON),
		slackapi.NewActionBlock("tool_approval", approve, always, deny),
	}
}

func settledApprovalBlocks(p timeline.Prompt, res protocol.ApprovalResolution) []slackapi.Block {
	icon, verdict := "🚫", "Denied"
	if res.Approved {
		icon, verdict = "✅", "Approved"
	}
	note := verdict + "."
	if res.Reason != "" {
		note = res.Reason
	}
	return []slackapi.Block{
		interaction.Section(fmt.Sprintf("%s *%s* · `%s`", icon, verdict, res.ToolName)),
		argsBlock(p.ToolInput),
		slackapi.NewContextBlock("", interaction.MarkdownText(note)),
	}
}

// guideEntry shows the handler description and its finite input choices.
func guideEntry(h protocol.Handler) string {
	fields := h.Fields
	text := markdown.HandlerDescription(h)
	for _, f := range fields {
		options := f.Options
		if f.Kind == protocol.KindBoolean {
			options = []string{"true", "false"}
		}
		if len(options) == 0 {
			continue
		}
		label := "Possible values"
		if len(fields) > 1 {
			label += " (" + presentation.FieldLabel(f) + ")"
		}
		values := make([]string, len(options))
		for i, option := range options {
			values[i] = "`" + option + "`"
		}
		text += "\n" + label + ": " + strings.Join(values, " | ")
	}
	return text
}

func guideBlocks(sessionID string, handlers []protocol.Handler) []slackapi.Block {
	blocks := []slackapi.Block{
		slackapi.NewHeaderBlock(interaction.PlainText("What this agent accepts", 150)),
		interaction.Section("Keep responding to the agent to continue chatting, or use each of the following buttons to set the agent's parameters."),
	}
	// Leave room for the action blocks below the handler sections, and for a note when
	// the list is cut.
	room := interaction.MaxBlocks - len(blocks) - (len(handlers)+maxActions-1)/maxActions
	if len(handlers) > room {
		room--
	}
	for i, h := range handlers {
		if i >= room {
			blocks = append(blocks, interaction.Section(fmt.Sprintf("_…and %d more. Their buttons are below._", len(handlers)-i)))
			break
		}
		blocks = append(blocks, interaction.Section(guideEntry(h)))
	}

	var buttons []slackapi.BlockElement
	for _, h := range handlers {
		buttons = append(buttons, slackapi.NewButtonBlockElement(interaction.ActionOpenFormPrefix+h.Name, interaction.EncodeFormButton(sessionID, h), interaction.PlainText(h.Name, buttonTextMax)))
	}
	for len(buttons) > 0 && len(blocks) < interaction.MaxBlocks {
		n := min(len(buttons), maxActions)
		blocks = append(blocks, slackapi.NewActionBlock("", buttons[:n]...))
		buttons = buttons[n:]
	}
	return blocks
}
