package slackoutbound

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"sync/atomic"
	"time"

	slackapi "github.com/slack-go/slack"
	"go.temporal.io/sdk/activity"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/markdown"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/timeline"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/presentation"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/interaction"
)

// DefaultLimits sets size and duration limits for streamed messages.
var DefaultLimits = timeline.Limits{
	// Slack ends a stream after about 5 minutes.
	MaxAge: 4 * time.Minute,
	// Limit rendered text and card details to reduce msg_too_long errors.
	MaxChars: 2500,
	// Each card is one task_card block. Slack allows 50 blocks per message.
	MaxCards: 45,
}

// SlackPlatform runs the SlackDeliver activity.
type SlackPlatform struct {
	client *slackapi.Client
	teamID string

	// TaskDisplayMode sets chat.startStream's task_display_mode: "timeline" or "plan".
	// Empty uses Slack's default.
	TaskDisplayMode slackapi.TaskDisplayMode
	Limits          timeline.Limits

	// cardsUnsupported switches this worker to text after a card fails and text succeeds.
	cardsUnsupported atomic.Bool
	// statusUnsupported disables status updates in this worker after an API error.
	statusUnsupported atomic.Bool
}

// NewSlackPlatform creates a SlackPlatform with DefaultLimits.
func NewSlackPlatform(client *slackapi.Client, teamID string) *SlackPlatform {
	return &SlackPlatform{client: client, teamID: teamID, Limits: DefaultLimits}
}

// Deliver applies one batch of events and returns the updated timeline state.
func (p *SlackPlatform) Deliver(ctx context.Context, in DeliverInput) (json.RawMessage, error) {
	var addr slack.Address
	if err := json.Unmarshal(in.Address, &addr); err != nil {
		return nil, fmt.Errorf("decode address: %w", err)
	}
	var st timeline.State
	if !router.Empty(in.State) {
		if err := json.Unmarshal(in.State, &st); err != nil {
			return nil, fmt.Errorf("decode state: %w", err)
		}
	}
	s := &sink{ctx: ctx, p: p, addr: addr}
	cfg := timeline.Config{Streaming: true, Limits: p.Limits, Size: chunkSize}
	if in.End {
		timeline.End(cfg, s, &st)
	} else {
		timeline.Apply(cfg, s, &st, in.Events)
	}
	return json.Marshal(st)
}

// cardOverhead approximates Slack's formatting around one card.
const cardOverhead = 64

// chunkSize measures a chunk as Slack shows it: reply text in full, a card as its title
// plus the arguments and error that taskCard sends.
func chunkSize(c protocol.Chunk) int {
	switch {
	case c.Tool != nil:
		size := cardOverhead + runes(c.Tool.Name) + min(runes(c.Tool.Input), cardDetailsMax)
		if c.Tool.Phase == protocol.ToolErrored {
			size += min(runes(c.Tool.Error), cardErrorMax)
		}
		return size
	case c.Thought != nil:
		return cardOverhead + min(runes(c.Thought.Text), cardDetailsMax)
	default:
		return runes(c.Text)
	}
}

func runes(s string) int { return len([]rune(s)) }

// sink makes the Slack API calls for one delivery.
type sink struct {
	ctx  context.Context
	p    *SlackPlatform
	addr slack.Address
}

var _ timeline.Sink = (*sink)(nil)

func (s *sink) Now() time.Time { return time.Now() }

func (s *sink) Warn(msg string, err error) {
	activity.GetLogger(s.ctx).Warn("SlackDeliver: "+msg, "error", err)
}

func (s *sink) thread() []slackapi.MsgOption {
	if s.addr.ThreadTS == "" {
		return nil
	}
	return []slackapi.MsgOption{slackapi.MsgOptionTS(s.addr.ThreadTS)}
}

// Open starts a stream with its first chunks. Rejected cards are retried as text.
func (s *sink) Open(chunks []protocol.Chunk) (string, error) {
	opts := append([]slackapi.MsgOption{slackapi.MsgOptionStartStream()}, s.thread()...)
	if s.addr.UserID != "" {
		opts = append(opts, slackapi.MsgOptionRecipientUserID(s.addr.UserID))
	}
	if s.p.teamID != "" {
		opts = append(opts, slackapi.MsgOptionRecipientTeamID(s.p.teamID))
	}
	if s.p.TaskDisplayMode != "" {
		opts = append(opts, slackapi.MsgOptionTaskDisplayMode(s.p.TaskDisplayMode))
	}
	start := func(content []slackapi.StreamChunk) (string, error) {
		withContent := opts
		if len(content) > 0 {
			withContent = append(append([]slackapi.MsgOption(nil), opts...), slackapi.MsgOptionChunks(content...))
		}
		_, ts, err := s.p.client.StartStreamContext(s.ctx, s.addr.Channel, withContent...)
		return ts, err
	}

	cards := !s.p.cardsUnsupported.Load()
	ts, err := start(streamChunks(chunks, cards))
	if err != nil && cards && hasCards(chunks) {
		if textTS, retryErr := start(streamChunks(chunks, false)); retryErr == nil {
			s.p.cardsUnsupported.Store(true)
			ts, err = textTS, nil
		}
	}
	if err != nil {
		return "", fmt.Errorf("chat.startStream: %w", err)
	}
	return ts, nil
}

// Append sends text and task cards. Card IDs identify updates within a message. If a
// card fails and the same content succeeds as text, later calls use text.
func (s *sink) Append(stream string, chunks []protocol.Chunk) error {
	cards := !s.p.cardsUnsupported.Load()
	content := streamChunks(chunks, cards)
	if len(content) == 0 {
		return nil
	}
	_, _, err := s.p.client.AppendStreamContext(s.ctx, s.addr.Channel, stream, slackapi.MsgOptionChunks(content...))
	if err == nil {
		return nil
	}
	if !cards || !hasCards(chunks) {
		return fmt.Errorf("chat.appendStream: %w", err)
	}
	text := streamChunks(chunks, false)
	if len(text) == 0 {
		return nil
	}
	if _, _, retryErr := s.p.client.AppendStreamContext(s.ctx, s.addr.Channel, stream, slackapi.MsgOptionChunks(text...)); retryErr != nil {
		return fmt.Errorf("chat.appendStream: %w", err)
	}
	s.p.cardsUnsupported.Store(true)
	return nil
}

// Finish closes the stream and adds citations. A message with cards gets a Sources
// list, because chat.update would replace the cards. A text message gets inline
// citation markers.
func (s *sink) Finish(stream string, m timeline.Message) error {
	withCards := !s.p.cardsUnsupported.Load() && hasCards(m.Chunks)
	var stopOpts []slackapi.MsgOption
	if withCards {
		if footer := markdown.SourcesFooter(m.Citations, markdown.CommonMarkLink); footer != "" {
			// The stream uses chunks, so the stop must send chunks too. Slack rejects
			// markdown_text on a chunks stream with streaming_mode_mismatch.
			stopOpts = append(stopOpts, slackapi.MsgOptionChunks(slackapi.NewMarkdownTextChunk(footer)))
		}
	}
	if _, _, err := s.p.client.StopStreamContext(s.ctx, s.addr.Channel, stream, stopOpts...); err != nil {
		return fmt.Errorf("chat.stopStream: %w", err)
	}
	if withCards || len(m.Citations) == 0 {
		return nil
	}
	finalText := markdown.SpliceChunks(m.Chunks, m.Citations, markdown.CommonMarkLink)
	if finalText == "" {
		// chat.update rejects an empty markdown_text.
		return nil
	}
	if _, _, _, err := s.p.client.UpdateMessageContext(s.ctx, s.addr.Channel, stream,
		slackapi.MsgOptionMarkdownText(finalText),
	); err != nil {
		return fmt.Errorf("chat.update: %w", err)
	}
	return nil
}

// Post sends a message with every chunk as text.
func (s *sink) Post(m timeline.Message) error {
	text := markdown.SpliceChunks(m.Chunks, m.Citations, mrkdwnLink)
	if text == "" {
		text = m.Text
	}
	return s.postText(text)
}

func (s *sink) postText(text string) error {
	if text == "" {
		return nil
	}
	opts := append([]slackapi.MsgOption{slackapi.MsgOptionText(text, false)}, s.thread()...)
	if _, _, err := s.p.client.PostMessageContext(s.ctx, s.addr.Channel, opts...); err != nil {
		return fmt.Errorf("chat.postMessage: %w", err)
	}
	return nil
}

// Status updates the thread status line. Slack rotates through loading_messages.
// slack-go separates messages with commas, so each message must avoid commas.
func (s *sink) Status(st timeline.Status) error {
	if s.addr.ThreadTS == "" || s.p.statusUnsupported.Load() {
		return nil
	}
	text, messages := statusText(st)
	params := slackapi.AssistantThreadsSetStatusParameters{
		ChannelID:       s.addr.Channel,
		ThreadTS:        s.addr.ThreadTS,
		Status:          text,
		LoadingMessages: messages,
	}
	if err := s.p.client.SetAssistantThreadsStatusContext(s.ctx, params); err != nil {
		s.p.statusUnsupported.Store(true)
		return fmt.Errorf("assistant.threads.setStatus (status line now off for this worker): %w", err)
	}
	return nil
}

// Prompt posts the tool, its arguments, and three buttons. It returns the prompt's ts.
func (s *sink) Prompt(req protocol.ApprovalRequest) (string, error) {
	opts := append([]slackapi.MsgOption{
		slackapi.MsgOptionText(fmt.Sprintf("Tool approval required: %s", req.ToolName), false),
		slackapi.MsgOptionBlocks(approvalPromptBlocks(s.addr.SessionID, req)...),
	}, s.thread()...)
	_, ts, err := s.p.client.PostMessageContext(s.ctx, s.addr.Channel, opts...)
	if err != nil {
		return "", fmt.Errorf("chat.postMessage (approval): %w", err)
	}
	return ts, nil
}

// Settle deletes an approved prompt and replaces a denied prompt with the decision.
// If the delete fails, the prompt is replaced so no buttons stay. A prompt that this
// turn did not post is not changed: the turn that posted it settles it.
func (s *sink) Settle(p timeline.Prompt, res protocol.ApprovalResolution) error {
	if p.ID == "" {
		return nil
	}
	if res.Approved && res.Remember {
		defer func() {
			if err := s.postText(allowListedNote(res.ToolName, res.Reason)); err != nil {
				s.Warn("allow-list note failed", err)
			}
		}()
	}
	if res.Approved {
		if _, _, err := s.p.client.DeleteMessageContext(s.ctx, s.addr.Channel, p.ID); err == nil {
			return nil
		}
	}
	verdict := "Denied"
	if res.Approved {
		verdict = "Approved"
	}
	if _, _, _, err := s.p.client.UpdateMessageContext(s.ctx, s.addr.Channel, p.ID,
		slackapi.MsgOptionText(fmt.Sprintf("%s: %s", verdict, res.ToolName), false),
		slackapi.MsgOptionBlocks(settledApprovalBlocks(p, res)...),
	); err != nil {
		return fmt.Errorf("chat.update (approval): %w", err)
	}
	return nil
}

// Guide posts handler descriptions, possible values, and form buttons.
func (s *sink) Guide(handlers []protocol.Handler) error {
	opts := append([]slackapi.MsgOption{
		slackapi.MsgOptionText("What this agent accepts", false),
		slackapi.MsgOptionBlocks(guideBlocks(s.addr.SessionID, handlers)...),
	}, s.thread()...)
	if _, _, err := s.p.client.PostMessageContext(s.ctx, s.addr.Channel, opts...); err != nil {
		return fmt.Errorf("chat.postMessage (guide): %w", err)
	}
	return nil
}

// Form opens the handler's modal. The trigger_id expires about 3 seconds after the
// click.
func (s *sink) Form(h protocol.Handler) error {
	meta := interaction.FormMetadata{SessionID: s.addr.SessionID, ThreadID: s.addr.ThreadTS, Handler: h.Name}
	if _, err := s.p.client.OpenViewContext(s.ctx, s.addr.TriggerID, interaction.BuildFormView(meta, h.Description, h.Fields)); err != nil {
		return fmt.Errorf("views.open: %w", err)
	}
	return nil
}

func (s *sink) Error(msg string) error {
	return s.postText(errorText(msg))
}

func mrkdwnLink(url, title string) string {
	return fmt.Sprintf("<%s|%s>", url, title)
}

func hasCards(chunks []protocol.Chunk) bool {
	for _, c := range chunks {
		if c.Tool != nil || c.Thought != nil {
			return true
		}
	}
	return false
}

// streamChunks converts content to Slack chunks and merges adjacent text.
func streamChunks(chunks []protocol.Chunk, cards bool) []slackapi.StreamChunk {
	var out []slackapi.StreamChunk
	var text strings.Builder
	flushText := func() {
		if text.Len() > 0 {
			out = append(out, slackapi.NewMarkdownTextChunk(text.String()))
			text.Reset()
		}
	}
	for _, c := range chunks {
		if card, ok := taskCard(c); ok && cards {
			flushText()
			out = append(out, card)
			continue
		}
		text.WriteString(markdown.Render(c))
	}
	flushText()
	return out
}

// Display limits for task card text.
const (
	cardDetailsMax = 800
	cardErrorMax   = 150
)

// taskCard renders a task_update card for a tool call or thought summary.
// Tool cards show arguments and errors. Successful results are omitted.
func taskCard(c protocol.Chunk) (slackapi.TaskUpdateChunk, bool) {
	switch {
	case c.Tool != nil:
		card := slackapi.NewTaskUpdateChunk(c.Tool.ID, c.Tool.Name)
		card.Details = presentation.Clip(prettyJSON(c.Tool.Input), cardDetailsMax)
		switch c.Tool.Phase {
		case protocol.ToolRunning:
			card.Status = slackapi.TaskCardStatusInProgress
		case protocol.ToolComplete:
			card.Status = slackapi.TaskCardStatusComplete
		case protocol.ToolErrored:
			card.Status = slackapi.TaskCardStatusError
			card.Output = presentation.Clip(c.Tool.Error, cardErrorMax)
		}
		return card, true
	case c.Thought != nil:
		card := slackapi.NewTaskUpdateChunk(c.Thought.ID, thinkingTitle)
		card.Details = clipTail(c.Thought.Text, cardDetailsMax)
		card.Status = slackapi.TaskCardStatusInProgress
		if c.Thought.Done {
			card.Status = slackapi.TaskCardStatusComplete
		}
		return card, true
	}
	return slackapi.TaskUpdateChunk{}, false
}

// clipTail keeps the last max runes, with an ellipsis if text was removed.
func clipTail(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	return "…" + string(r[len(r)-max+1:])
}

// prettyJSON indents a JSON object for display, or returns "" for no arguments.
func prettyJSON(s string) string {
	if s == "" || s == "{}" || s == "null" {
		return ""
	}
	var v any
	if err := json.Unmarshal([]byte(s), &v); err != nil {
		return s
	}
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return s
	}
	return string(b)
}
