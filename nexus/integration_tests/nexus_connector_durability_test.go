//go:build integration

package nexusinteg

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/nexus-rpc/sdk-go/nexus"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent"
	harnessgen "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent/generated"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/timeline"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
	sdkworker "go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
)

// ptr returns a pointer to v, for populating nex-gen's pointer-typed optional fields.
func ptr[T any](v T) *T { return &v }

// derefOrZero returns *p, or the zero value of T if p is nil, for reading
// nex-gen's pointer-typed optional fields.
func derefOrZero[T any](p *T) T {
	if p == nil {
		var zero T
		return zero
	}
	return *p
}

// -- Tests --------------------------------------------------------------------

func TestConnector_TwoMessagesDeliveredAcrossWorkerRestart(t *testing.T) {
	// Verify that after worker0 finishes message-1 and dies, worker1 can still
	// process message-2 for the same session. Each message is an independent
	// single-turn workflow.
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	agentTaskQueue := TaskQueue(t, "agent-")
	CreateNexusEndpoint(t, temporalClient, agent.AgentNexusEndpoint, agentTaskQueue)
	startMockAgentWorker(t, temporalClient, agentTaskQueue, connectorTurnItems(t, 4))

	connectorTaskQueue := TaskQueue(t, "connector-")
	platform := newmockMsgPlatform()
	worker0 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	worker1 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	defer worker1.Stop()

	msg1 := testMessage{ID: "m1", Text: "hello"}
	require.NoError(t, startMessageConnector(t, temporalClient, connectorTaskQueue, "integ", "slack:C001", msg1))
	platform.waitCompletions(t, 1, 30*time.Second)

	worker0.Stop()

	msg2 := testMessage{ID: "m2", Text: "world"}
	require.NoError(t, startMessageConnector(t, temporalClient, connectorTaskQueue, "integ", "slack:C001", msg2))
	platform.waitCompletions(t, 1, 30*time.Second)

	require.Equal(t, 2, platform.starts, "Stream(start) must be called once per message")
	require.Equal(t, 2, platform.stops, "Stream(stop) must be called once per message")
}

func TestConnector_MultipleSessionsSurviveWorkerDeath(t *testing.T) {
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	agentTaskQueue := TaskQueue(t, "agent-")
	CreateNexusEndpoint(t, temporalClient, agent.AgentNexusEndpoint, agentTaskQueue)
	startMockAgentWorker(t, temporalClient, agentTaskQueue, connectorTurnItems(t, 4))

	connectorTaskQueue := TaskQueue(t, "connector-")
	platform := newmockMsgPlatform()
	worker0 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	worker1 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	defer worker1.Stop()

	sessions := []string{"slack:C101", "slack:C102"}
	for _, sess := range sessions {
		msg := testMessage{ID: "r1", Text: "ping"}
		require.NoError(t, startMessageConnector(t, temporalClient, connectorTaskQueue, "integ", sess, msg))
	}
	platform.waitCompletions(t, len(sessions), 30*time.Second)

	worker0.Stop()

	for _, sess := range sessions {
		msg := testMessage{ID: "r2", Text: "pong"}
		require.NoError(t, startMessageConnector(t, temporalClient, connectorTaskQueue, "integ", sess, msg))
	}
	platform.waitCompletions(t, len(sessions), 30*time.Second)

	require.Equal(t, 4, platform.starts, "Stream(start): 2 sessions * 2 rounds")
	require.Equal(t, 4, platform.stops, "Stream(stop): 2 sessions * 2 rounds")
}

func TestConnector_WorkerDiesMidActivity_OtherWorkerCompletes(t *testing.T) {
	// When worker0 dies while Stream(start) is blocking (simulating a crash
	// mid-activity), worker1 picks up the single-turn workflow and the message
	// is delivered via the PostMessage fallback.
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	agentTaskQueue := TaskQueue(t, "agent-")
	CreateNexusEndpoint(t, temporalClient, agent.AgentNexusEndpoint, agentTaskQueue)
	startMockAgentWorker(t, temporalClient, agentTaskQueue, connectorTurnItems(t, 2))

	connectorTaskQueue := TaskQueue(t, "connector-")
	platform := newmockMsgPlatform()
	blocker := &blockOnStart{recording: platform, started: make(chan struct{})}

	worker0 := startConnectorWorker(t, temporalClient, connectorTaskQueue, blocker)

	msg := testMessage{ID: "m1", Text: "hello"}
	wfID := router.RouterWorkflowID("integ", "slack:C003", msg.ID)
	require.NoError(t, startMessageConnector(t, temporalClient, connectorTaskQueue, "integ", "slack:C003", msg))

	select {
	case <-blocker.started:
	case <-time.After(30 * time.Second):
		t.Fatal("timed out waiting for Stream(start) to start on worker-0")
	}

	worker0.Stop()

	worker1 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	defer worker1.Stop()

	platform.waitCompletions(t, 1, 30*time.Second)

	// Wait for the workflow itself to finish. PostMessage completing (above) means
	// the activity returned, but the workflow still needs to process its final task.
	wfCtx, wfCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer wfCancel()
	require.NoError(t, temporalClient.GetWorkflow(wfCtx, wfID, "").Get(wfCtx, nil),
		"message connector workflow must complete without error")
	require.Equal(t, 1, platform.posts, "PostMessage fallback must have been triggered")
}

// -- helpers -----------------------------------------------------------------

// testMessage is one chat message for a test.
type testMessage struct{ ID, Text string }

// startMessageConnector starts a fresh RouterWorkflow for one message.
func startMessageConnector(t *testing.T, tc client.Client, taskQueue, identity, sessionID string, msg testMessage) error {
	t.Helper()
	wfID := router.RouterWorkflowID(identity, sessionID, msg.ID)
	_, err := tc.ExecuteWorkflow(context.Background(),
		client.StartWorkflowOptions{ID: wfID, TaskQueue: taskQueue},
		router.WorkflowName,
		router.Input{
			Request: protocol.Request{SessionID: sessionID, Message: &protocol.Message{Text: msg.Text, Sender: "user"}},
			Address: json.RawMessage(`{}`),
		},
	)
	return err
}

// -- Local copies of workflow-internal wire types -----------------------------
//
// streamItem and turnEvent are unexported in the agent package; we
// replicate their JSON structure here so tests can build correctly-encoded payloads
// without depending on package internals.
// TODO: refactor/figure out how to do this cleanly without duplicating code.
//       the JSON itself is already a duplication of the harness' implementation detail.

const agentTurnEventsTopic = "turn_events"

type agentStreamItem struct {
	TurnID     string         `json:"turn_id"`
	TurnNumber int            `json:"turn_number"`
	Timestamp  float64        `json:"timestamp"`
	Event      agentTurnEvent `json:"event"`
}

type agentTurnEvent struct {
	Type       string         `json:"type"`
	Text       string         `json:"text"`
	ToolName   string         `json:"tool_name"`
	ToolOutput string         `json:"tool_output"`
	Message    string         `json:"message"`
	Delta      map[string]any `json:"delta"`
}

// makeAgentStreamItem encodes an agentStreamItem into the wire format
// expected by the driver's decoder.
func makeAgentStreamItem(t *testing.T, item agentStreamItem, offset int64, topic string) harnessgen.StreamItem {
	t.Helper()
	data, err := json.Marshal(item)
	require.NoError(t, err)
	payload := &commonpb.Payload{
		Metadata: map[string][]byte{"encoding": []byte("json/plain")},
		Data:     data,
	}
	b, err := proto.Marshal(payload)
	require.NoError(t, err)
	return harnessgen.StreamItem{
		Topic:  topic,
		Offset: offset,
		Data:   base64.StdEncoding.EncodeToString(b),
	}
}

// -- mockAgent helpers ---------------------------------------------------

// mockAgentSvc returns a Nexus service that implements the nexus-agent/ service operations.
//
//   - sendMessage always succeeds and reports TurnNumber=1.
//   - pollMessages returns at most 2 items per call (one complete turn) starting
//     at the requested cursor, then Closed=true once all items are exhausted.
func mockAgentSvc(items []harnessgen.StreamItem) *nexus.Service {
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.SendAgentMessage.Name(),
		func(_ context.Context, _ harnessgen.SendAgentMessageInput, _ nexus.StartOperationOptions) (harnessgen.SendMessageOutput, error) {
			return harnessgen.SendMessageOutput{TurnNumber: 1, TurnId: "t1"}, nil
		},
	))
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.PollMessages.Name(),
		func(_ context.Context, input harnessgen.PollMessagesInput, _ nexus.StartOperationOptions) (harnessgen.PollMessagesOutput, error) {
			var out []harnessgen.StreamItem
			for _, item := range items {
				if item.Offset >= input.Cursor {
					out = append(out, item)
					if len(out) == 2 { // cap at one turn per batch
						break
					}
				}
			}
			if len(out) == 0 {
				return harnessgen.PollMessagesOutput{Closed: ptr(true), NextOffset: input.Cursor, Items: []harnessgen.StreamItem{}}, nil
			}
			return harnessgen.PollMessagesOutput{
				Items:      out,
				NextOffset: out[len(out)-1].Offset + 1,
			}, nil
		},
	))
	return svc
}

func startMockAgentWorker(t *testing.T, tc client.Client, agentTaskQueue string, items []harnessgen.StreamItem) sdkworker.Worker {
	t.Helper()
	w := sdkworker.New(tc, agentTaskQueue, sdkworker.Options{DisableWorkflowWorker: true})
	w.RegisterNexusService(mockAgentSvc(items))
	require.NoError(t, w.Start())
	t.Cleanup(func() { w.Stop() })
	return w
}

// testOutboundActivities is the test's platform: the activities that the test
// outbound driver calls. It stands in for a real platform's API calls.
type testOutboundActivities interface {
	BeginStream(ctx context.Context, chunks []protocol.Chunk) (string, error)
	AppendStream(ctx context.Context, in appendInput) error
	FinishStream(ctx context.Context, stream string) error
	PostMessage(ctx context.Context, m timeline.Message) error
}

type appendInput struct {
	Stream string
	Chunks []protocol.Chunk
}

const (
	testBeginStreamActivity  = "TestBeginStream"
	testAppendStreamActivity = "TestAppendStream"
	testFinishStreamActivity = "TestFinishStream"
	testPostMessageActivity  = "TestPostMessage"
)

// testOutboundDriver implements router.Outbound. It runs the timeline in workflow
// code and calls the test activities.
type testOutboundDriver struct{}

var _ router.Outbound = testOutboundDriver{}

func (testOutboundDriver) Deliver(ctx workflow.Context, _ router.Address, state router.State, events []protocol.Event) (router.State, error) {
	return applyTimeline(ctx, state, func(s timeline.Sink, st *timeline.State) {
		timeline.Apply(timeline.Config{Streaming: true}, s, st, events)
	})
}

func (testOutboundDriver) Done(ctx workflow.Context, _ router.Address, state router.State) error {
	if len(state) == 0 {
		return nil
	}
	_, err := applyTimeline(ctx, state, func(s timeline.Sink, st *timeline.State) {
		timeline.End(timeline.Config{Streaming: true}, s, st)
	})
	return err
}

func applyTimeline(ctx workflow.Context, state router.State, run func(timeline.Sink, *timeline.State)) (router.State, error) {
	var st timeline.State
	if len(state) > 0 {
		if err := json.Unmarshal(state, &st); err != nil {
			return nil, err
		}
	}
	run(testSink{ctx: workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})}, &st)
	return json.Marshal(st)
}

type testSink struct{ ctx workflow.Context }

func (s testSink) Now() time.Time { return workflow.Now(s.ctx) }
func (s testSink) Warn(msg string, err error) {
	workflow.GetLogger(s.ctx).Warn(msg, "error", err)
}
func (s testSink) Open(chunks []protocol.Chunk) (string, error) {
	var id string
	err := workflow.ExecuteActivity(s.ctx, testBeginStreamActivity, chunks).Get(s.ctx, &id)
	return id, err
}
func (s testSink) Append(stream string, chunks []protocol.Chunk) error {
	return workflow.ExecuteActivity(s.ctx, testAppendStreamActivity, appendInput{Stream: stream, Chunks: chunks}).Get(s.ctx, nil)
}
func (s testSink) Finish(stream string, _ timeline.Message) error {
	return workflow.ExecuteActivity(s.ctx, testFinishStreamActivity, stream).Get(s.ctx, nil)
}
func (s testSink) Post(m timeline.Message) error {
	return workflow.ExecuteActivity(s.ctx, testPostMessageActivity, m).Get(s.ctx, nil)
}
func (testSink) Status(timeline.Status) error                              { return nil }
func (testSink) Prompt(protocol.ApprovalRequest) (string, error)           { return "", nil }
func (testSink) Settle(timeline.Prompt, protocol.ApprovalResolution) error { return nil }
func (testSink) Guide([]protocol.Handler) error                            { return nil }
func (testSink) Form(protocol.Handler) error                               { return nil }
func (testSink) Error(string) error                                        { return nil }

func startConnectorWorker(t *testing.T, tc client.Client, connectorTaskQueue string, platform testOutboundActivities) sdkworker.Worker {
	t.Helper()
	routerWorkflow := router.NewRouterWorkflow(testOutboundDriver{}, &agent.Driver{})
	w := sdkworker.New(tc, connectorTaskQueue, sdkworker.Options{})
	w.RegisterWorkflowWithOptions(routerWorkflow.Run, workflow.RegisterOptions{Name: router.WorkflowName})
	w.RegisterActivityWithOptions(platform.BeginStream, activity.RegisterOptions{Name: testBeginStreamActivity})
	w.RegisterActivityWithOptions(platform.AppendStream, activity.RegisterOptions{Name: testAppendStreamActivity})
	w.RegisterActivityWithOptions(platform.FinishStream, activity.RegisterOptions{Name: testFinishStreamActivity})
	w.RegisterActivityWithOptions(platform.PostMessage, activity.RegisterOptions{Name: testPostMessageActivity})
	require.NoError(t, w.Start())
	return w
}

// connectorTurnItems pre-generates stream items for n complete turns.
// Each turn i gets two events: a reply_delta at offset 2i and a turn_end at 2i+1.
func connectorTurnItems(t *testing.T, n int) []harnessgen.StreamItem {
	t.Helper()
	items := make([]harnessgen.StreamItem, 0, n*2)
	for i := 0; i < n; i++ {
		base := int64(i * 2)
		items = append(items,
			makeAgentStreamItem(t, agentStreamItem{
				TurnID: fmt.Sprintf("turn-%d", i+1), TurnNumber: 1, Timestamp: float64(i + 1),
				Event: agentTurnEvent{Type: "reply_delta", Text: "hello"},
			}, base, agentTurnEventsTopic),
			makeAgentStreamItem(t, agentStreamItem{
				TurnID: fmt.Sprintf("turn-%d", i+1), TurnNumber: 1, Timestamp: float64(i+1) + 0.5,
				Event: agentTurnEvent{Type: "turn_end"},
			}, base+1, agentTurnEventsTopic),
		)
	}
	return items
}

// -- mockMsgPlatform ---------------------------------------------------------
// Mock outbound activity implementation — tracks start/append/stop/post counts via the
// durable stream lifecycle.

type mockMsgPlatform struct {
	mu          sync.Mutex
	starts      int
	appends     int
	stops       int
	posts       int
	completions chan struct{}
}

func newmockMsgPlatform() *mockMsgPlatform {
	return &mockMsgPlatform{completions: make(chan struct{}, 64)}
}

func (p *mockMsgPlatform) BeginStream(_ context.Context, _ []protocol.Chunk) (string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.starts++
	return "stream-1", nil
}

func (p *mockMsgPlatform) AppendStream(_ context.Context, _ appendInput) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.appends++
	return nil
}

func (p *mockMsgPlatform) FinishStream(_ context.Context, _ string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.stops++
	p.completions <- struct{}{}
	return nil
}

func (p *mockMsgPlatform) PostMessage(_ context.Context, _ timeline.Message) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.posts++
	p.completions <- struct{}{}
	return nil
}

func (p *mockMsgPlatform) counts() (starts, appends, stops, posts int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.starts, p.appends, p.stops, p.posts
}

func (p *mockMsgPlatform) waitCompletions(t *testing.T, n int, timeout time.Duration) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	for i := range n {
		select {
		case <-p.completions:
		case <-ctx.Done():
			t.Fatalf("timeout waiting for completion %d/%d within %v", i+1, n, timeout)
		}
	}
}

// -- blockOnStart --------------------------------------------------------------

type blockOnStart struct {
	recording *mockMsgPlatform
	started   chan struct{}
	once      sync.Once
}

func (b *blockOnStart) BeginStream(ctx context.Context, _ []protocol.Chunk) (string, error) {
	// Block the start call until the context is cancelled (simulates worker crash).
	b.once.Do(func() { close(b.started) })
	<-ctx.Done()
	return "", ctx.Err()
}

func (b *blockOnStart) AppendStream(ctx context.Context, in appendInput) error {
	return b.recording.AppendStream(ctx, in)
}

func (b *blockOnStart) FinishStream(ctx context.Context, stream string) error {
	return b.recording.FinishStream(ctx, stream)
}

func (b *blockOnStart) PostMessage(ctx context.Context, m timeline.Message) error {
	return b.recording.PostMessage(ctx, m)
}
