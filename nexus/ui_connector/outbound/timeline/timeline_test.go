package timeline

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/markdown"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

// fakeSink records each platform call in order.
type fakeSink struct {
	calls      []string
	opens      [][]protocol.Chunk
	appends    [][]protocol.Chunk
	finishes   []Message
	posts      []Message
	settles    []Prompt
	openErr    error
	openErrs   []error // one per Open call, before openErr; nil entries succeed
	appendErrs []error // one per Append call; nil entries succeed
}

func next(errs *[]error) error {
	if len(*errs) == 0 {
		return nil
	}
	err := (*errs)[0]
	*errs = (*errs)[1:]
	return err
}

func (f *fakeSink) Now() time.Time         { return time.Unix(0, 0) }
func (f *fakeSink) Warn(string, error)     {}
func (f *fakeSink) record(call string)     { f.calls = append(f.calls, call) }
func (f *fakeSink) Error(msg string) error { f.record("Error:" + msg); return nil }

func (f *fakeSink) Open(chunks []protocol.Chunk) (string, error) {
	f.record("Open:" + markdown.RenderChunks(chunks))
	f.opens = append(f.opens, chunks)
	if err := next(&f.openErrs); err != nil {
		return "", err
	}
	if f.openErr != nil {
		return "", f.openErr
	}
	return fmt.Sprintf("stream-%d", len(f.opens)), nil
}

func (f *fakeSink) Append(_ string, chunks []protocol.Chunk) error {
	f.record("Append:" + markdown.RenderChunks(chunks))
	f.appends = append(f.appends, chunks)
	return next(&f.appendErrs)
}

func (f *fakeSink) Finish(_ string, m Message) error {
	f.record("Finish:" + m.Text)
	f.finishes = append(f.finishes, m)
	return nil
}

func (f *fakeSink) Post(m Message) error {
	f.record("Post:" + markdown.RenderChunks(m.Chunks))
	f.posts = append(f.posts, m)
	return nil
}

func (f *fakeSink) Status(s Status) error {
	switch {
	case s.Clear:
		f.record("Status:clear")
	case len(s.Running) == 0:
		f.record("Status:thinking")
	default:
		f.record(fmt.Sprintf("Status:running %v", s.Running))
	}
	return nil
}

func (f *fakeSink) Prompt(req protocol.ApprovalRequest) (string, error) {
	f.record("Prompt:" + req.ToolName)
	return "prompt-" + req.ToolID, nil
}

func (f *fakeSink) Settle(p Prompt, res protocol.ApprovalResolution) error {
	f.record(fmt.Sprintf("Settle:%s:%v", p.ID, res.Approved))
	f.settles = append(f.settles, p)
	return nil
}

func (f *fakeSink) Guide(h []protocol.Handler) error {
	f.record(fmt.Sprintf("Guide:%d", len(h)))
	return nil
}

func (f *fakeSink) Form(h protocol.Handler) error {
	f.record("Form:" + h.Name)
	return nil
}

var streaming = Config{Streaming: true}

// turn plays a turn as the router delivers it: a start batch, then each batch, then
// End.
func turn(cfg Config, sink Sink, batches ...[]protocol.Event) {
	var st State
	Apply(cfg, sink, &st, []protocol.Event{{TurnStarted: true}})
	for _, b := range batches {
		Apply(cfg, sink, &st, b)
	}
	End(cfg, sink, &st)
}

func text(s string) protocol.Event { return protocol.Event{Chunk: &protocol.Chunk{Text: s}} }

func thought(s string) protocol.Event {
	return protocol.Event{Chunk: &protocol.Chunk{Thought: &protocol.ThoughtChunk{Delta: s}}}
}

func tool(id, name string, phase protocol.ToolPhase, input string) protocol.Event {
	return protocol.Event{Chunk: &protocol.Chunk{Tool: &protocol.ToolChunk{ID: id, Name: name, Phase: phase, Input: input}}}
}

func ask(id, name, input string) protocol.Event {
	return protocol.Event{ApprovalRequested: &protocol.ApprovalRequest{ToolID: id, ToolName: name, ToolInputJSON: input}}
}

func decide(id, name string, approved bool) protocol.Event {
	return protocol.Event{ApprovalResolved: &protocol.ApprovalResolution{ToolID: id, ToolName: name, Approved: approved}}
}

func withoutStatus(calls []string) []string {
	var out []string
	for _, c := range calls {
		if !strings.HasPrefix(c, "Status:") {
			out = append(out, c)
		}
	}
	return out
}

func TestStreamsOneAppendPerBatch(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s, []protocol.Event{text("hello "), text("world")}, []protocol.Event{text("!")})
	assert.Equal(t, []string{
		"Status:thinking", "Open:hello world", "Append:!", "Finish:hello world!", "Status:clear",
	}, s.calls)
}

func TestEmptyTurnOpensNoMessage(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s)
	assert.Equal(t, []string{"Status:thinking", "Status:clear"}, s.calls)
}

func TestToolChunksCarryInputOnEveryPhase(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s,
		[]protocol.Event{tool("t1", "search", protocol.ToolRunning, `{"q":"x"}`)},
		[]protocol.Event{tool("t1", "search", protocol.ToolComplete, ""), text("done")},
	)
	require.Len(t, s.appends, 1)
	done := s.appends[0][0].Tool
	assert.Equal(t, protocol.ToolComplete, done.Phase)
	assert.Equal(t, `{"q":"x"}`, done.Input)
	assert.Equal(t, []string{
		"Status:thinking", "Open:\n_🔧 search…_\n", "Status:running [search]",
		"Append:_✅ search_\ndone", "Status:thinking", "Finish:done", "Status:clear",
	}, s.calls)
}

func TestThoughtBlockGetsIDAndEndsWhenTextArrives(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s, []protocol.Event{thought("Let me "), thought("think.")}, []protocol.Event{text("Answer.")})

	require.Len(t, s.opens, 1)
	require.Len(t, s.opens[0], 1, "deltas of one block merge within a batch")
	assert.Equal(t, &protocol.ThoughtChunk{ID: "thought-1", Delta: "Let me think.", Text: "Let me think."}, s.opens[0][0].Thought)

	require.Len(t, s.appends, 1)
	assert.Equal(t, &protocol.ThoughtChunk{ID: "thought-1", Text: "Let me think.", Done: true}, s.appends[0][0].Thought)
	assert.Equal(t, "Answer.", s.appends[0][1].Text)

	fin := s.finishes[0]
	assert.Equal(t, "Answer.", fin.Text, "Text is reply text only")
	require.Len(t, fin.Chunks, 2, "the finished message holds one merged thought chunk")
	assert.Equal(t, &protocol.ThoughtChunk{ID: "thought-1", Delta: "Let me think.", Text: "Let me think.", Done: true}, fin.Chunks[0].Thought)
}

func TestApprovalEndsMessageAndSettlesPrompt(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s,
		[]protocol.Event{text("Checking."), ask("t1", "delete", `{"p":1}`)},
		[]protocol.Event{{ApprovalResolved: &protocol.ApprovalResolution{ToolID: "t1", Approved: true, Remember: true}}},
		[]protocol.Event{tool("t1", "delete", protocol.ToolRunning, ""), tool("t1", "delete", protocol.ToolComplete, ""), text("Gone.")},
	)
	assert.Equal(t, []string{
		"Open:Checking.", "Finish:Checking.",
		"Prompt:delete",
		"Settle:prompt-t1:true",
		"Open:\n_🔧 delete…_\n_✅ delete_\nGone.", "Finish:Gone.",
	}, withoutStatus(s.calls))
	assert.Equal(t, []Prompt{{ID: "prompt-t1", ToolName: "delete", ToolInput: `{"p":1}`}}, s.settles)
	assert.Equal(t, `{"p":1}`, s.opens[1][0].Tool.Input, "the approved call's card carries the input")
}

func TestStatusClearsForPromptAndResumesOnApproval(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s, []protocol.Event{ask("t1", "delete", "")}, []protocol.Event{decide("t1", "delete", true)})
	assert.Equal(t, []string{
		"Status:thinking", "Status:clear", "Prompt:delete",
		"Settle:prompt-t1:true", "Status:running [delete]", "Status:clear",
	}, s.calls)
}

// A decision on a prompt that this timeline did not post reaches Settle with no ID:
// in a turn it belongs to another turn; alone it is the click's own decision.
func TestDecisionWithoutPostedPrompt(t *testing.T) {
	s := &fakeSink{}
	var st State
	Apply(streaming, s, &st, []protocol.Event{decide("other", "x", true)})
	End(streaming, s, &st)
	assert.Equal(t, []string{"Settle::true"}, s.calls)
	assert.Empty(t, st.Running)
}

func TestHandlerOutputShownOnlyWithoutText(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s, []protocol.Event{{HandlerOutput: &protocol.HandlerOutput{Value: map[string]any{"text": "Policy updated."}}}})
	assert.Equal(t, []string{"Open:Policy updated.", "Finish:Policy updated."}, withoutStatus(s.calls))

	s = &fakeSink{}
	turn(streaming, s, []protocol.Event{text("Streamed."), {HandlerOutput: &protocol.HandlerOutput{Value: map[string]any{"text": "Streamed."}}}})
	assert.Equal(t, []string{"Open:Streamed.", "Finish:Streamed."}, withoutStatus(s.calls))
}

func TestCitationsReachFinish(t *testing.T) {
	s := &fakeSink{}
	cite := protocol.Citation{URL: "https://example.com", Title: "Doc", EndIndex: 3}
	turn(streaming, s, []protocol.Event{text("hello"), {Chunk: &protocol.Chunk{Citations: []protocol.Citation{cite}}}})
	require.Len(t, s.finishes, 1)
	assert.Equal(t, []protocol.Citation{cite}, s.finishes[0].Citations)
	assert.Equal(t, "hello", s.finishes[0].Text)
}

func TestWithoutStreamingEachMessageIsPosted(t *testing.T) {
	s := &fakeSink{}
	turn(Config{}, s, []protocol.Event{text("A"), ask("t1", "x", "")}, []protocol.Event{text("B")})
	assert.Equal(t, []string{"Post:A", "Prompt:x", "Post:B"}, withoutStatus(s.calls))

	s = &fakeSink{}
	turn(Config{}, s, []protocol.Event{{Chunk: &protocol.Chunk{Citations: []protocol.Citation{{URL: "u", EndIndex: -1}}}}})
	require.Len(t, s.posts, 1)
	assert.Len(t, s.posts[0].Citations, 1)
}

func TestOpenFailurePostsInstead(t *testing.T) {
	s := &fakeSink{openErr: errors.New("no stream")}
	turn(streaming, s, []protocol.Event{text("hello "), text("world")})
	assert.Equal(t, []string{"Open:hello world", "Post:hello world"}, withoutStatus(s.calls))
}

func TestAppendFailureContinuesInNewMessageWithOpenCards(t *testing.T) {
	s := &fakeSink{appendErrs: []error{errors.New("expired")}}
	turn(streaming, s,
		[]protocol.Event{tool("t1", "slow", protocol.ToolRunning, ""), tool("t2", "other", protocol.ToolRunning, "")},
		[]protocol.Event{tool("t1", "slow", protocol.ToolComplete, ""), text("after")},
	)
	assert.Equal(t, []string{
		"Open:\n_🔧 slow…_\n\n_🔧 other…_\n",
		"Append:_✅ slow_\nafter", // fails
		"Finish:",                // the old message keeps only what reached it
		"Open:\n_🔧 other…_\n_✅ slow_\nafter",
		"Finish:after",
	}, withoutStatus(s.calls))
}

func TestRepeatedFailurePostsTheRest(t *testing.T) {
	s := &fakeSink{appendErrs: []error{errors.New("expired")}, openErrs: []error{nil, errors.New("unavailable")}}
	turn(streaming, s, []protocol.Event{text("before")}, []protocol.Event{text("after")}, []protocol.Event{text(" done")})
	assert.Equal(t, []string{
		"Open:before", "Append:after", "Finish:before",
		"Open:after", // fails: the rest is posted
		"Post:after done",
	}, withoutStatus(s.calls))
	require.Len(t, s.posts, 1)
	assert.Equal(t, "after done", s.posts[0].Text)
}

func TestLaterFailureKeepsRetriedContent(t *testing.T) {
	s := &fakeSink{appendErrs: []error{errors.New("expired"), errors.New("expired")}}
	turn(streaming, s, []protocol.Event{text("first")}, []protocol.Event{text("second")}, []protocol.Event{text("third")})
	assert.Equal(t, []string{
		"Open:first", "Append:second", "Finish:first",
		"Open:second", "Append:third", "Finish:second",
		"Open:third", "Finish:third",
	}, withoutStatus(s.calls))
}

func TestRotatesAtCardLimitButNotWhileAToolRuns(t *testing.T) {
	s := &fakeSink{}
	turn(Config{Streaming: true, Limits: Limits{MaxCards: 2}}, s,
		[]protocol.Event{tool("a", "a", protocol.ToolRunning, ""), tool("a", "a", protocol.ToolComplete, ""), tool("b", "b", protocol.ToolRunning, "")},
		[]protocol.Event{tool("b", "b", protocol.ToolComplete, "")},
		[]protocol.Event{text("next")},
	)
	assert.Equal(t, []string{
		"Open:\n_🔧 a…_\n_✅ a_\n\n_🔧 b…_\n", // limit reached, but b is running
		"Append:_✅ b_\n", "Finish:", // b done: rotate
		"Open:next", "Finish:next",
	}, withoutStatus(s.calls))
}

func TestSizeReplacesCardSizesAndDrivesMaxChars(t *testing.T) {
	s := &fakeSink{}
	size := func(c protocol.Chunk) int {
		if c.Tool != nil {
			return 100 + len(c.Tool.Output)
		}
		return len(c.Text)
	}
	big := protocol.Event{Chunk: &protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t1", Name: "a", Phase: protocol.ToolComplete, Output: strings.Repeat("x", 150)}}}
	turn(Config{Streaming: true, Limits: Limits{MaxChars: 300}, Size: size}, s,
		// t1 updates three times; only its latest size (100) counts.
		[]protocol.Event{tool("t1", "a", protocol.ToolRunning, ""), tool("t1", "a", protocol.ToolRunning, ""), tool("t1", "a", protocol.ToolRunning, "")},
		[]protocol.Event{text("hi")},
		// t1 completes at 250; with "hi" the message is 252: under 300.
		[]protocol.Event{big},
		// t2 adds 100: 352 >= 300, so the message ends after this batch.
		[]protocol.Event{tool("t2", "b", protocol.ToolComplete, "")},
		[]protocol.Event{text("next")},
	)
	require.Len(t, s.finishes, 2)
	last := s.finishes[0].Chunks[len(s.finishes[0].Chunks)-1]
	assert.Equal(t, "t2", last.Tool.ID, "the split comes after t2")
	assert.Equal(t, "next", s.finishes[1].Text)
}

func TestGuideFormAndErrorAreOwnMessages(t *testing.T) {
	s := &fakeSink{}
	var st State
	Apply(streaming, s, &st, []protocol.Event{
		{Guide: &protocol.Guide{Handlers: []protocol.Handler{{Name: "ask"}, {Name: "set_model"}}}},
		{TurnStarted: true},
	})
	Apply(streaming, s, &st, []protocol.Event{text("hi"), {Form: &protocol.Handler{Name: "set_model"}}, {Error: "boom"}})
	End(streaming, s, &st)
	assert.Equal(t, []string{"Guide:2", "Open:hi", "Finish:hi", "Form:set_model", "Error:boom"}, withoutStatus(s.calls))
}

func TestNoStatusWithoutATurn(t *testing.T) {
	s := &fakeSink{}
	var st State
	Apply(streaming, s, &st, []protocol.Event{text("Unknown handler.")})
	End(streaming, s, &st)
	assert.Equal(t, []string{"Open:Unknown handler.", "Finish:Unknown handler."}, s.calls)
}

func TestClipsToolText(t *testing.T) {
	s := &fakeSink{}
	big := strings.Repeat("x", maxToolText+100)
	turn(streaming, s, []protocol.Event{{Chunk: &protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t1", Name: "dump", Phase: protocol.ToolComplete, Input: big, Output: big}}}})
	got := s.opens[0][0].Tool
	assert.Len(t, []rune(got.Input), maxToolText)
	assert.Len(t, []rune(got.Output), maxToolText)
}

// A batch sends its cards first, then one status with the tools still running.
func TestOneStatusPerBatchAfterTheCards(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s,
		[]protocol.Event{
			tool("a", "search", protocol.ToolRunning, ""), tool("b", "read", protocol.ToolRunning, ""),
			tool("c", "read", protocol.ToolRunning, ""), tool("a", "search", protocol.ToolComplete, ""),
		},
		[]protocol.Event{tool("b", "read", protocol.ToolComplete, "")},
		[]protocol.Event{tool("c", "read", protocol.ToolComplete, "")},
		[]protocol.Event{tool("d", "fetch", protocol.ToolRunning, "")},
		[]protocol.Event{tool("d", "fetch", protocol.ToolRunning, "")}, // unchanged: no call
		[]protocol.Event{tool("d", "fetch", protocol.ToolComplete, "")},
	)
	var status []string
	for _, c := range s.calls {
		if strings.HasPrefix(c, "Status:") {
			status = append(status, c)
		}
	}
	assert.Equal(t, []string{
		"Status:thinking",
		"Status:running [read read]",
		"Status:running [read]",
		"Status:thinking",
		"Status:running [fetch]",
		"Status:thinking",
		"Status:clear",
	}, status)
	assert.True(t, strings.HasPrefix(s.calls[1], "Open:"), "the cards go out before the batch's status")
}

func TestStateSurvivesJSON(t *testing.T) {
	s := &fakeSink{}
	var st State
	Apply(streaming, s, &st, []protocol.Event{{TurnStarted: true}, tool("t1", "x", protocol.ToolRunning, `{"a":1}`), ask("t2", "y", "")})
	b, err := json.Marshal(st)
	require.NoError(t, err)
	var restored State
	require.NoError(t, json.Unmarshal(b, &restored))
	assert.Equal(t, st, restored)
}

func TestStructuredOutputAndArgumentErrorsAreRenderedOutbound(t *testing.T) {
	s := &fakeSink{}
	turn(streaming, s, []protocol.Event{{HandlerOutput: &protocol.HandlerOutput{Value: map[string]any{"count": 3}}}})
	assert.Equal(t, "```json\n{\n  \"count\": 3\n}\n```", s.finishes[0].Text)
	s = &fakeSink{}
	h := protocol.Handler{Name: "set_model", Fields: []protocol.Field{{Name: "model", Kind: protocol.KindEnum, Options: []string{"a", "b"}, Required: true}}}
	var st State
	Apply(streaming, s, &st, []protocol.Event{{HandlerHelp: &protocol.HandlerHelp{Handler: h, Errors: map[string]string{"model": "Required."}}}})
	End(streaming, s, &st)
	assert.Contains(t, s.opens[0][0].Text, "• model: Required.")
	assert.Contains(t, s.opens[0][0].Text, "`/set_model model=<a|b>`")
}
