package slackoutbound

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	slackapi "github.com/slack-go/slack"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.temporal.io/sdk/testsuite"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/timeline"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/interaction"
)

// fakeSlack records Slack Web API calls. fail maps an API path to the Slack error it
// returns; failOnce applies to the first call only.
type fakeSlack struct {
	srv      *httptest.Server
	requests []slackRequest
	fail     map[string]string
	failOnce map[string]string
}

type slackRequest struct {
	path string
	form url.Values
	body string
}

func newFakeSlack(t *testing.T) *fakeSlack {
	f := &fakeSlack{fail: map[string]string{}, failOnce: map[string]string{}}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, r.ParseForm())
		f.requests = append(f.requests, slackRequest{path: r.URL.Path, form: r.Form})
		w.Header().Set("Content-Type", "application/json")
		if msg, ok := f.failOnce[r.URL.Path]; ok {
			delete(f.failOnce, r.URL.Path)
			_ = json.NewEncoder(w).Encode(map[string]any{"ok": false, "error": msg})
			return
		}
		if msg, ok := f.fail[r.URL.Path]; ok {
			_ = json.NewEncoder(w).Encode(map[string]any{"ok": false, "error": msg})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "channel": "C12345", "ts": "1721609600.123456"})
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeSlack) platform() *SlackPlatform {
	return NewSlackPlatform(slackapi.New("test-token", slackapi.OptionAPIURL(f.srv.URL+"/")), "T12345")
}

var addr = slack.Address{SessionID: "slack:C12345:1.0", Channel: "C12345", ThreadTS: "1.0", UserID: "U1", TriggerID: "trig"}

func (f *fakeSlack) sink() *sink {
	return &sink{ctx: context.Background(), p: f.platform(), addr: addr}
}

func (f *fakeSlack) paths() []string {
	out := make([]string, len(f.requests))
	for i, r := range f.requests {
		out[i] = r.path
	}
	return out
}

// chunksOf decodes the "chunks" form field of a streaming call.
func chunksOf(t *testing.T, r slackRequest) []map[string]any {
	t.Helper()
	var chunks []map[string]any
	require.NoError(t, json.Unmarshal([]byte(r.form.Get("chunks")), &chunks))
	return chunks
}

const ts = "1721609600.123456"

func toolChunk(phase protocol.ToolPhase) protocol.Chunk {
	return protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t1", Name: "search", Phase: phase, Input: `{"q":"x"}`, Output: "3 hits", Error: "boom"}}
}

func TestStreamChunks_CardsAndText(t *testing.T) {
	chunks := []protocol.Chunk{
		{Text: "a"}, {Text: "b"},
		toolChunk(protocol.ToolRunning),
		{Thought: &protocol.ThoughtChunk{ID: "thought-1", Delta: "hm", Text: "hm"}},
		{Citations: []protocol.Citation{{URL: "u"}}},
		{Text: "c"},
	}

	withCards := streamChunks(chunks, true)
	require.Len(t, withCards, 4)
	assert.Equal(t, slackapi.NewMarkdownTextChunk("ab"), withCards[0])
	tool := withCards[1].(slackapi.TaskUpdateChunk)
	assert.Equal(t, "t1", tool.ID)
	assert.Equal(t, slackapi.TaskCardStatusInProgress, tool.Status)
	assert.Equal(t, "{\n  \"q\": \"x\"\n}", tool.Details)
	thought := withCards[2].(slackapi.TaskUpdateChunk)
	assert.Equal(t, "Thinking", thought.Title)
	assert.Equal(t, slackapi.NewMarkdownTextChunk("c"), withCards[3])

	textOnly := streamChunks(chunks, false)
	require.Len(t, textOnly, 1)
	assert.Equal(t, slackapi.NewMarkdownTextChunk("ab\n_🔧 search…_\n\n> 💭 hmc"), textOnly[0])
}

func TestTaskCard_Phases(t *testing.T) {
	done, _ := taskCard(toolChunk(protocol.ToolComplete))
	assert.Equal(t, slackapi.TaskCardStatusComplete, done.Status)
	assert.Empty(t, done.Output, "a successful result is not shown")

	failed, _ := taskCard(toolChunk(protocol.ToolErrored))
	assert.Equal(t, slackapi.TaskCardStatusError, failed.Status)
	assert.Equal(t, "boom", failed.Output)

	thought, _ := taskCard(protocol.Chunk{Thought: &protocol.ThoughtChunk{ID: "th", Text: strings.Repeat("x", 900), Done: true}})
	assert.Equal(t, slackapi.TaskCardStatusComplete, thought.Status)
	assert.Len(t, []rune(thought.Details), cardDetailsMax)
	assert.True(t, strings.HasPrefix(thought.Details, "…"), "a long thought keeps its latest text")

	_, ok := taskCard(protocol.Chunk{Text: "x"})
	assert.False(t, ok)
}

func TestChunkSize(t *testing.T) {
	long := strings.Repeat("o", 2000)
	assert.Equal(t, 5, chunkSize(protocol.Chunk{Text: "hello"}))
	assert.Equal(t, cardOverhead+4+7, chunkSize(protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t", Name: "read", Phase: protocol.ToolComplete, Input: `{"a":1}`, Output: long}}))
	assert.Equal(t, cardOverhead+4+cardErrorMax, chunkSize(protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t", Name: "read", Phase: protocol.ToolErrored, Error: long}}))
	assert.Equal(t, cardOverhead+cardDetailsMax, chunkSize(protocol.Chunk{Thought: &protocol.ThoughtChunk{ID: "th", Text: long}}))
}

// The size limit must split messages with many tool cards.
func TestDefaultLimitsSplitBetweenTheObservedCases(t *testing.T) {
	cards := func(n int) int {
		total := 0
		for i := range n {
			total += chunkSize(protocol.Chunk{Tool: &protocol.ToolChunk{
				ID: fmt.Sprint(i), Name: "pylon_search_issues", Phase: protocol.ToolComplete, Input: strings.Repeat("i", 75),
			}})
		}
		return total
	}
	assert.Less(t, cards(12), DefaultLimits.MaxChars)
	assert.GreaterOrEqual(t, cards(18), DefaultLimits.MaxChars)
}

func TestSink_StreamLifecycle(t *testing.T) {
	f := newFakeSlack(t)
	s := f.sink()
	s.p.TaskDisplayMode = slackapi.TaskDisplayModeTimeline

	stream, err := s.Open([]protocol.Chunk{toolChunk(protocol.ToolRunning), {Text: "hi"}})
	require.NoError(t, err)
	assert.Equal(t, ts, stream)
	require.NoError(t, s.Append(stream, []protocol.Chunk{{Text: "more"}}))
	require.NoError(t, s.Finish(stream, timeline.Message{Text: "hi", Chunks: []protocol.Chunk{{Text: "hi"}}}))

	assert.Equal(t, []string{"/chat.startStream", "/chat.appendStream", "/chat.stopStream"}, f.paths())
	start := f.requests[0].form
	assert.Equal(t, "C12345", start.Get("channel"))
	assert.Equal(t, "1.0", start.Get("thread_ts"))
	assert.Equal(t, "U1", start.Get("recipient_user_id"))
	assert.Equal(t, "T12345", start.Get("recipient_team_id"))
	assert.Equal(t, "timeline", start.Get("task_display_mode"))
	first := chunksOf(t, f.requests[0])
	require.Len(t, first, 2, "the message opens with its first content")
	assert.Equal(t, "task_update", first[0]["type"])
	assert.Equal(t, map[string]any{"type": "markdown_text", "text": "hi"}, first[1])
	assert.Empty(t, f.requests[2].form.Get("markdown_text"), "no citations: nothing to add")
}

func TestSink_CardsFallBackToText(t *testing.T) {
	f := newFakeSlack(t)
	f.failOnce["/chat.startStream"] = "invalid_chunks"
	s := f.sink()
	stream, err := s.Open([]protocol.Chunk{toolChunk(protocol.ToolRunning)})
	require.NoError(t, err)
	assert.Equal(t, ts, stream)
	assert.Equal(t, "\n_🔧 search…_\n", chunksOf(t, f.requests[1])[0]["text"])
	assert.True(t, s.p.cardsUnsupported.Load(), "later calls send text")

	f = newFakeSlack(t)
	f.failOnce["/chat.appendStream"] = "invalid_chunks"
	s = f.sink()
	require.NoError(t, s.Append(ts, []protocol.Chunk{toolChunk(protocol.ToolRunning)}))
	assert.Equal(t, "markdown_text", chunksOf(t, f.requests[1])[0]["type"])

	f = newFakeSlack(t)
	f.fail["/chat.appendStream"] = "message_not_in_streaming_state"
	assert.ErrorContains(t, f.sink().Append(ts, []protocol.Chunk{toolChunk(protocol.ToolRunning)}), "message_not_in_streaming_state")
}

func TestSink_FinishWithCardsListsSourcesAsAChunk(t *testing.T) {
	f := newFakeSlack(t)
	cite := protocol.Citation{URL: "https://a", Title: "A", EndIndex: 5}
	require.NoError(t, f.sink().Finish(ts, timeline.Message{
		Text:      "hello",
		Chunks:    []protocol.Chunk{toolChunk(protocol.ToolComplete), {Text: "hello"}},
		Citations: []protocol.Citation{cite, cite},
	}))
	assert.Equal(t, []string{"/chat.stopStream"}, f.paths(), "chat.update would replace the cards")
	stop := f.requests[0]
	assert.Empty(t, stop.form.Get("markdown_text"), "the stream used chunks, so the stop must too")
	assert.Equal(t, []map[string]any{{"type": "markdown_text", "text": "\n\n*Sources*\n1. [A](https://a)"}}, chunksOf(t, stop))
}

func TestSink_FinishTextOnlySplicesCitationsInline(t *testing.T) {
	f := newFakeSlack(t)
	require.NoError(t, f.sink().Finish(ts, timeline.Message{
		Text:      "hello world",
		Chunks:    []protocol.Chunk{{Text: "hello world"}},
		Citations: []protocol.Citation{{URL: "https://a", Title: "A", EndIndex: 5}},
	}))
	assert.Equal(t, []string{"/chat.stopStream", "/chat.update"}, f.paths())
	assert.Contains(t, f.requests[1].form.Get("markdown_text"), "(https://a)")
}

func TestSink_CitationsStayOutsideInlineCode(t *testing.T) {
	text := "A customer saw `WorkflowTaskScheduled/Started/Completed` events."
	message := timeline.Message{
		Text:      text,
		Chunks:    []protocol.Chunk{{Text: "A customer saw `WorkflowTaskScheduled/"}, {Text: "Started/Completed` events."}},
		Citations: []protocol.Citation{{URL: "https://a", EndIndex: len("A customer saw `WorkflowTaskScheduled/Started")}},
	}
	t.Run("streamed reply", func(t *testing.T) {
		f := newFakeSlack(t)
		require.NoError(t, f.sink().Finish(ts, message))
		assert.Equal(t, "A customer saw `WorkflowTaskScheduled/Started/Completed` [[1]](https://a) events.", f.requests[1].form.Get("markdown_text"))
	})
	t.Run("posted reply", func(t *testing.T) {
		f := newFakeSlack(t)
		require.NoError(t, f.sink().Post(message))
		assert.Equal(t, "A customer saw `WorkflowTaskScheduled/Started/Completed` <https://a|[1]> events.", f.requests[0].form.Get("text"))
	})
}

func TestSink_PostUsesMrkdwnLinks(t *testing.T) {
	f := newFakeSlack(t)
	require.NoError(t, f.sink().Post(timeline.Message{
		Chunks:    []protocol.Chunk{toolChunk(protocol.ToolComplete), {Text: "hello"}},
		Citations: []protocol.Citation{{URL: "https://a", Title: "A", EndIndex: 5}},
	}))
	text := f.requests[0].form.Get("text")
	assert.True(t, strings.HasPrefix(text, "_✅ search_\nhello"))
	assert.Contains(t, text, "<https://a|[1]>")
	assert.Equal(t, "1.0", f.requests[0].form.Get("thread_ts"))
}

func TestStatusText(t *testing.T) {
	text, messages := statusText(timeline.Status{Clear: true})
	assert.Equal(t, "", text)
	assert.Nil(t, messages)

	text, messages = statusText(timeline.Status{})
	assert.Equal(t, "Thinking…", text)
	assert.Equal(t, []string{"Thinking…"}, messages)

	text, messages = statusText(timeline.Status{Running: []string{"search"}})
	assert.Equal(t, "Running search…", text)
	assert.Equal(t, []string{"Running search…"}, messages)

	text, messages = statusText(timeline.Status{Running: []string{"read", "search", "read"}})
	assert.Equal(t, "Running 3 tools…", text)
	assert.Equal(t, []string{"Running read…", "Running search…"}, messages)
}

func TestSink_Status(t *testing.T) {
	f := newFakeSlack(t)
	s := f.sink()
	require.NoError(t, s.Status(timeline.Status{Running: []string{"search", "read"}}))
	require.NoError(t, s.Status(timeline.Status{Clear: true}))
	assert.Equal(t, "Running 2 tools…", f.requests[0].form.Get("status"))
	assert.Equal(t, "Running search…,Running read…", f.requests[0].form.Get("loading_messages"))
	assert.Equal(t, "", f.requests[1].form.Get("status"))

	f = newFakeSlack(t)
	f.fail["/assistant.threads.setStatus"] = "missing_scope"
	s = f.sink()
	assert.Error(t, s.Status(timeline.Status{}))
	assert.NoError(t, s.Status(timeline.Status{}), "status is off for this worker after a failure")
	assert.Len(t, f.requests, 1)
}

func TestSink_PromptCarriesTheDecisionInItsButtons(t *testing.T) {
	f := newFakeSlack(t)
	id, err := f.sink().Prompt(protocol.ApprovalRequest{ToolID: "t1", ToolName: "delete_file", ToolInputJSON: `{"path":"/tmp/x"}`})
	require.NoError(t, err)
	assert.Equal(t, ts, id)
	blocks := f.requests[0].form.Get("blocks")
	assert.Contains(t, blocks, "Run `delete_file`?")
	assert.Contains(t, blocks, `\"path\": \"/tmp/x\"`)

	actions := approvalPromptBlocks("s1", protocol.ApprovalRequest{ToolID: "t1", ToolName: "x"})[2].(*slackapi.ActionBlock).Elements.ElementSet
	want := []interaction.ApprovalButtonValue{
		{SessionID: "s1", ToolID: "t1", ToolName: "x", Approved: true},
		{SessionID: "s1", ToolID: "t1", ToolName: "x", Approved: true, Remember: true},
		{SessionID: "s1", ToolID: "t1", ToolName: "x"},
	}
	for i, el := range actions {
		var got interaction.ApprovalButtonValue
		require.NoError(t, json.Unmarshal([]byte(el.(*slackapi.ButtonBlockElement).Value), &got))
		assert.Equal(t, want[i], got)
	}
}

func TestSink_Settle(t *testing.T) {
	prompt := timeline.Prompt{ID: "9.9", ToolName: "delete_file"}
	res := func(approved, remember bool) protocol.ApprovalResolution {
		return protocol.ApprovalResolution{ToolName: "delete_file", Approved: approved, Remember: remember, Reason: "by <@U1>"}
	}

	f := newFakeSlack(t)
	require.NoError(t, f.sink().Settle(prompt, res(true, false)))
	assert.Equal(t, []string{"/chat.delete"}, f.paths())

	f = newFakeSlack(t)
	f.fail["/chat.delete"] = "cant_delete_message"
	require.NoError(t, f.sink().Settle(prompt, res(true, false)))
	assert.Equal(t, []string{"/chat.delete", "/chat.update"}, f.paths())

	f = newFakeSlack(t)
	require.NoError(t, f.sink().Settle(prompt, res(false, false)))
	assert.Equal(t, []string{"/chat.update"}, f.paths())
	blocks := f.requests[0].form.Get("blocks")
	assert.Contains(t, blocks, "*Denied* · `delete_file`")
	assert.NotContains(t, blocks, interaction.ActionApprove, "a settled prompt has no buttons")

	f = newFakeSlack(t)
	require.NoError(t, f.sink().Settle(prompt, res(true, true)))
	assert.Equal(t, []string{"/chat.delete", "/chat.postMessage"}, f.paths())
	assert.Equal(t, "_`delete_file` is allowed for the rest of this session — by <@U1>_", f.requests[1].form.Get("text"))

	f = newFakeSlack(t)
	require.NoError(t, f.sink().Settle(timeline.Prompt{}, res(true, true)))
	assert.Empty(t, f.requests, "another turn's prompt is settled by that turn")
}

func TestSink_GuideFormAndError(t *testing.T) {
	f := newFakeSlack(t)
	s := f.sink()
	h := protocol.Handler{Name: "set_model", Fields: []protocol.Field{{Name: "model", Label: "Model", Kind: protocol.KindEnum, Options: []string{"a", "b"}, Required: true}}}
	require.NoError(t, s.Guide([]protocol.Handler{h}))
	require.NoError(t, s.Form(h))
	require.NoError(t, s.Error("boom"))
	assert.Equal(t, []string{"/chat.postMessage", "/views.open", "/chat.postMessage"}, f.paths())
	assert.Contains(t, f.requests[0].form.Get("blocks"), interaction.ActionOpenFormPrefix+"set_model")
	assert.Equal(t, "_Something went wrong handling that: boom_", f.requests[2].form.Get("text"))
}

// Deliver runs the timeline in one activity and returns state for the next call.
func TestDeliverActivity(t *testing.T) {
	f := newFakeSlack(t)
	p := f.platform()
	env := (&testsuite.WorkflowTestSuite{}).NewTestActivityEnvironment()
	env.RegisterActivity(p.Deliver)
	address, _ := json.Marshal(addr)

	val, err := env.ExecuteActivity(p.Deliver, DeliverInput{Address: address, Events: []protocol.Event{
		{TurnStarted: true}, {Chunk: &protocol.Chunk{Text: "hi"}},
	}})
	require.NoError(t, err)
	var state json.RawMessage
	require.NoError(t, val.Get(&state))
	assert.Equal(t, []string{"/chat.startStream", "/assistant.threads.setStatus"}, f.paths())

	_, err = env.ExecuteActivity(p.Deliver, DeliverInput{Address: address, State: state, End: true})
	require.NoError(t, err)
	assert.Equal(t, []string{"/chat.startStream", "/assistant.threads.setStatus", "/chat.stopStream", "/assistant.threads.setStatus"}, f.paths())
}

func TestGuideBlocks(t *testing.T) {
	handlers := []protocol.Handler{
		{Name: "ask", Description: "Chat."},
		{Name: "set_model", Description: "Set the model.", MidTurn: "accept",
			Fields: []protocol.Field{{Name: "model", Label: "Model", Kind: protocol.KindEnum, Options: []string{"a", "b"}, Required: true}}},
	}
	blocks := guideBlocks("slack:C1:1.0", handlers)
	require.Len(t, blocks, 5)
	assert.Contains(t, blocks[3].(*slackapi.SectionBlock).Text.Text, "*set_model* — Set the model. · usable mid-turn")
	assert.Contains(t, blocks[3].(*slackapi.SectionBlock).Text.Text, "`a` | `b`")

	b := blocks[4].(*slackapi.ActionBlock).Elements.ElementSet[1].(*slackapi.ButtonBlockElement)
	assert.Equal(t, interaction.ActionOpenFormPrefix+"set_model", b.ActionID)
	assert.JSONEq(t, `{"s":"slack:C1:1.0","h":"set_model","i":true,"f":[{"n":"model","k":"enum","l":"Model","r":true,"o":["a","b"]}]}`, b.Value)

	var many []protocol.Handler
	for i := range 60 {
		many = append(many, protocol.Handler{Name: fmt.Sprintf("h%d", i)})
	}
	blocks = guideBlocks("s", many)
	assert.LessOrEqual(t, len(blocks), interaction.MaxBlocks)
	buttons := 0
	for _, blk := range blocks {
		if a, ok := blk.(*slackapi.ActionBlock); ok {
			buttons += len(a.Elements.ElementSet)
		}
	}
	assert.Equal(t, 60, buttons, "every handler keeps its button")
}

func TestPostPrompt_UnknownType(t *testing.T) {
	_, err := NewSlackPlatform(nil, "").PostPrompt(context.Background(), PostPromptInput{
		Channel: "C12345", PromptID: "p1", Text: "choose one", Type: "unknown",
	})
	assert.ErrorContains(t, err, "unknown prompt type")
}

func TestDeliverSplitsAtTheNextParagraph(t *testing.T) {
	f := newFakeSlack(t)
	p := f.platform()
	p.Limits = timeline.Limits{MaxChars: 40, MaxOverflow: 200}
	address, err := json.Marshal(addr)
	require.NoError(t, err)
	input := DeliverInput{Address: address, Events: []protocol.Event{{Chunk: &protocol.Chunk{Text: "Current public documentation describes **Current and R"}}}}
	input.State, err = p.Deliver(context.Background(), input)
	require.NoError(t, err)
	assert.Equal(t, []string{"/chat.startStream"}, f.paths())
	input.Events = []protocol.Event{{Chunk: &protocol.Chunk{Text: "amping Versions**, with Pinned Workflows on their assigned version.\n\nNext paragraph."}}}
	input.State, err = p.Deliver(context.Background(), input)
	require.NoError(t, err)
	assert.Equal(t, []string{"/chat.startStream", "/chat.appendStream", "/chat.stopStream", "/chat.startStream"}, f.paths())
	assert.Equal(t, "Next paragraph.", chunksOf(t, f.requests[3])[0]["text"])
	input.Events, input.End = nil, true
	_, err = p.Deliver(context.Background(), input)
	require.NoError(t, err)
}

func TestFinishClosesFormattingBeforeSources(t *testing.T) {
	f := newFakeSlack(t)
	require.NoError(t, f.sink().Finish(ts, timeline.Message{
		Chunks:    []protocol.Chunk{toolChunk(protocol.ToolComplete), {Text: "**words"}},
		Suffix:    "**",
		Citations: []protocol.Citation{{URL: "https://a", Title: "A", EndIndex: 7}},
	}))
	assert.Equal(t, []string{"/chat.stopStream"}, f.paths())
	assert.Equal(t, "**\n\n*Sources*\n1. [A](https://a)", chunksOf(t, f.requests[0])[0]["text"])
}

func TestFinishRepairsFormattingInCitationUpdate(t *testing.T) {
	f := newFakeSlack(t)
	require.NoError(t, f.sink().Finish(ts, timeline.Message{
		Chunks: []protocol.Chunk{{Text: " words"}}, Prefix: "**", Suffix: "**",
		Citations: []protocol.Citation{{URL: "https://a", EndIndex: 6}},
	}))
	assert.Equal(t, "**", chunksOf(t, f.requests[0])[0]["text"])
	assert.Equal(t, " **words [[1]](https://a)**", f.requests[1].form.Get("markdown_text"))
}
