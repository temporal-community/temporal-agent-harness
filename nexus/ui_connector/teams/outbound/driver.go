// Package teamsoutbound shows agent output in Teams through the Python Teams activity
// worker.
package teamsoutbound

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/markdown"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/timeline"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/teams"
	"go.temporal.io/sdk/workflow"
)

// Activity names registered by the Python worker.
const (
	beginStreamActivity        = "BeginStream"
	updateStreamActivity       = "UpdateStream"
	finishStreamActivity       = "FinishStream"
	postMessageActivity        = "PostMessage"
	postApprovalPromptActivity = "PostApprovalPrompt"
	updateMessageActivity      = "UpdateActivity"
)

const pinnedActivityScheduleToStartTimeout = 10 * time.Second

// Driver implements router.Outbound. It runs the timeline in workflow code and calls
// the Python worker's activities. Each stream is pinned to the worker that opened it.
type Driver struct {
	ActivityOptions workflow.ActivityOptions
}

var _ router.Outbound = Driver{}

func NewDriver(opts workflow.ActivityOptions) Driver {
	return Driver{ActivityOptions: opts}
}

func (d Driver) Deliver(ctx workflow.Context, address router.Address, state router.State, events []protocol.Event) (router.State, error) {
	return d.apply(ctx, address, state, func(cfg timeline.Config, s timeline.Sink, st *timeline.State) {
		timeline.Apply(cfg, s, st, events)
	})
}

func (d Driver) Done(ctx workflow.Context, address router.Address, state router.State) error {
	if router.Empty(state) {
		return nil
	}
	_, err := d.apply(ctx, address, state, timeline.End)
	return err
}

func (d Driver) apply(ctx workflow.Context, address router.Address, state router.State, run func(timeline.Config, timeline.Sink, *timeline.State)) (router.State, error) {
	var addr teams.Address
	if err := json.Unmarshal(address, &addr); err != nil {
		return nil, fmt.Errorf("decode address: %w", err)
	}
	var st timeline.State
	if !router.Empty(state) {
		if err := json.Unmarshal(state, &st); err != nil {
			return nil, fmt.Errorf("decode state: %w", err)
		}
	}
	run(timeline.Config{Streaming: SupportsStreaming(addr.ConversationType)}, &sink{ctx: ctx, d: d, addr: addr}, &st)
	return json.Marshal(st)
}

// SupportsStreaming reports whether the conversation can show a streamed reply. Teams
// does not stream in channels and group chats.
func SupportsStreaming(conversationType string) bool {
	switch strings.ToLower(strings.TrimSpace(conversationType)) {
	case "channel", "groupchat":
		return false
	default:
		return true
	}
}

// Payloads in the Python worker's format (teams_activity_worker/contracts.py).

type textMetadata struct {
	SenderID   string
	SessionID  string
	ThreadID   string
	Text       string
	ServiceURL string
	ChannelID  string
}

type streamHandle struct {
	ID            string
	SessionID     string
	TransportMode string
	TaskQueue     string
}

type beginStreamPayload struct {
	textMetadata
	ConversationType string
}

type updateStreamPayload struct {
	textMetadata
	Handle streamHandle
	Delta  string
}

type finishStreamPayload struct {
	textMetadata
	Handle streamHandle
}

type approvalPromptPayload struct {
	textMetadata
	ToolID    string
	ToolName  string
	ToolInput string
}

type updateMessagePayload struct {
	textMetadata
	MessageID string
}

// sink calls the Python worker's activities from workflow code.
type sink struct {
	ctx  workflow.Context
	d    Driver
	addr teams.Address
}

var _ timeline.Sink = (*sink)(nil)

func (s *sink) meta(text string) textMetadata {
	return textMetadata{
		SenderID: s.addr.SenderID, SessionID: s.addr.SessionID, ThreadID: s.addr.ThreadID,
		Text: text, ServiceURL: s.addr.ServiceURL, ChannelID: s.addr.ChannelID,
	}
}

func (s *sink) exec(name string, input, result any) error {
	return workflow.ExecuteActivity(workflow.WithActivityOptions(s.ctx, s.d.ActivityOptions), name, input).Get(s.ctx, result)
}

// execPinned runs a stream activity on the worker that holds the stream.
func (s *sink) execPinned(h streamHandle, name string, input any) error {
	if h.TaskQueue == "" {
		return s.exec(name, input, nil)
	}
	opts := s.d.ActivityOptions
	opts.TaskQueue = h.TaskQueue
	opts.ScheduleToStartTimeout = pinnedActivityScheduleToStartTimeout
	return workflow.ExecuteActivity(workflow.WithActivityOptions(s.ctx, opts), name, input).Get(s.ctx, nil)
}

func (s *sink) Now() time.Time { return workflow.Now(s.ctx) }

func (s *sink) Warn(msg string, err error) {
	workflow.GetLogger(s.ctx).Warn("Teams: "+msg, "error", err)
}

// Open opens the stream, then appends the first chunks. The Python worker's
// begin_stream takes no content. The stream ID is the handle as JSON.
func (s *sink) Open(chunks []protocol.Chunk) (string, error) {
	var h streamHandle
	if err := s.exec(beginStreamActivity, beginStreamPayload{textMetadata: s.meta(""), ConversationType: s.addr.ConversationType}, &h); err != nil {
		return "", err
	}
	id, err := json.Marshal(h)
	if err != nil {
		return "", err
	}
	if err := s.Append(string(id), chunks); err != nil {
		return "", err
	}
	return string(id), nil
}

func decodeHandle(stream string) (streamHandle, error) {
	var h streamHandle
	err := json.Unmarshal([]byte(stream), &h)
	return h, err
}

// Append sends the chunks as Markdown. Teams has no cards.
func (s *sink) Append(stream string, chunks []protocol.Chunk) error {
	delta := markdown.RenderChunks(chunks)
	if delta == "" {
		return nil
	}
	h, err := decodeHandle(stream)
	if err != nil {
		return err
	}
	return s.execPinned(h, updateStreamActivity, updateStreamPayload{textMetadata: s.meta(""), Handle: h, Delta: delta})
}

// Finish closes the stream. Streamed citations are not shown: the Python worker only
// closes the stream.
func (s *sink) Finish(stream string, _ timeline.Message) error {
	h, err := decodeHandle(stream)
	if err != nil {
		return err
	}
	return s.execPinned(h, finishStreamActivity, finishStreamPayload{textMetadata: s.meta(""), Handle: h})
}

// Post renders every chunk as Markdown with citation markers.
func (s *sink) Post(m timeline.Message) error {
	return s.postText(markdown.SpliceChunks(m.Chunks, m.Citations, markdown.CommonMarkLink))
}

func (s *sink) postText(text string) error {
	if text == "" {
		return nil
	}
	return s.exec(postMessageActivity, s.meta(text), nil)
}

// Status does nothing: Teams has no status line.
func (s *sink) Status(timeline.Status) error { return nil }

// Prompt posts the approval card. The Python worker returns no ID; the card is updated
// from the click (see Settle).
func (s *sink) Prompt(req protocol.ApprovalRequest) (string, error) {
	return "", s.exec(postApprovalPromptActivity, approvalPromptPayload{
		textMetadata: s.meta(""), ToolID: req.ToolID, ToolName: req.ToolName, ToolInput: req.ToolInputJSON,
	}, nil)
}

// Settle updates the card that a click came from with the decision.
func (s *sink) Settle(_ timeline.Prompt, res protocol.ApprovalResolution) error {
	if s.addr.ActivityID == "" {
		return nil
	}
	decision := "❌ Denied"
	if res.Approved {
		decision = "✅ Approved"
	}
	return s.exec(updateMessageActivity, updateMessagePayload{
		textMetadata: s.meta(fmt.Sprintf("🔐 Tool `%s`: %s", res.ToolName, decision)),
		MessageID:    s.addr.ActivityID,
	}, nil)
}

func (s *sink) Guide(handlers []protocol.Handler) error {
	return s.postText(markdown.Guide(handlers))
}

// Form posts the handler's usage: Teams has no forms here.
func (s *sink) Form(h protocol.Handler) error {
	return s.postText(markdown.GuideEntry(h))
}

func (s *sink) Error(msg string) error {
	return s.postText("_Something went wrong handling that: " + msg + "_")
}
