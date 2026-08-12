package slackoutbound

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	slackapi "github.com/slack-go/slack"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/citations"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

// Activity name constants under which SlackPlatform's methods must be registered on
// the worker (see cmd/worker). Private to this package: RouterWorkflow never sees
// these — it only calls Driver, and a different outbound driver is free to use a
// completely different activity shape internally, or none at all.
const (
	beginStreamActivity        = "SlackBeginStream"
	updateStreamActivity       = "SlackUpdateStream"
	finishStreamActivity       = "SlackFinishStream"
	postMessageActivity        = "SlackPostMessage"
	postApprovalPromptActivity = "SlackPostApprovalPrompt"
)

// Driver implements router.OutboundDriver for Slack by dispatching to Activities backed by
// SlackPlatform. It's the bridge between RouterWorkflow (workflow.Context calls) and
// the real Slack API calls, which must run as Activities since they're non-deterministic
// I/O.
type Driver struct {
	ActivityOptions workflow.ActivityOptions
}

var _ router.OutboundDriver = (*Driver)(nil)

// NewDriver returns a Driver that calls the Slack activities with the given options.
func NewDriver(opts workflow.ActivityOptions) Driver {
	return Driver{ActivityOptions: opts}
}

// SupportsStreaming reports that Slack supports incremental response updates.
func (Driver) SupportsStreaming(router.Input) bool {
	return true
}

func (d Driver) BeginStream(ctx workflow.Context, input router.BeginStreamInput) (router.StreamHandle, error) {
	var handle router.StreamHandle
	err := workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), beginStreamActivity, input,
	).Get(ctx, &handle)
	return handle, err
}

func (d Driver) UpdateStream(ctx workflow.Context, input router.UpdateStreamInput) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), updateStreamActivity, input,
	).Get(ctx, nil)
}

func (d Driver) FinishStream(ctx workflow.Context, input router.FinishStreamInput) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), finishStreamActivity, input,
	).Get(ctx, nil)
}

func (d Driver) PostMessage(ctx workflow.Context, input router.TextMetadata) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), postMessageActivity, input,
	).Get(ctx, nil)
}

func (d Driver) PostApprovalPrompt(ctx workflow.Context, input router.ApprovalPromptInput) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), postApprovalPromptActivity, input,
	).Get(ctx, nil)
}

// AcknowledgeApproval is a no-op because the Slack interaction webhook replaces
// the original prompt through its response URL while handling the button click.
func (d Driver) AcknowledgeApproval(workflow.Context, router.ApprovalAcknowledgementInput) error {
	return nil
}

// RegisterActivities registers platform's methods on w under the activity names Driver
// dispatches to. Call this from the worker binary alongside NewDriver. w is
// worker.Registry (not worker.Worker) so this also works for a Lambda worker's
// lambdaworker.Options, which implements Registry but not the full Worker interface.
func RegisterActivities(w worker.Registry, platform *SlackPlatform) {
	w.RegisterActivityWithOptions(platform.BeginStream, activity.RegisterOptions{Name: beginStreamActivity})
	w.RegisterActivityWithOptions(platform.UpdateStream, activity.RegisterOptions{Name: updateStreamActivity})
	w.RegisterActivityWithOptions(platform.FinishStream, activity.RegisterOptions{Name: finishStreamActivity})
	w.RegisterActivityWithOptions(platform.PostMessage, activity.RegisterOptions{Name: postMessageActivity})
	w.RegisterActivityWithOptions(platform.PostApprovalPrompt, activity.RegisterOptions{Name: postApprovalPromptActivity})
}

// ApprovalButtonValue is encoded in each Approve/Deny button's value field so the
// interaction webhook can reconstruct the decision without server-side state.
// Compact single-letter JSON keys keep the encoded string short.
type ApprovalButtonValue struct {
	SessionID string `json:"s"`
	ToolID    string `json:"t"`
	ToolName  string `json:"n"`
	Approved  bool   `json:"a"`
}

// parseChannel extracts the channel from a session ID. Sessions are
// "provider:channel" or, when thread-scoped, "provider:channel:threadRoot"
// (e.g. "slack:C12345" or "slack:C12345:1699.0001" → "C12345"). The channel is
// always the second colon-delimited segment; a trailing thread root is ignored.
func parseChannel(sessionID string) (string, error) {
	parts := strings.SplitN(sessionID, ":", 3)
	if len(parts) < 2 || parts[1] == "" {
		return "", fmt.Errorf("invalid session ID %q: expected \"provider:channel[:threadRoot]\" format", sessionID)
	}
	return parts[1], nil
}

// mrkdwnLink renders a link in Slack's classic mrkdwn syntax, used by the chat.postMessage
// "text" field (and Block Kit "mrkdwn" text objects).
func mrkdwnLink(url, title string) string {
	return fmt.Sprintf("<%s|%s>", url, title)
}

// SlackPlatform is the real Activity implementation backing Driver: its methods make
// the actual Slack API calls and are registered on the worker under the activity names
// Driver dispatches to. It also exposes additional Slack-specific methods not covered
// by router.OutboundDriver.
type SlackPlatform struct {
	client *slackapi.Client
	teamID string
}

// NewSlackPlatform creates a SlackPlatform from a Slack client and team ID.
func NewSlackPlatform(client *slackapi.Client, teamID string) *SlackPlatform {
	return &SlackPlatform{client: client, teamID: teamID}
}

// BeginStream opens a native Slack stream and returns the stream's ID as part of
// router.StreamHandle so we can later use append to the stream via UpdateStream(...).
func (p *SlackPlatform) BeginStream(ctx context.Context, input router.BeginStreamInput) (router.StreamHandle, error) {
	channel, err := parseChannel(input.SessionID)
	if err != nil {
		return router.StreamHandle{}, err
	}

	opts := []slackapi.MsgOption{slackapi.MsgOptionStartStream()}
	if input.ThreadID != "" {
		opts = append(opts, slackapi.MsgOptionTS(input.ThreadID))
	}
	if input.SenderID != "" {
		opts = append(opts, slackapi.MsgOptionRecipientUserID(input.SenderID))
	}
	if p.teamID != "" {
		opts = append(opts, slackapi.MsgOptionRecipientTeamID(p.teamID))
	}
	_, ts, err := p.client.StartStreamContext(ctx, channel, opts...)
	if err != nil {
		return router.StreamHandle{}, fmt.Errorf("chat.startStream: %w", err)
	}
	return router.StreamHandle{
		ID:                  ts,
		SessionID:           input.SessionID,
		TransportMode:       "native",
		CloseBeforeApproval: false,
	}, nil
}

// UpdateStream appends the pending agent delta to a native Slack stream, rendering
// whichever of Delta/ToolStatus/ThoughtSummary is present - see flattenForDisplay. This
// live view is independent of the citation-indexed text FinishStream corrects the
// message to: that always rebuilds from clean reply-only text, so whatever gets shown
// here (including tool status/thinking) is transient and gets replaced once the turn
// ends, regardless of what it looked like while streaming.
func (p *SlackPlatform) UpdateStream(ctx context.Context, input router.UpdateStreamInput) error {
	channel, err := parseChannel(input.SessionID)
	if err != nil {
		return err
	}
	if input.Handle.ID == "" {
		return errors.New("stream handle ID is required")
	}
	if input.Handle.SessionID != input.SessionID {
		return errors.New("stream handle session does not match input session")
	}
	text := flattenForDisplay(input)
	if text == "" {
		return nil
	}
	if _, _, err := p.client.AppendStreamContext(ctx, channel, input.Handle.ID,
		slackapi.MsgOptionMarkdownText(text),
	); err != nil {
		return fmt.Errorf("chat.appendStream: %w", err)
	}
	return nil
}

// flattenForDisplay renders one delta's content for the live stream view. Slack's SDK
// has no equivalent of chat.startStream's rich "task_update" chunk (checked both the
// pinned and latest slack-go releases - neither implements it), so tool status renders
// as plain inline text, the same as it always did.
func flattenForDisplay(input router.UpdateStreamInput) string {
	return flattenDeltaText(router.Delta{
		Text:           input.Delta,
		ToolStatus:     input.ToolStatus,
		ThoughtSummary: input.ThoughtSummary,
	})
}

// flattenDeltaText renders one delta's content as plain inline text - the same
// rendering flattenForDisplay uses live, reused here so the final message (built from
// Segments) shows tool status/thought summaries identically to how they looked while
// streaming, instead of being dropped.
func flattenDeltaText(d router.Delta) string {
	switch {
	case d.ToolStatus != nil:
		switch d.ToolStatus.Status {
		case router.ToolStarted:
			return "\n_" + d.ToolStatus.ToolName + "..._"
		case router.ToolCompleted:
			return " ✅\n\n"
		case router.ToolErrored:
			return " ❌ Error: " + d.ToolStatus.Message + "\n\n"
		}
		return ""
	case d.ThoughtSummary != "":
		return d.ThoughtSummary
	default:
		return d.Text
	}
}

// flattenSegments concatenates every delta's flattened text in order, producing the
// full message text (reply + tool status + thought summaries) for the final,
// citation-spliced message.
func flattenSegments(segments []router.Delta) string {
	var b strings.Builder
	for _, d := range segments {
		b.WriteString(flattenDeltaText(d))
	}
	return b.String()
}

// retargetCitations rewrites each citation's EndIndex from an offset into the reply
// text alone (see router.Delta.Text) to the matching offset in flattenSegments' output,
// which interleaves tool status/thought summary text between reply chunks.
func retargetCitations(segments []router.Delta, cs []router.Citation) []router.Citation {
	if len(cs) == 0 {
		return cs
	}
	retargeted := make([]router.Citation, len(cs))
	for i, c := range cs {
		c.EndIndex = retargetEndIndex(segments, c.EndIndex)
		retargeted[i] = c
	}
	return retargeted
}

func retargetEndIndex(segments []router.Delta, endIndex int) int {
	if endIndex < 0 {
		return -1
	}
	var replyOffset, flatOffset int
	for _, seg := range segments {
		if seg.Text != "" {
			replyLen := len([]rune(seg.Text))
			if endIndex <= replyOffset+replyLen {
				return flatOffset + (endIndex - replyOffset)
			}
			replyOffset += replyLen
		}
		flatOffset += len([]rune(flattenDeltaText(seg)))
	}
	return flatOffset
}

// FinishStream stops a native Slack stream, then corrects the message to the full
// flattened turn (reply text plus tool status/thought summaries, see flattenSegments)
// with citation markers spliced in at their retargeted positions - overwriting whatever
// was live-appended, so the final message keeps the same tool-use lines shown live
// instead of dropping them. Skips the correction if there's no content at all, since
// chat.update rejects an empty markdown_text; the live-streamed text stands instead.
func (p *SlackPlatform) FinishStream(ctx context.Context, input router.FinishStreamInput) error {
	channel, err := parseChannel(input.SessionID)
	if err != nil {
		return err
	}
	if input.Handle.ID == "" {
		return errors.New("stream handle ID is required")
	}
	if input.Handle.SessionID != input.SessionID {
		return errors.New("stream handle session does not match input session")
	}
	if _, _, err := p.client.StopStreamContext(ctx, channel, input.Handle.ID); err != nil {
		return fmt.Errorf("chat.stopStream: %w", err)
	}
	text := flattenSegments(input.Segments)
	finalText := citations.Splice(text, retargetCitations(input.Segments, input.Citations), citations.CommonMarkLink)
	if finalText == "" {
		return nil
	}
	if _, _, _, err := p.client.UpdateMessageContext(ctx, channel, input.Handle.ID,
		slackapi.MsgOptionMarkdownText(finalText),
	); err != nil {
		return fmt.Errorf("chat.update: %w", err)
	}
	return nil
}

func (p *SlackPlatform) PostMessage(ctx context.Context, input router.TextMetadata) error {
	channel, err := parseChannel(input.SessionID)
	if err != nil {
		return err
	}
	text := flattenSegments(input.Segments)
	opts := []slackapi.MsgOption{slackapi.MsgOptionText(citations.Splice(text, retargetCitations(input.Segments, input.Citations), mrkdwnLink), false)}
	if input.ThreadID != "" {
		opts = append(opts, slackapi.MsgOptionTS(input.ThreadID))
	}
	_, _, err = p.client.PostMessageContext(ctx, channel, opts...)
	if err != nil {
		return fmt.Errorf("chat.postMessage: %w", err)
	}
	return nil
}

func (p *SlackPlatform) PostApprovalPrompt(ctx context.Context, input router.ApprovalPromptInput) error {
	channel, err := parseChannel(input.SessionID)
	if err != nil {
		return err
	}

	// Encode session/tool info into each button's value so the interaction
	// callback can reconstruct the decision without server-side state.
	approveBytes, _ := json.Marshal(ApprovalButtonValue{SessionID: input.SessionID, ToolID: input.ToolID, ToolName: input.ToolName, Approved: true})
	denyBytes, _ := json.Marshal(ApprovalButtonValue{SessionID: input.SessionID, ToolID: input.ToolID, ToolName: input.ToolName, Approved: false})

	text := fmt.Sprintf("🔐 *Tool approval required* — `%s`", input.ToolName)
	if input.ToolInput != "" && input.ToolInput != "{}" && input.ToolInput != "null" {
		text += fmt.Sprintf("\n```%s```", input.ToolInput)
	}

	approveBtn := slackapi.NewButtonBlockElement("tool_approval_approve", string(approveBytes),
		slackapi.NewTextBlockObject("plain_text", "✅ Approve", false, false)).
		WithStyle(slackapi.StylePrimary)
	denyBtn := slackapi.NewButtonBlockElement("tool_approval_deny", string(denyBytes),
		slackapi.NewTextBlockObject("plain_text", "❌ Deny", false, false)).
		WithStyle(slackapi.StyleDanger)

	blocks := []slackapi.Block{
		slackapi.NewSectionBlock(slackapi.NewTextBlockObject("mrkdwn", text, false, false), nil, nil),
		slackapi.NewActionBlock("tool_approval", approveBtn, denyBtn),
	}
	opts := []slackapi.MsgOption{slackapi.MsgOptionBlocks(blocks...)}
	if input.ThreadID != "" {
		opts = append(opts, slackapi.MsgOptionTS(input.ThreadID))
	}
	_, _, err = p.client.PostMessageContext(ctx, channel, opts...)
	return err
}

// --- Slack-specific methods not covered by router.OutboundDriver ---

type FetchMessagesOutput struct {
	Messages []MessageElement
}

type MessageElement struct {
	MessageID string
	Sender    string
	Text      string
	Timestamp string
}

func (p *SlackPlatform) FetchMessages(ctx context.Context, channel string, limit int, senderFilter string) (FetchMessagesOutput, error) {
	if channel == "" {
		return FetchMessagesOutput{}, errors.New("channel is required")
	}
	if limit <= 0 {
		limit = 10
	}
	fetchLimit := limit
	if senderFilter != "" {
		fetchLimit = min(limit*5, 200)
	}
	history, err := p.client.GetConversationHistoryContext(ctx, &slackapi.GetConversationHistoryParameters{
		ChannelID: channel,
		Limit:     fetchLimit,
	})
	if err != nil {
		return FetchMessagesOutput{}, fmt.Errorf("conversations.history: %w", err)
	}
	var matched []MessageElement
	for _, m := range history.Messages {
		if senderFilter != "" && m.User != senderFilter {
			continue
		}
		matched = append(matched, MessageElement{
			MessageID: m.Timestamp,
			Sender:    m.User,
			Text:      m.Text,
			Timestamp: m.Timestamp,
		})
		if len(matched) >= limit {
			break
		}
	}
	for i, j := 0, len(matched)-1; i < j; i, j = i+1, j-1 {
		matched[i], matched[j] = matched[j], matched[i]
	}
	return FetchMessagesOutput{Messages: matched}, nil
}

type ListChannelsOutput struct {
	Channels   []ChannelElement
	NextCursor string
}

type ChannelElement struct {
	ID        string
	Name      string
	Topic     string
	IsPrivate bool
}

func (p *SlackPlatform) ListChannels(ctx context.Context, cursor string, limit int) (ListChannelsOutput, error) {
	if limit == 0 {
		limit = 100
	}
	chans, nextCursor, err := p.client.GetConversationsContext(ctx, &slackapi.GetConversationsParameters{
		Cursor: cursor,
		Limit:  limit,
		Types:  []string{"public_channel", "private_channel"},
	})
	if err != nil {
		return ListChannelsOutput{}, fmt.Errorf("conversations.list: %w", err)
	}
	channels := make([]ChannelElement, len(chans))
	for i, ch := range chans {
		channels[i] = ChannelElement{
			ID:        ch.ID,
			Name:      ch.Name,
			IsPrivate: ch.IsPrivate,
			Topic:     ch.Topic.Value,
		}
	}
	return ListChannelsOutput{Channels: channels, NextCursor: nextCursor}, nil
}

type PostPromptInput struct {
	Channel  string
	PromptID string
	Text     string
	ThreadID string
	Type     string // "text", "confirm", or "choose"
	Options  []PromptOption
}

type PromptOption struct {
	Label string
	Value string
}

type PostPromptOutput struct {
	MessageID string
	ThreadID  string
}

func (p *SlackPlatform) PostPrompt(ctx context.Context, in PostPromptInput) (PostPromptOutput, error) {
	if in.Channel == "" {
		return PostPromptOutput{}, errors.New("channel is required")
	}
	var blocks []slackapi.Block
	switch in.Type {
	case "text":
		blocks = append(blocks,
			slackapi.NewSectionBlock(slackapi.NewTextBlockObject("mrkdwn", in.Text, false, false), nil, nil),
		)
	case "confirm":
		blocks = append(blocks,
			slackapi.NewSectionBlock(slackapi.NewTextBlockObject("mrkdwn", in.Text, false, false), nil, nil),
			slackapi.NewActionBlock(in.PromptID,
				slackapi.NewButtonBlockElement(in.PromptID+"-yes", "true",
					slackapi.NewTextBlockObject("plain_text", "Yes", false, false)),
				slackapi.NewButtonBlockElement(in.PromptID+"-no", "false",
					slackapi.NewTextBlockObject("plain_text", "No", false, false)),
			),
		)
	case "choose":
		var optObjs []*slackapi.OptionBlockObject
		for _, opt := range in.Options {
			optObjs = append(optObjs, slackapi.NewOptionBlockObject(
				opt.Value,
				slackapi.NewTextBlockObject("plain_text", opt.Label, false, false),
				nil,
			))
		}
		blocks = append(blocks,
			slackapi.NewSectionBlock(slackapi.NewTextBlockObject("mrkdwn", in.Text, false, false), nil, nil),
			slackapi.NewActionBlock(in.PromptID,
				slackapi.NewOptionsSelectBlockElement(
					slackapi.OptTypeStatic,
					slackapi.NewTextBlockObject("plain_text", "Choose...", false, false),
					in.PromptID+"-select",
					optObjs...,
				),
			),
		)
	default:
		return PostPromptOutput{}, fmt.Errorf("unknown prompt type: %s", in.Type)
	}
	opts := []slackapi.MsgOption{slackapi.MsgOptionBlocks(blocks...)}
	if in.ThreadID != "" {
		opts = append(opts, slackapi.MsgOptionTS(in.ThreadID))
	}
	_, ts, err := p.client.PostMessageContext(ctx, in.Channel, opts...)
	if err != nil {
		return PostPromptOutput{}, fmt.Errorf("chat.postMessage (prompt): %w", err)
	}
	return PostPromptOutput{MessageID: ts, ThreadID: ts}, nil
}
