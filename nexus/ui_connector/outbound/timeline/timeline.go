// Package timeline converts protocol events to messages through a Sink.
// Drivers retain State between deliveries. A Sink supplies the clock and I/O,
// so drivers can run the timeline in workflow code or activities.
package timeline

import (
	"fmt"
	"strings"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/markdown"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/presentation"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

// maxToolText limits tool text kept in State.
const maxToolText = 4000

// Sink makes the platform calls. Errors are logged; they do not stop the timeline.
type Sink interface {
	Now() time.Time
	// Open starts a streamed message with its first chunks and returns its ID.
	Open(chunks []protocol.Chunk) (string, error)
	Append(stream string, chunks []protocol.Chunk) error
	// Finish closes a streamed message. m holds its full content.
	Finish(stream string, m Message) error
	// Post sends a complete message.
	Post(m Message) error
	Status(s Status) error
	// Prompt posts an approval prompt and returns its ID, or "".
	Prompt(req protocol.ApprovalRequest) (string, error)
	// Settle shows a decision. p.ID is "" when this timeline did not post the prompt.
	Settle(p Prompt, res protocol.ApprovalResolution) error
	Guide(handlers []protocol.Handler) error
	Form(h protocol.Handler) error
	Error(msg string) error
	Warn(msg string, err error)
}

// Message is the content of one platform message.
type Message struct {
	// Final is true for the last message closed by End.
	Final     bool
	Chunks    []protocol.Chunk
	Text      string // reply text only; citation offsets refer to it
	Citations []protocol.Citation
	// Suffix closes formatting. Prefix reopens it in the next message. Text excludes both.
	Prefix, Suffix string
}

// Status is the state to show in a status line.
type Status struct {
	// Clear is true when nothing shows: the work ended or a person must decide.
	Clear bool
	// Running holds the name of each running tool call, in start order. Empty with
	// Clear false means the agent is thinking.
	Running []string
}

// Prompt is an approval prompt.
type Prompt struct {
	ID        string
	ToolName  string
	ToolInput string
}

// Limits bounds one streamed message. Zero means no limit.
type Limits struct {
	MaxAge   time.Duration
	MaxChars int // measured by Config.Size
	// MaxOverflow allows a text block to finish after MaxChars.
	MaxOverflow int
	MaxCards    int // distinct tool and thought cards
}

// Config is the platform's choice of behavior.
type Config struct {
	// Streaming opens streamed messages. False posts each message when it ends.
	Streaming bool
	Limits    Limits
	// Size measures a chunk for Limits.MaxChars. A card's size replaces the last size
	// for the same card. Nil counts Markdown characters and adds them up.
	Size func(protocol.Chunk) int
	// SplitText chooses message boundaries before pending text is sent.
	SplitText func(text string, minimum, target, maximum int) markdown.TextBreak
}

// State is the timeline's state between deliveries. It encodes as JSON.
type State struct {
	TextTail   string `json:",omitempty"` // text saved until the next delivery
	TextOffset int    `json:",omitempty"` // original reply text in closed messages
	Segment    Segment
	Thought    *protocol.ThoughtChunk `json:",omitempty"` // open thought block
	ThoughtSeq int                    `json:",omitempty"`
	ToolInputs map[string]string      `json:",omitempty"`
	Prompts    map[string]Prompt      `json:",omitempty"` // by tool ID
	Running    []RunningTool          `json:",omitempty"`
	SawText    bool                   `json:",omitempty"`
	Active     bool                   `json:",omitempty"` // a turn started; show status
	LastStatus string                 `json:",omitempty"`
	NoStream   bool                   `json:",omitempty"` // opening failed; post instead
}

// Segment is the current message.
type Segment struct {
	PrefixSent bool             `json:",omitempty"`
	Prefix     string           `json:",omitempty"`
	Suffix     string           `json:",omitempty"`
	Stream     string           `json:",omitempty"`
	StartedAt  time.Time        `json:",omitzero"`
	Chunks     []protocol.Chunk `json:",omitempty"`
	Chars      int              `json:",omitempty"` // text, and cards when Config.Size is nil
	CardChars  int              `json:",omitempty"` // sum of CardSizes
	CardSizes  map[string]int   `json:",omitempty"`
	Cards      int              `json:",omitempty"`
	CardIDs    map[string]bool  `json:",omitempty"`
	OpenTools  []protocol.Chunk `json:",omitempty"` // latest chunk of each running tool
	Pending    []protocol.Chunk `json:",omitempty"` // not yet sent
	Delivered  []protocol.Chunk `json:",omitempty"` // content at the last successful send
}

type RunningTool struct {
	ID   string
	Name string
}

// Apply shows a batch of events, then updates the status once.
func Apply(cfg Config, sink Sink, st *State, events []protocol.Event) {
	t := timeline{cfg: cfg, sink: sink, st: st}
	for _, ev := range events {
		t.event(ev)
	}
	t.flush()
	t.updateStatus()
	t.maybeRotate()
}

// End closes the open message and clears the status.
func End(cfg Config, sink Sink, st *State) {
	t := timeline{cfg: cfg, sink: sink, st: st}
	t.text("", true)
	t.closeThought()
	t.flush()
	t.final = true
	t.closeSegment()
	t.setStatus(Status{Clear: true})
}

type timeline struct {
	cfg   Config
	sink  Sink
	st    *State
	final bool
}

func (t *timeline) event(ev protocol.Event) {
	if ev.Chunk == nil || ev.Chunk.Text == "" {
		t.text("", true)
	}
	switch {
	case ev.TurnStarted:
		t.st.Active = true
	case ev.Chunk != nil:
		t.chunk(*ev.Chunk)
	case ev.ApprovalRequested != nil:
		t.approvalRequested(*ev.ApprovalRequested)
	case ev.ApprovalResolved != nil:
		t.approvalResolved(*ev.ApprovalResolved)
	case ev.HandlerOutput != nil:
		// Use the return value only if the handler sent no reply text.
		if !t.st.SawText {
			if text := markdown.HandlerOutput(ev.HandlerOutput.Value); text != "" {
				t.chunk(protocol.Chunk{Text: text})
			}
		}
	case ev.HandlerHelp != nil:
		t.chunk(protocol.Chunk{Text: markdown.HandlerHelp(*ev.HandlerHelp)})
	case ev.Guide != nil:
		t.standalone(func() error { return t.sink.Guide(ev.Guide.Handlers) }, "Guide")
	case ev.Form != nil:
		t.standalone(func() error { return t.sink.Form(*ev.Form) }, "Form")
	case ev.Error != "":
		t.standalone(func() error { return t.sink.Error(ev.Error) }, "Error")
	}
}

// standalone ends the current message, then makes a call that posts its own message.
func (t *timeline) standalone(call func() error, name string) {
	t.closeThought()
	t.finishSegment()
	if err := call(); err != nil {
		t.sink.Warn(name+" failed", err)
	}
}

func (t *timeline) chunk(c protocol.Chunk) {
	st := t.st
	switch {
	case c.Thought != nil:
		if c.Thought.Delta == "" {
			return
		}
		if st.Thought == nil {
			st.ThoughtSeq++
			st.Thought = &protocol.ThoughtChunk{ID: fmt.Sprintf("thought-%d", st.ThoughtSeq)}
		}
		st.Thought.Text += c.Thought.Delta
		t.emit(protocol.Chunk{Thought: &protocol.ThoughtChunk{ID: st.Thought.ID, Delta: c.Thought.Delta, Text: st.Thought.Text}})
		return
	case c.Tool == nil && c.Text == "":
		if len(c.Citations) > 0 {
			t.record(c)
		}
		return
	}

	t.closeThought()
	if c.Tool != nil {
		tool := *c.Tool
		if tool.Input != "" {
			if st.ToolInputs == nil {
				st.ToolInputs = map[string]string{}
			}
			st.ToolInputs[tool.ID] = presentation.Clip(tool.Input, maxToolText)
		}
		tool.Input = st.ToolInputs[tool.ID]
		tool.Output = presentation.Clip(tool.Output, maxToolText)
		tool.Error = presentation.Clip(tool.Error, maxToolText)
		c = protocol.Chunk{Tool: &tool}
		if tool.Phase == protocol.ToolRunning {
			t.startRunning(tool.ID, tool.Name)
		} else {
			t.stopRunning(tool.ID)
		}
	}
	if c.Text != "" {
		st.SawText = true
		t.text(c.Text, false)
		return
	}
	t.emit(c)
}

// text holds only incomplete block markers. The rest continues streaming until
// SplitText finds a boundary or reaches the overflow limit.
func (t *timeline) text(value string, final bool) {
	value = t.st.TextTail + value
	t.st.TextTail = ""
	for value != "" {
		s := &t.st.Segment
		l := t.cfg.Limits
		if t.cfg.SplitText == nil || l.MaxChars <= 0 || len(s.OpenTools) > 0 || t.st.Thought != nil {
			t.emit(protocol.Chunk{Text: value})
			return
		}
		body := segmentText(s.Chunks)
		if body == "" && len(s.Chunks) > 0 && s.Chars+s.CardChars >= l.MaxChars {
			prefix := s.Prefix
			t.finishSegment()
			t.st.Segment.Prefix = prefix
			continue
		}
		minimum := len([]rune(s.Prefix)) + len([]rune(body))
		other := s.Chars - len([]rune(body)) + s.CardChars
		target := l.MaxChars - other
		limit := max(target+max(l.MaxOverflow, 0), len([]rune(s.Prefix))+1)
		if s.Prefix != "" && body == "" && strings.TrimSpace(value) == "" && !strings.HasSuffix(s.Prefix, "\n") {
			if !final {
				t.st.TextTail = value
				return
			}
			s.Prefix = ""
		}
		plan := t.cfg.SplitText(markdown.PrefixText(body+value, s.Prefix), minimum, target, limit)
		r := []rune(value)
		if plan.Index > minimum || plan.Index == minimum && len(s.Chunks) > 0 {
			take := min(plan.Index-minimum, len(r))
			if take > 0 {
				t.emit(protocol.Chunk{Text: string(r[:take])})
			}
			t.st.Segment.Suffix = plan.Suffix
			t.finishSegment()
			t.st.Segment.Prefix = plan.Prefix
			value = string(r[take:])
			continue
		}
		if !final && plan.Hold >= minimum && plan.Hold > 0 {
			take := plan.Hold - minimum
			if take > 0 {
				t.emit(protocol.Chunk{Text: string(r[:take])})
			}
			t.st.TextTail = string(r[take:])
			return
		}
		t.emit(protocol.Chunk{Text: value})
		return
	}
}

func segmentText(chunks []protocol.Chunk) string {
	var text strings.Builder
	for _, c := range chunks {
		text.WriteString(c.Text)
	}
	return text.String()
}

func (t *timeline) closeThought() {
	th := t.st.Thought
	if th == nil {
		return
	}
	t.st.Thought = nil
	t.emit(protocol.Chunk{Thought: &protocol.ThoughtChunk{ID: th.ID, Text: th.Text, Done: true}})
}

func (t *timeline) record(c protocol.Chunk) {
	s := &t.st.Segment
	s.Chunks = appendMerged(s.Chunks, c)
	id := cardID(c)
	switch {
	case t.cfg.Size == nil:
		s.Chars += len([]rune(markdown.Render(c)))
	case id != "":
		if s.CardSizes == nil {
			s.CardSizes = map[string]int{}
		}
		size := t.cfg.Size(c)
		s.CardChars += size - s.CardSizes[id]
		s.CardSizes[id] = size
	default:
		s.Chars += t.cfg.Size(c)
	}
	if id != "" {
		if s.CardIDs == nil {
			s.CardIDs = map[string]bool{}
		}
		if !s.CardIDs[id] {
			s.CardIDs[id] = true
			s.Cards++
		}
	}
	if c.Tool != nil {
		s.OpenTools = removeTool(s.OpenTools, c.Tool.ID)
		if c.Tool.Phase == protocol.ToolRunning {
			s.OpenTools = append(s.OpenTools, c)
		}
	}
}

func (t *timeline) emit(c protocol.Chunk) {
	t.record(c)
	if !t.cfg.Streaming || t.st.NoStream {
		return
	}
	t.st.Segment.Pending = appendMerged(t.st.Segment.Pending, c)
}

// flush sends pending chunks. The first chunks open the message. If an append fails,
// the failed chunks continue in a new message. If that also fails, the rest is posted.
func (t *timeline) flush() {
	for attempt := range 2 {
		s := &t.st.Segment
		if len(s.Pending) == 0 || t.st.NoStream {
			return
		}
		pending := s.Pending
		s.Pending = nil
		wire := pending
		prefixSent := s.PrefixSent
		if s.Prefix != "" && !prefixSent && segmentText(pending) != "" {
			wire, _ = markdown.WrapChunks(pending, nil, s.Prefix, "")
			prefixSent = true
		}
		if s.Stream == "" {
			id, err := t.sink.Open(wire)
			if err != nil {
				t.sink.Warn("Open failed, posting instead", err)
				t.st.NoStream = true
				return
			}
			s.Stream = id
			s.StartedAt = t.sink.Now()
			s.PrefixSent = prefixSent
			s.Delivered = append([]protocol.Chunk(nil), s.Chunks...)
			return
		}
		if err := t.sink.Append(s.Stream, wire); err == nil {
			s.PrefixSent = prefixSent
			s.Delivered = append([]protocol.Chunk(nil), s.Chunks...)
			return
		} else {
			t.sink.Warn("Append failed", err)
		}

		resend := t.resendAfterFailure(pending)
		s.Chunks = s.Delivered
		var prefix string
		if t.cfg.SplitText != nil {
			value := markdown.PrefixText(segmentText(s.Chunks), s.Prefix)
			n := len([]rune(value))
			plan := t.cfg.SplitText(value, n, n, n)
			s.Suffix, prefix = plan.Suffix, plan.Prefix
		}
		t.closeSegment()
		t.st.Segment.Prefix = prefix
		if attempt == 1 {
			t.st.NoStream = true
		}
		for _, c := range resend {
			t.emit(c)
		}
	}
}

// resendAfterFailure returns the running cards not in failed, then failed, then the
// open thought block in full.
func (t *timeline) resendAfterFailure(failed []protocol.Chunk) []protocol.Chunk {
	mentioned := map[string]bool{}
	for _, c := range failed {
		if c.Tool != nil {
			mentioned[c.Tool.ID] = true
		}
	}
	var out []protocol.Chunk
	for _, c := range t.st.Segment.OpenTools {
		if !mentioned[c.Tool.ID] {
			out = append(out, c)
		}
	}
	th := t.st.Thought
	for _, c := range failed {
		if c.Thought != nil && th != nil && c.Thought.ID == th.ID {
			continue
		}
		out = append(out, c)
	}
	if th != nil {
		out = append(out, protocol.Chunk{Thought: &protocol.ThoughtChunk{ID: th.ID, Delta: th.Text, Text: th.Text}})
	}
	return out
}

// maybeRotate applies stream age and card count limits after open cards finish.
// SplitText chooses where to split text at the size limit.
func (t *timeline) maybeRotate() {
	s := &t.st.Segment
	if s.Stream == "" || len(s.OpenTools) > 0 || t.st.Thought != nil {
		return
	}
	l := t.cfg.Limits
	forced := (l.MaxAge > 0 && t.sink.Now().Sub(s.StartedAt) >= l.MaxAge) ||
		(l.MaxCards > 0 && s.Cards >= l.MaxCards)
	large := l.MaxChars > 0 && s.Chars+s.CardChars+len([]rune(s.Prefix)) >= l.MaxChars
	if !forced && !large {
		return
	}
	if t.cfg.SplitText == nil {
		t.finishSegment()
		return
	}
	body := segmentText(s.Chunks)
	if !forced && body != "" && s.Chars+s.CardChars+len([]rune(s.Prefix)) < l.MaxChars+max(l.MaxOverflow, 0) {
		return
	}
	value := markdown.PrefixText(body, s.Prefix)
	n := len([]rune(value))
	plan := t.cfg.SplitText(value, n, n, n)
	s.Suffix = plan.Suffix
	t.finishSegment()
	t.st.Segment.Prefix = plan.Prefix
}

func (t *timeline) finishSegment() {
	t.flush()
	t.closeSegment()
}

// closeSegment closes or posts the current message, then starts a new segment.
func (t *timeline) closeSegment() {
	s := t.st.Segment
	t.st.Segment = Segment{}

	m := Message{Final: t.final, Chunks: s.Chunks, Prefix: s.Prefix, Suffix: s.Suffix}
	for _, c := range s.Chunks {
		m.Text += c.Text
		for _, citation := range c.Citations {
			if citation.EndIndex >= 0 {
				citation.EndIndex = max(0, citation.EndIndex-t.st.TextOffset)
			}
			m.Citations = append(m.Citations, citation)
		}
	}
	t.st.TextOffset += len([]rune(m.Text))
	switch {
	case s.Stream != "":
		if err := t.sink.Finish(s.Stream, m); err != nil {
			t.sink.Warn("Finish failed", err)
		}
	case len(s.Chunks) > 0:
		if err := t.sink.Post(m); err != nil {
			t.sink.Warn("Post failed", err)
		}
	}
}

func (t *timeline) approvalRequested(req protocol.ApprovalRequest) {
	t.closeThought()
	t.finishSegment()
	t.setStatus(Status{Clear: true})
	input := presentation.Clip(req.ToolInputJSON, maxToolText)
	if t.st.ToolInputs == nil {
		t.st.ToolInputs = map[string]string{}
	}
	t.st.ToolInputs[req.ToolID] = input

	id, err := t.sink.Prompt(req)
	if err != nil {
		t.sink.Warn("Prompt failed", err)
		return
	}
	if t.st.Prompts == nil {
		t.st.Prompts = map[string]Prompt{}
	}
	t.st.Prompts[req.ToolID] = Prompt{ID: id, ToolName: req.ToolName, ToolInput: input}
}

func (t *timeline) approvalResolved(res protocol.ApprovalResolution) {
	p, posted := t.st.Prompts[res.ToolID]
	if posted {
		delete(t.st.Prompts, res.ToolID)
	}
	if res.ToolName == "" {
		res.ToolName = p.ToolName
	}
	if err := t.sink.Settle(p, res); err != nil {
		t.sink.Warn("Settle failed", err)
	}
	if posted && res.Approved {
		// The approved call runs next. Show it until its own events arrive.
		t.startRunning(res.ToolID, res.ToolName)
	}
}

func (t *timeline) startRunning(id, name string) {
	for _, r := range t.st.Running {
		if r.ID == id {
			return
		}
	}
	t.st.Running = append(t.st.Running, RunningTool{ID: id, Name: name})
}

func (t *timeline) stopRunning(id string) {
	var out []RunningTool
	for _, r := range t.st.Running {
		if r.ID != id {
			out = append(out, r)
		}
	}
	t.st.Running = out
}

// updateStatus shows the state at the end of a batch, after a turn started.
func (t *timeline) updateStatus() {
	if !t.st.Active {
		return
	}
	if len(t.st.Prompts) > 0 {
		t.setStatus(Status{Clear: true})
		return
	}
	s := Status{}
	for _, r := range t.st.Running {
		s.Running = append(s.Running, r.Name)
	}
	t.setStatus(s)
}

// setStatus sends a status change. An unchanged status sends nothing.
func (t *timeline) setStatus(s Status) {
	key := fmt.Sprint(s.Clear, s.Running)
	if !t.st.Active || key == t.st.LastStatus {
		return
	}
	t.st.LastStatus = key
	if err := t.sink.Status(s); err != nil {
		t.sink.Warn("Status failed", err)
	}
}

// appendMerged appends c. It merges c into the last chunk when both are text, or
// both are the same thought block.
func appendMerged(chunks []protocol.Chunk, c protocol.Chunk) []protocol.Chunk {
	if n := len(chunks); n > 0 {
		last := &chunks[n-1]
		switch {
		case c.Text != "" && last.Text != "":
			last.Text += c.Text
			return chunks
		case c.Thought != nil && last.Thought != nil && last.Thought.ID == c.Thought.ID:
			merged := *last.Thought
			merged.Delta += c.Thought.Delta
			merged.Text = c.Thought.Text
			merged.Done = c.Thought.Done
			last.Thought = &merged
			return chunks
		}
	}
	return append(chunks, c)
}

func removeTool(chunks []protocol.Chunk, id string) []protocol.Chunk {
	var out []protocol.Chunk
	for _, c := range chunks {
		if c.Tool.ID != id {
			out = append(out, c)
		}
	}
	return out
}

func cardID(c protocol.Chunk) string {
	switch {
	case c.Tool != nil:
		return c.Tool.ID
	case c.Thought != nil:
		return c.Thought.ID
	}
	return ""
}
