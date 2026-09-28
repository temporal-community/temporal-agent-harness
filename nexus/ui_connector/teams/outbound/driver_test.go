package teamsoutbound

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/teams"
)

// call is one activity call to the fake Python worker.
type call struct {
	name      string
	taskQueue string
	payload   map[string]any
}

// run delivers each batch, then calls Done, with fake Python worker activities.
func run(t *testing.T, addr teams.Address, pinnedQueue string, batches ...[]protocol.Event) []call {
	t.Helper()
	var calls []call
	env := (&testsuite.WorkflowTestSuite{}).NewTestWorkflowEnvironment()
	record := func(name string) func(context.Context, map[string]any) (map[string]any, error) {
		return func(ctx context.Context, p map[string]any) (map[string]any, error) {
			calls = append(calls, call{name: name, taskQueue: activity.GetInfo(ctx).TaskQueue, payload: p})
			if name == beginStreamActivity {
				return map[string]any{"ID": "s1", "SessionID": addr.SessionID, "TransportMode": "native", "TaskQueue": pinnedQueue}, nil
			}
			return nil, nil
		}
	}
	for _, name := range []string{beginStreamActivity, updateStreamActivity, finishStreamActivity, postMessageActivity, postApprovalPromptActivity, updateMessageActivity} {
		env.RegisterActivityWithOptions(record(name), activity.RegisterOptions{Name: name})
	}
	driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Minute})
	address, err := json.Marshal(addr)
	require.NoError(t, err)
	wf := func(ctx workflow.Context) error {
		var state json.RawMessage
		for _, b := range batches {
			if state, err = driver.Deliver(ctx, address, state, b); err != nil {
				return err
			}
		}
		return driver.Done(ctx, address, state)
	}
	env.RegisterWorkflow(wf)
	env.ExecuteWorkflow(wf)
	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	return calls
}

func names(calls []call) []string {
	out := make([]string, len(calls))
	for i, c := range calls {
		out[i] = c.name
	}
	return out
}

var personal = teams.Address{
	SessionID: "teams:conversation-1", SenderID: "u1", ThreadID: "m1",
	ConversationType: "personal", ServiceURL: "https://example.test/teams/", ChannelID: "msteams",
}

func tool(phase protocol.ToolPhase) protocol.Event {
	return protocol.Event{Chunk: &protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t1", Name: "search", Phase: phase}}}
}

// The Python worker reads these Go field names in the JSON payloads.
func TestStreamUsesThePythonContractAndThePinnedWorker(t *testing.T) {
	calls := run(t, personal, "teams-worker-1",
		[]protocol.Event{{TurnStarted: true}},
		[]protocol.Event{tool(protocol.ToolRunning), tool(protocol.ToolComplete), {Chunk: &protocol.Chunk{Text: "hello"}}},
	)
	assert.Equal(t, []string{beginStreamActivity, updateStreamActivity, finishStreamActivity}, names(calls))

	begin := calls[0].payload
	assert.Equal(t, "teams:conversation-1", begin["SessionID"])
	assert.Equal(t, "personal", begin["ConversationType"])
	assert.Equal(t, "https://example.test/teams/", begin["ServiceURL"])
	assert.Nil(t, begin["Chunks"], "begin_stream takes no content")

	update := calls[1].payload
	assert.Equal(t, "\n_🔧 search…_\n_✅ search_\nhello", update["Delta"])
	assert.Equal(t, "s1", update["Handle"].(map[string]any)["ID"])
	assert.Equal(t, "teams-worker-1", calls[1].taskQueue, "stream calls go to the worker that holds the stream")
	assert.Equal(t, "teams-worker-1", calls[2].taskQueue)
}

func TestSharedConversationPostsCompleteMessages(t *testing.T) {
	addr := personal
	addr.ConversationType = "channel"
	calls := run(t, addr, "",
		[]protocol.Event{{TurnStarted: true}},
		[]protocol.Event{
			{Chunk: &protocol.Chunk{Text: "hello world"}},
			{Chunk: &protocol.Chunk{Citations: []protocol.Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 5}}}},
		},
	)
	require.Equal(t, []string{postMessageActivity}, names(calls))
	assert.Equal(t, "hello [[1]](https://example.com/doc) world", calls[0].payload["Text"])
}

func TestApprovalPromptAndClickDecision(t *testing.T) {
	calls := run(t, personal, "", []protocol.Event{{ApprovalRequested: &protocol.ApprovalRequest{ToolID: "t1", ToolName: "deploy", ToolInputJSON: `{"env":"prod"}`}}})
	require.Equal(t, []string{postApprovalPromptActivity}, names(calls))
	assert.Equal(t, "deploy", calls[0].payload["ToolName"])
	assert.Equal(t, `{"env":"prod"}`, calls[0].payload["ToolInput"])

	click := teams.Address{SessionID: "teams:conversation-1", ActivityID: "card-1", ServiceURL: "https://example.test/teams/", ChannelID: "msteams"}
	for _, approved := range []bool{true, false} {
		calls = run(t, click, "", []protocol.Event{{ApprovalResolved: &protocol.ApprovalResolution{ToolID: "t1", ToolName: "deploy", Approved: approved}}})
		require.Equal(t, []string{updateMessageActivity}, names(calls))
		assert.Equal(t, "card-1", calls[0].payload["MessageID"])
		want := "🔐 Tool `deploy`: ❌ Denied"
		if approved {
			want = "🔐 Tool `deploy`: ✅ Approved"
		}
		assert.Equal(t, want, calls[0].payload["Text"])
	}

	calls = run(t, personal, "", []protocol.Event{{ApprovalResolved: &protocol.ApprovalResolution{ToolID: "t1", Approved: true}}})
	assert.Empty(t, calls, "without a clicked card there is nothing to update")
}

func TestSupportsStreaming(t *testing.T) {
	for conversationType, want := range map[string]bool{
		"personal": true, "channel": false, " GROUPCHAT ": false, "unknown": true, "": true,
	} {
		assert.Equal(t, want, SupportsStreaming(conversationType), conversationType)
	}
}
