package slack

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	slackapi "github.com/slack-go/slack"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

// Activity name constants under which SlackPlatform's methods must be registered on
// the worker (see cmd/worker). Private to this package: RouterWorkflow never sees
// these — it only calls Driver, and a different inbound driver is free to use a
// completely different activity shape internally, or none at all.
const (
	beginStreamActivity        = "SlackBeginStream"
	updateStreamActivity       = "SlackUpdateStream"
	finishStreamActivity       = "SlackFinishStream"
	postMessageActivity        = "SlackPostMessage"
	postApprovalPromptActivity = "SlackPostApprovalPrompt"
)

// Driver implements inbound.Driver for Slack by dispatching to Activities backed by
// SlackPlatform. It's the bridge between RouterWorkflow (workflow.Context calls) and
// the real Slack API calls, which must run as Activities since they're non-deterministic
// I/O.
type Driver struct {
	ActivityOptions workflow.ActivityOptions
}

// NewDriver returns a Driver that calls the Slack activities with the given options.
func NewDriver(opts workflow.ActivityOptions) Driver {
	return Driver{ActivityOptions: opts}
}

func (d Driver) BeginStream(ctx workflow.Context, input inbound.BeginStreamInput) (inbound.StreamHandle, error) {
	var handle inbound.StreamHandle
	err := workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), beginStreamActivity, input,
	).Get(ctx, &handle)
	return handle, err
}

func (d Driver) UpdateStream(ctx workflow.Context, input inbound.UpdateStreamInput) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), updateStreamActivity, input,
	).Get(ctx, nil)
}

func (d Driver) FinishStream(ctx workflow.Context, input inbound.FinishStreamInput) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), finishStreamActivity, input,
	).Get(ctx, nil)
}

func (d Driver) PostMessage(ctx workflow.Context, input inbound.TextMetadata) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), postMessageActivity, input,
	).Get(ctx, nil)
}

func (d Driver) PostApprovalPrompt(ctx workflow.Context, input inbound.ApprovalPromptInput) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions), postApprovalPromptActivity, input,
	).Get(ctx, nil)
}

func (d Driver) UpdateActivity(workflow.Context, inbound.UpdateActivityInput) error {
	return nil
}

// RegisterActivities registers platform's methods on w under the activity names Driver
// dispatches to. Call this from the worker binary alongside NewDriver.
func RegisterActivities(w worker.Worker, platform *SlackPlatform) {
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

// parseChannel strips the provider prefix from a session ID (e.g. "slack:C12345" → "C12345").
func parseChannel(sessionID string) (string, error) {
	_, ch, found := strings.Cut(sessionID, ":")
	if !found || ch == "" {
		return "", fmt.Errorf("invalid session ID %q: expected \"provider:id\" format", sessionID)
	}
	return ch, nil
}

// SlackPlatform is the real Activity implementation backing Driver: its methods make
// the actual Slack API calls and are registered on the worker under the activity names
// Driver dispatches to. It also exposes additional Slack-specific methods not covered
// by inbound.Driver.
type SlackPlatform struct {
	client *slackapi.Client
	teamID string
}

// NewSlackPlatform creates a SlackPlatform from a Slack client and team ID.
func NewSlackPlatform(client *slackapi.Client, teamID string) *SlackPlatform {
	return &SlackPlatform{client: client, teamID: teamID}
}

// BeginStream opens a native Slack stream and returns the stream's ID as part of
// inbound.StreamHandle so we can later use append to the stream via UpdateStream(...).
func (p *SlackPlatform) BeginStream(ctx context.Context, input inbound.BeginStreamInput) (inbound.StreamHandle, error) {
	channel, err := parseChannel(input.SessionID)
	if err != nil {
		return inbound.StreamHandle{}, err
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
		return inbound.StreamHandle{}, fmt.Errorf("chat.startStream: %w", err)
	}
	return inbound.StreamHandle{
		ID:                  ts,
		SessionID:           input.SessionID,
		TransportMode:       "native",
		WireTextMode:        inbound.StreamWireTextDelta,
		CloseBeforeApproval: false,
	}, nil
}

// UpdateStream appends the pending agent delta to a native Slack stream.
// An ID that identifies the stream is required.
func (p *SlackPlatform) UpdateStream(ctx context.Context, input inbound.UpdateStreamInput) error {
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
	if input.Delta == "" {
		return nil
	}
	if _, _, err := p.client.AppendStreamContext(ctx, channel, input.Handle.ID,
		slackapi.MsgOptionMarkdownText(input.Delta),
	); err != nil {
		return fmt.Errorf("chat.appendStream: %w", err)
	}
	return nil
}

// FinishStream stops and finalises a native Slack stream.
// An ID that identifies the stream is required.
func (p *SlackPlatform) FinishStream(ctx context.Context, input inbound.FinishStreamInput) error {
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
	return nil
}

func (p *SlackPlatform) PostMessage(ctx context.Context, input inbound.TextMetadata) error {
	channel, err := parseChannel(input.SessionID)
	if err != nil {
		return err
	}
	opts := []slackapi.MsgOption{slackapi.MsgOptionText(input.Text, false)}
	if input.ThreadID != "" {
		opts = append(opts, slackapi.MsgOptionTS(input.ThreadID))
	}
	_, _, err = p.client.PostMessageContext(ctx, channel, opts...)
	if err != nil {
		return fmt.Errorf("chat.postMessage: %w", err)
	}
	return nil
}

func (p *SlackPlatform) PostApprovalPrompt(ctx context.Context, input inbound.ApprovalPromptInput) error {
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

// --- Slack-specific methods not covered by inbound.Driver ---

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
