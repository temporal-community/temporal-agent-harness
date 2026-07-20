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
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/driver/temporal_agent_harness"
	harnessgen "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/driver/temporal_agent_harness/generated"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/wire"
	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
	sdkworker "go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
)

// -- Tests --------------------------------------------------------------------

func TestConnector_TwoMessagesDeliveredAcrossWorkerRestart(t *testing.T) {
	// Verify that after worker0 finishes message-1 and dies, worker1 can still
	// process message-2 for the same session. Each message is an independent
	// single-turn workflow.
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	agentTaskQueue := TaskQueue(t, "agent-")
	CreateNexusEndpoint(t, temporalClient, temporal_agent_harness.AgentNexusEndpoint, agentTaskQueue)
	startMockAgentWorker(t, temporalClient, agentTaskQueue, connectorTurnItems(t, 4))

	connectorTaskQueue := TaskQueue(t, "connector-")
	platform := newmockMsgPlatform()
	worker0 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	worker1 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	defer worker1.Stop()

	msg1 := wire.IncomingMessage{MessageID: "m1", Sender: "user", Text: "hello", Timestamp: "1000.0"}
	require.NoError(t, startMessageConnector(t, temporalClient, connectorTaskQueue, "integ", "slack:C001", msg1))
	platform.waitCompletions(t, 1, 30*time.Second)

	worker0.Stop()

	msg2 := wire.IncomingMessage{MessageID: "m2", Sender: "user", Text: "world", Timestamp: "2000.0"}
	require.NoError(t, startMessageConnector(t, temporalClient, connectorTaskQueue, "integ", "slack:C001", msg2))
	platform.waitCompletions(t, 1, 30*time.Second)

	require.Equal(t, 2, platform.starts, "Stream(start) must be called once per message")
	require.Equal(t, 2, platform.stops, "Stream(stop) must be called once per message")
}

func TestConnector_MultipleSessionsSurviveWorkerDeath(t *testing.T) {
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	agentTaskQueue := TaskQueue(t, "agent-")
	CreateNexusEndpoint(t, temporalClient, temporal_agent_harness.AgentNexusEndpoint, agentTaskQueue)
	startMockAgentWorker(t, temporalClient, agentTaskQueue, connectorTurnItems(t, 4))

	connectorTaskQueue := TaskQueue(t, "connector-")
	platform := newmockMsgPlatform()
	worker0 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	worker1 := startConnectorWorker(t, temporalClient, connectorTaskQueue, platform)
	defer worker1.Stop()

	sessions := []string{"slack:C101", "slack:C102"}
	for _, sess := range sessions {
		msg := wire.IncomingMessage{MessageID: "r1", Sender: "user", Text: "ping", Timestamp: "1000.0"}
		require.NoError(t, startMessageConnector(t, temporalClient, connectorTaskQueue, "integ", sess, msg))
	}
	platform.waitCompletions(t, len(sessions), 30*time.Second)

	worker0.Stop()

	for _, sess := range sessions {
		msg := wire.IncomingMessage{MessageID: "r2", Sender: "user", Text: "pong", Timestamp: "2000.0"}
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
	CreateNexusEndpoint(t, temporalClient, temporal_agent_harness.AgentNexusEndpoint, agentTaskQueue)
	startMockAgentWorker(t, temporalClient, agentTaskQueue, connectorTurnItems(t, 2))

	connectorTaskQueue := TaskQueue(t, "connector-")
	platform := newmockMsgPlatform()
	blocker := &blockOnStart{recording: platform, started: make(chan struct{})}

	worker0 := startConnectorWorker(t, temporalClient, connectorTaskQueue, blocker)

	msg := wire.IncomingMessage{MessageID: "m1", Sender: "user", Text: "hello", Timestamp: "1000.0"}
	wfID := router.RouterWorkflowID("integ", "slack:C003", msg.MessageID)
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

// startMessageConnector starts a fresh RouterWorkflow for one message.
func startMessageConnector(t *testing.T, tc client.Client, taskQueue, identity, sessionID string, msg wire.IncomingMessage) error {
	t.Helper()
	wfID := router.RouterWorkflowID(identity, sessionID, msg.MessageID)
	_, err := tc.ExecuteWorkflow(context.Background(),
		client.StartWorkflowOptions{ID: wfID, TaskQueue: taskQueue},
		router.WorkflowName,
		wire.Input{Identity: identity, SessionID: sessionID, Message: &msg},
	)
	return err
}

// -- Local copies of workflow-internal wire types -----------------------------
//
// streamItem and turnEvent are unexported in the temporal_agent_harness package; we
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
func makeAgentStreamItem(t *testing.T, item agentStreamItem, offset int64, topic string) harnessgen.ItemElement {
	t.Helper()
	data, err := json.Marshal(item)
	require.NoError(t, err)
	payload := &commonpb.Payload{
		Metadata: map[string][]byte{"encoding": []byte("json/plain")},
		Data:     data,
	}
	b, err := proto.Marshal(payload)
	require.NoError(t, err)
	return harnessgen.ItemElement{
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
func mockAgentSvc(items []harnessgen.ItemElement) *nexus.Service {
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.SendAgentMessage.Name(),
		func(_ context.Context, _ harnessgen.SendAgentMessageInput, _ nexus.StartOperationOptions) (harnessgen.SendMessageOutput, error) {
			return harnessgen.SendMessageOutput{TurnNumber: 1, TurnID: "t1"}, nil
		},
	))
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.PollMessages.Name(),
		func(_ context.Context, input harnessgen.PollMessagesInput, _ nexus.StartOperationOptions) (harnessgen.PollMessagesOutput, error) {
			var out []harnessgen.ItemElement
			for _, item := range items {
				if item.Offset >= input.Cursor {
					out = append(out, item)
					if len(out) == 2 { // cap at one turn per batch
						break
					}
				}
			}
			if len(out) == 0 {
				return harnessgen.PollMessagesOutput{Closed: true, NextOffset: input.Cursor}, nil
			}
			return harnessgen.PollMessagesOutput{
				Items:      out,
				NextOffset: out[len(out)-1].Offset + 1,
			}, nil
		},
	))
	return svc
}

func startMockAgentWorker(t *testing.T, tc client.Client, agentTaskQueue string, items []harnessgen.ItemElement) sdkworker.Worker {
	t.Helper()
	w := sdkworker.New(tc, agentTaskQueue, sdkworker.Options{DisableWorkflowWorker: true})
	w.RegisterNexusService(mockAgentSvc(items))
	require.NoError(t, w.Start())
	t.Cleanup(func() { w.Stop() })
	return w
}

// testInboundActivities is satisfied by anything providing the raw (context.Context)
// activity implementations this test registers on the connector worker — the test
// double standing in for a real platform driver's activities (cf. SlackPlatform).
type testInboundActivities interface {
	BeginStream(ctx context.Context, input inbound.BeginStreamInput) (inbound.StreamHandle, error)
	UpdateStream(ctx context.Context, input inbound.UpdateStreamInput) error
	FinishStream(ctx context.Context, input inbound.FinishStreamInput) error
	PostMessage(ctx context.Context, input inbound.TextMetadata) error
	PostApprovalPrompt(ctx context.Context, input inbound.ApprovalPromptInput) error
}

// Activity name constants for this test's inbound driver, private to this file — a
// real driver (e.g. slack.Driver) is free to choose its own.
const (
	testBeginStreamActivity        = "TestBeginStream"
	testUpdateStreamActivity       = "TestUpdateStream"
	testFinishStreamActivity       = "TestFinishStream"
	testPostMessageActivity        = "TestPostMessage"
	testPostApprovalPromptActivity = "TestPostApprovalPrompt"
)

// testInboundDriver implements inbound.Driver by dispatching to whichever
// testInboundActivities implementation the test registered on the worker.
type testInboundDriver struct{}

func (testInboundDriver) activityOptions(ctx workflow.Context) workflow.Context {
	return workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})
}

func (d testInboundDriver) BeginStream(ctx workflow.Context, input inbound.BeginStreamInput) (inbound.StreamHandle, error) {
	var handle inbound.StreamHandle
	err := workflow.ExecuteActivity(d.activityOptions(ctx), testBeginStreamActivity, input).Get(ctx, &handle)
	return handle, err
}

func (d testInboundDriver) UpdateStream(ctx workflow.Context, input inbound.UpdateStreamInput) error {
	return workflow.ExecuteActivity(d.activityOptions(ctx), testUpdateStreamActivity, input).Get(ctx, nil)
}

func (d testInboundDriver) FinishStream(ctx workflow.Context, input inbound.FinishStreamInput) error {
	return workflow.ExecuteActivity(d.activityOptions(ctx), testFinishStreamActivity, input).Get(ctx, nil)
}

func (d testInboundDriver) PostMessage(ctx workflow.Context, input inbound.TextMetadata) error {
	return workflow.ExecuteActivity(d.activityOptions(ctx), testPostMessageActivity, input).Get(ctx, nil)
}

func (d testInboundDriver) PostApprovalPrompt(ctx workflow.Context, input inbound.ApprovalPromptInput) error {
	return workflow.ExecuteActivity(d.activityOptions(ctx), testPostApprovalPromptActivity, input).Get(ctx, nil)
}

func (testInboundDriver) UpdateActivity(workflow.Context, inbound.UpdateActivityInput) error {
	return nil
}

func startConnectorWorker(t *testing.T, tc client.Client, connectorTaskQueue string, platform testInboundActivities) sdkworker.Worker {
	t.Helper()
	routerWorkflow := router.NewRouterWorkflow(testInboundDriver{}, &temporal_agent_harness.Driver{})
	w := sdkworker.New(tc, connectorTaskQueue, sdkworker.Options{})
	w.RegisterWorkflowWithOptions(routerWorkflow.Run, workflow.RegisterOptions{Name: router.WorkflowName})
	w.RegisterActivityWithOptions(platform.BeginStream, activity.RegisterOptions{Name: testBeginStreamActivity})
	w.RegisterActivityWithOptions(platform.UpdateStream, activity.RegisterOptions{Name: testUpdateStreamActivity})
	w.RegisterActivityWithOptions(platform.FinishStream, activity.RegisterOptions{Name: testFinishStreamActivity})
	w.RegisterActivityWithOptions(platform.PostMessage, activity.RegisterOptions{Name: testPostMessageActivity})
	w.RegisterActivityWithOptions(platform.PostApprovalPrompt, activity.RegisterOptions{Name: testPostApprovalPromptActivity})
	require.NoError(t, w.Start())
	return w
}

// connectorTurnItems pre-generates stream items for n complete turns.
// Each turn i gets two events: a reply_delta at offset 2i and a reply at 2i+1.
func connectorTurnItems(t *testing.T, n int) []harnessgen.ItemElement {
	t.Helper()
	items := make([]harnessgen.ItemElement, 0, n*2)
	for i := 0; i < n; i++ {
		base := int64(i * 2)
		items = append(items,
			makeAgentStreamItem(t, agentStreamItem{
				TurnID: fmt.Sprintf("turn-%d", i+1), TurnNumber: 1, Timestamp: float64(i + 1),
				Event: agentTurnEvent{Type: "reply_delta", Text: "hello"},
			}, base, agentTurnEventsTopic),
			makeAgentStreamItem(t, agentStreamItem{
				TurnID: fmt.Sprintf("turn-%d", i+1), TurnNumber: 1, Timestamp: float64(i+1) + 0.5,
				Event: agentTurnEvent{Type: "reply"},
			}, base+1, agentTurnEventsTopic),
		)
	}
	return items
}

// -- mockMsgPlatform ---------------------------------------------------------
// Mock inbound activity implementation — tracks start/append/stop/post counts via the
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

func (p *mockMsgPlatform) BeginStream(_ context.Context, in inbound.BeginStreamInput) (inbound.StreamHandle, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.starts++
	return inbound.StreamHandle{
		ID:           "stream-1",
		SessionID:    in.SessionID,
		WireTextMode: inbound.StreamWireTextDelta,
	}, nil
}

func (p *mockMsgPlatform) UpdateStream(_ context.Context, _ inbound.UpdateStreamInput) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.appends++
	return nil
}

func (p *mockMsgPlatform) FinishStream(_ context.Context, _ inbound.FinishStreamInput) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.stops++
	p.completions <- struct{}{}
	return nil
}

func (p *mockMsgPlatform) PostMessage(_ context.Context, _ inbound.TextMetadata) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.posts++
	p.completions <- struct{}{}
	return nil
}

// PostApprovalPrompt is a no-op here — none of these durability tests exercise
// the tool-approval flow, but the mock must still satisfy testInboundActivities.
func (p *mockMsgPlatform) PostApprovalPrompt(_ context.Context, _ inbound.ApprovalPromptInput) error {
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

func (b *blockOnStart) BeginStream(ctx context.Context, in inbound.BeginStreamInput) (inbound.StreamHandle, error) {
	// Block the start call until the context is cancelled (simulates worker crash).
	b.once.Do(func() { close(b.started) })
	<-ctx.Done()
	return inbound.StreamHandle{}, ctx.Err()
}

func (b *blockOnStart) UpdateStream(ctx context.Context, in inbound.UpdateStreamInput) error {
	return b.recording.UpdateStream(ctx, in)
}

func (b *blockOnStart) FinishStream(ctx context.Context, in inbound.FinishStreamInput) error {
	return b.recording.FinishStream(ctx, in)
}

func (b *blockOnStart) PostMessage(ctx context.Context, in inbound.TextMetadata) error {
	return b.recording.PostMessage(ctx, in)
}

func (b *blockOnStart) PostApprovalPrompt(ctx context.Context, in inbound.ApprovalPromptInput) error {
	return b.recording.PostApprovalPrompt(ctx, in)
}
