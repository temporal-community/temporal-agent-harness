//go:build integration

package nexusinteg

import (
	"context"
	"encoding/base64"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/a2apb"
	"github.com/nexus-rpc/sdk-go/nexus"
	"github.com/stretchr/testify/require"
	a2anexus "github.com/temporal-community/temporal-agent-harness/nexus/a2a"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	sdkworker "go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
)

const (
	testA2AEndpoint      = "test-a2a-agent"
	testDeliveryActivity = "TestDeliverA2A"
)

type mockA2AAgent struct {
	mu    sync.Mutex
	items []a2anexus.StreamItem
	turns int64
}

func (mock *mockA2AAgent) service(t *testing.T) *nexus.Service {
	t.Helper()
	service := nexus.NewService(a2anexus.ServiceName)
	service.MustRegister(nexus.NewSyncOperation(
		"SendMessage",
		func(_ context.Context, input a2anexus.SendMessageRequest, _ nexus.StartOperationOptions) (a2anexus.SendMessageResponse, error) {
			mock.mu.Lock()
			defer mock.mu.Unlock()

			mock.turns++
			offset := int64(len(mock.items))
			taskID := input.Value.GetRequest().GetTaskId()
			if taskID == "" {
				taskID = fmt.Sprintf("task-%d", mock.turns)
			}
			stream := &a2apb.StreamResponse{Payload: &a2apb.StreamResponse_Msg{Msg: &a2apb.Message{
				MessageId: fmt.Sprintf("reply-%d", mock.turns),
				TaskId:    taskID,
				ContextId: taskID,
				Role:      a2apb.Role_ROLE_AGENT,
				Parts: []*a2apb.Part{{Part: &a2apb.Part_Text{
					Text: fmt.Sprintf("reply %d", mock.turns),
				}}},
			}}}
			encoded, err := proto.Marshal(stream)
			if err != nil {
				return a2anexus.SendMessageResponse{}, err
			}
			mock.items = append(mock.items, a2anexus.StreamItem{
				Offset: offset,
				Data:   base64.StdEncoding.EncodeToString(encoded),
			})
			metadata, err := structpb.NewStruct(map[string]any{
				"temporal.io/turn-number":     mock.turns,
				"temporal.io/turn-id":         fmt.Sprintf("turn-%d", mock.turns),
				"temporal.io/accepted-offset": offset,
				"temporal.io/pending":         false,
			})
			if err != nil {
				return a2anexus.SendMessageResponse{}, err
			}
			return a2anexus.SendMessageResponse{Value: &a2apb.SendMessageResponse{
				Payload: &a2apb.SendMessageResponse_Task{Task: &a2apb.Task{
					Id: taskID, ContextId: taskID, Metadata: metadata,
				}},
			}}, nil
		},
	))
	service.MustRegister(nexus.NewSyncOperation(
		"SubscribeToTask",
		func(ctx context.Context, input a2anexus.SubscribeToTaskInput, _ nexus.StartOperationOptions) (a2anexus.SubscribeToTaskOutput, error) {
			deadline := time.NewTimer(50 * time.Millisecond)
			defer deadline.Stop()
			for {
				mock.mu.Lock()
				if input.Cursor < int64(len(mock.items)) {
					items := append([]a2anexus.StreamItem(nil), mock.items[input.Cursor:]...)
					nextCursor := int64(len(mock.items))
					mock.mu.Unlock()
					return a2anexus.SubscribeToTaskOutput{Items: items, NextCursor: nextCursor}, nil
				}
				mock.mu.Unlock()
				select {
				case <-ctx.Done():
					return a2anexus.SubscribeToTaskOutput{}, ctx.Err()
				case <-deadline.C:
					return a2anexus.SubscribeToTaskOutput{NextCursor: input.Cursor}, nil
				case <-time.After(time.Millisecond):
				}
			}
		},
	))
	return service
}

func startMockA2AWorker(t *testing.T, temporalClient client.Client, taskQueue string, mock *mockA2AAgent) sdkworker.Worker {
	t.Helper()
	worker := sdkworker.New(temporalClient, taskQueue, sdkworker.Options{DisableWorkflowWorker: true})
	worker.RegisterNexusService(mock.service(t))
	require.NoError(t, worker.Start())
	t.Cleanup(func() { worker.Stop() })
	return worker
}

type deliveryRecorder struct {
	mu          sync.Mutex
	raw         []string
	text        []string
	completions chan struct{}
}

func newDeliveryRecorder() *deliveryRecorder {
	return &deliveryRecorder{completions: make(chan struct{}, 16)}
}

func (recorder *deliveryRecorder) Deliver(_ context.Context, input router.DeliveryInput) (router.DeliveryOutput, error) {
	complete := false
	recorder.mu.Lock()
	for _, item := range input.Items {
		recorder.raw = append(recorder.raw, item.Data)
		delta, err := agent.DecodeStreamItem(item)
		if err != nil {
			recorder.mu.Unlock()
			return router.DeliveryOutput{}, err
		}
		if delta != nil {
			recorder.text = append(recorder.text, delta.Text)
			complete = complete || delta.IsFinal
		}
	}
	recorder.mu.Unlock()
	if complete {
		recorder.completions <- struct{}{}
	}
	return router.DeliveryOutput{TurnComplete: complete}, nil
}

func (recorder *deliveryRecorder) wait(t *testing.T) {
	t.Helper()
	select {
	case <-recorder.completions:
	case <-time.After(30 * time.Second):
		t.Fatal("timed out waiting for A2A delivery")
	}
}

func (recorder *deliveryRecorder) snapshot() ([]string, []string) {
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	return append([]string(nil), recorder.raw...), append([]string(nil), recorder.text...)
}

func startTunnelWorker(t *testing.T, temporalClient client.Client, taskQueue string, recorder *deliveryRecorder) sdkworker.Worker {
	t.Helper()
	worker := sdkworker.New(temporalClient, taskQueue, sdkworker.Options{})
	tunnel := router.NewTunnelWorkflow(agent.A2ABackend{})
	worker.RegisterWorkflowWithOptions(tunnel.Run, workflow.RegisterOptions{Name: router.TunnelWorkflowName})
	worker.RegisterActivityWithOptions(recorder.Deliver, activity.RegisterOptions{Name: testDeliveryActivity})
	require.NoError(t, worker.Start())
	return worker
}

func sendTurn(t *testing.T, temporalClient client.Client, taskQueue, sessionID, updateID, text string) router.TurnAccepted {
	t.Helper()
	connector := router.NewClient(
		temporalClient,
		taskQueue,
		testA2AEndpoint,
		agent.NewA2AActions(temporalClient, testA2AEndpoint),
	)
	accepted, err := connector.SendAndMount(context.Background(), sessionID, updateID,
		router.SendAndMountInput{
			Subscriber: router.Subscriber{
				ID:   "test-driver",
				Mode: router.TurnOwner,
				Delivery: &router.DeliveryTarget{
					Activity: testDeliveryActivity, TaskQueue: taskQueue,
				},
			},
			Message: router.SendMessageInput{MessageType: "ask", Payload: map[string]any{"text": text}},
		})
	if err != nil && strings.Contains(err.Error(), "unknown method StartNexusOperationExecution") {
		t.Skip("downloaded Temporal dev server does not yet expose standalone Nexus operations")
	}
	require.NoError(t, err)
	return accepted
}

func TestA2ATunnelUsesStandaloneActionsAndBoundedTurnWorkflows(t *testing.T) {
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	agentTaskQueue := TaskQueue(t, "a2a-agent-")
	CreateNexusEndpoint(t, temporalClient, testA2AEndpoint, agentTaskQueue)
	startMockA2AWorker(t, temporalClient, agentTaskQueue, &mockA2AAgent{})

	tunnelTaskQueue := TaskQueue(t, "ui-tunnel-")
	recorder := newDeliveryRecorder()
	worker0 := startTunnelWorker(t, temporalClient, tunnelTaskQueue, recorder)

	sessionID := "agent-session"
	first := sendTurn(t, temporalClient, tunnelTaskQueue, sessionID, "message-1", "hello")
	require.EqualValues(t, 1, first.TurnNumber)
	recorder.wait(t)

	description, err := temporalClient.DescribeWorkflowExecution(
		context.Background(), router.TunnelWorkflowID(sessionID, first.TurnNumber), "",
	)
	require.NoError(t, err)
	require.NotEmpty(t, description.WorkflowExecutionInfo.Execution.RunId)

	worker0.Stop()
	worker1 := startTunnelWorker(t, temporalClient, tunnelTaskQueue, recorder)
	defer worker1.Stop()

	second := sendTurn(t, temporalClient, tunnelTaskQueue, sessionID, "message-2", "again")
	require.EqualValues(t, 2, second.TurnNumber)
	recorder.wait(t)

	afterRestart, err := temporalClient.DescribeWorkflowExecution(
		context.Background(), router.TunnelWorkflowID(sessionID, second.TurnNumber), "",
	)
	require.NoError(t, err)
	require.NotEqual(
		t, description.WorkflowExecutionInfo.Execution.RunId,
		afterRestart.WorkflowExecutionInfo.Execution.RunId,
		"each turn has an independent tunnel execution",
	)

	raw, rendered := recorder.snapshot()
	require.Len(t, raw, 2)
	require.Equal(t, []string{"reply 1", "reply 2"}, rendered)
	for _, encoded := range raw {
		bytes, err := base64.StdEncoding.DecodeString(encoded)
		require.NoError(t, err)
		var stream a2apb.StreamResponse
		require.NoError(t, proto.Unmarshal(bytes, &stream))
		require.NotNil(t, stream.GetMsg(), "the tunnel must deliver the original A2A record")
	}

}
