// Package handler implements SubagentService: an A2A-shaped (Task/Message) Nexus handler for
// subagent dispatch. Forked from nexus/agent_adapter, not a modification — that package is
// shared with the Slack/UI connector and must stay untouched.
//
// Go-only because pollTaskUpdates needs update-with-callback, which the Python SDK can't do.
package handler

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"strings"
	"time"

	"github.com/nexus-rpc/sdk-go/nexus"
	commonpb "go.temporal.io/api/common/v1"
	enumspb "go.temporal.io/api/enums/v1"
	failurepb "go.temporal.io/api/failure/v1"
	"go.temporal.io/api/serviceerror"
	updatepb "go.temporal.io/api/update/v1"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/temporalnexus"
)

// Same harness workflow update/query names agent_adapter uses — only the Nexus envelope differs.
const (
	AgentStatusQuery             = "agent_status"
	SendAgentMessageUpdate       = "send_agent_message"
	ExecuteOperatorCommandUpdate = "execute_operator_command"
	WorkflowStreamPollUpdate     = "__temporal_workflow_stream_poll"
	WorkflowStreamOffsetQuery    = "__temporal_workflow_stream_offset"
	TurnEventsTopic              = "turn_events"
	DefaultPollTimeoutSeconds    = 30
	// StopCommandName: cancelTask maps to the harness's generic /stop command; there's no
	// separate cancel concept.
	StopCommandName = "stop"
)

// Config is per-deployment: which agent workflow this handler fronts.
type Config struct {
	AgentTaskQueue          string
	WorkflowName            string
	WorkflowIDPrefix        string
	IsMessageQueuingEnabled bool
}

// Harness wire types (unchanged from agent_adapter). The workflow itself never sees
// Task/Message — this adapter translates to/from these at the boundary.

type agentStartConfig struct {
	IsMessageQueuingEnabled bool `json:"is_message_queuing_enabled"`
}

// AgentMessage is the payload of the send_agent_message update.
type AgentMessage struct {
	Type         string         `json:"type"`
	Payload      map[string]any `json:"payload"`
	ExpectedTurn int            `json:"expected_turn"`
}

type UserInputResult struct {
	TurnNumber int    `json:"turn_number"`
	TurnID     string `json:"turn_id"`
	Pending    bool   `json:"pending"`
}

type AgentStatus struct {
	AgentID                 string `json:"agent_id"`
	CurrentTurn             int    `json:"current_turn"`
	TurnActive              bool   `json:"turn_active"`
	PendingTurns            []any  `json:"pending_turns"`
	IsMessageQueuingEnabled bool   `json:"is_message_queuing_enabled"`
}

type streamPollInput struct {
	FromOffset int64    `json:"from_offset"`
	Topics     []string `json:"topics"`
}

type operatorCommandRequest struct {
	Name string `json:"name"`
	Arg  string `json:"arg,omitempty"`
}

type operatorCommandResult struct {
	Text string `json:"text"`
}

// Task <-> harness-turn translation helpers.

// handlerPayload is what a Message's first "data" Part decodes to — see subagent.nexusrpc.yaml.
type handlerPayload struct {
	Handler string         `json:"handler"`
	Input   map[string]any `json:"input"`
}

func decodeHandlerPart(msg Message) (handlerPayload, error) {
	if len(msg.Parts) == 0 || msg.Parts[0].Kind != "data" {
		return handlerPayload{}, fmt.Errorf("message.parts[0] must be a 'data' part carrying {handler, input}")
	}
	var hp handlerPayload
	if err := json.Unmarshal([]byte(msg.Parts[0].Data), &hp); err != nil {
		return handlerPayload{}, fmt.Errorf("invalid handler-part JSON: %w", err)
	}
	if hp.Handler == "" {
		return handlerPayload{}, fmt.Errorf("handler-part missing required 'handler' field")
	}
	return hp, nil
}

// taskFromTurn is the response right after a turn is accepted — always "working"; poll/getTask
// report the real outcome.
func taskFromTurn(taskID string, result UserInputResult, streamHeadOffset int64) Task {
	return Task{
		ID:        taskID,
		ContextID: taskID,
		// MessageID = the turn id, for correlating with pollTaskUpdates (turn_driver.py filters on it).
		Status:           TaskStatus{State: "working", Message: &Message{MessageID: result.TurnID}},
		StreamHeadOffset: streamHeadOffset,
		TurnNumber:       int64(result.TurnNumber),
	}
}

// taskFromStatus approximates a Task from AgentStatus: no native "input-required"/"failed"
// state exists, so turnActive -> working, idle -> completed.
func taskFromStatus(taskID string, status AgentStatus) Task {
	state := "completed"
	if status.TurnActive {
		state = "working"
	}
	return Task{
		ID:        taskID,
		ContextID: taskID,
		Status:    TaskStatus{State: state},
	}
}

func NewSubagentNexusService(cfg Config) *nexus.Service {
	svc := nexus.NewService(SubagentService.ServiceName)
	svc.MustRegister(
		newSendMessageOperation(cfg),
		newGetTaskOperation(cfg),
		newCancelTaskOperation(cfg),
		newPollTaskUpdatesOperation(cfg),
	)
	return svc
}

// sendMessageTurn delivers via UpdateWithStartWorkflow. task_id doubles as the workflow id
// (USE_EXISTING conflict policy), so one call both starts and continues a task.
func sendMessageTurn(
	ctx context.Context,
	c client.Client,
	cfg Config,
	msg Message,
	requestID string,
) (Task, error) {
	hp, err := decodeHandlerPart(msg)
	if err != nil {
		// Caller error — non-retryable, it'll never decode.
		return Task{}, nexus.NewHandlerErrorf(nexus.HandlerErrorTypeBadRequest, "invalid message: %w", err)
	}
	workflowID := cfg.WorkflowIDPrefix + msg.TaskID
	startCfg := agentStartConfig{IsMessageQueuingEnabled: cfg.IsMessageQueuingEnabled}

	expectedTurn := 1
	maxRetries := 5
	for attempt := range maxRetries {
		if attempt > 0 {
			qh, err := c.QueryWorkflow(ctx, workflowID, "", AgentStatusQuery)
			if err != nil {
				return Task{}, fmt.Errorf("query agent_status failed with: %w", err)
			}
			var status AgentStatus
			if err := qh.Get(&status); err != nil {
				return Task{}, fmt.Errorf("decode agent_status failed with: %w", err)
			}
			expectedTurn = status.CurrentTurn + len(status.PendingTurns) + 1
		}

		streamHeadOffset := int64(0)
		if qh, err := c.QueryWorkflow(ctx, workflowID, "", WorkflowStreamOffsetQuery); err == nil {
			_ = qh.Get(&streamHeadOffset)
		}

		agentMsg := AgentMessage{Type: hp.Handler, Payload: hp.Input, ExpectedTurn: expectedTurn}
		startOp := c.NewWithStartWorkflowOperation(
			client.StartWorkflowOptions{
				ID:                       workflowID,
				TaskQueue:                cfg.AgentTaskQueue,
				WorkflowIDConflictPolicy: enumspb.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING,
			},
			cfg.WorkflowName, startCfg,
		)
		updateHandle, err := c.UpdateWithStartWorkflow(ctx, client.UpdateWithStartWorkflowOptions{
			StartWorkflowOperation: startOp,
			UpdateOptions: client.UpdateWorkflowOptions{
				UpdateID:     fmt.Sprintf("send-%s-%d", requestID, attempt),
				WorkflowID:   workflowID,
				UpdateName:   SendAgentMessageUpdate,
				Args:         []any{agentMsg},
				WaitForStage: client.WorkflowUpdateStageCompleted,
			},
		})
		if err != nil {
			return Task{}, fmt.Errorf("UpdateWithStart failed with: %w", err)
		}

		var result UserInputResult
		if err := updateHandle.Get(ctx, &result); err != nil {
			if isStaleTurn(err) {
				time.Sleep(time.Duration((attempt+1)*50) * time.Millisecond)
				continue
			}
			return Task{}, fmt.Errorf("get update result failed with: %w", err)
		}

		return taskFromTurn(msg.TaskID, result, streamHeadOffset), nil
	}
	return Task{}, fmt.Errorf("sendMessageTurn: exhausted retries")
}

// taskLookupError: NotFound -> non-retryable (bad/garbage taskID); anything else stays a
// plain, default-retryable error.
func taskLookupError(taskID string, err error) error {
	var notFound *serviceerror.NotFound
	if errors.As(err, &notFound) {
		return nexus.NewHandlerErrorf(nexus.HandlerErrorTypeNotFound, "task %q not found: %w", taskID, err)
	}
	return fmt.Errorf("lookup failed with: %w", err)
}

func isStaleTurn(err error) bool {
	var appErr *temporal.ApplicationError
	return errors.As(err, &appErr) && appErr.Type() == "StaleTurn"
}

func newSendMessageOperation(cfg Config) nexus.Operation[SendMessageInput, Task] {
	return nexus.NewSyncOperation(
		SubagentService.SendMessage.Name(),
		func(ctx context.Context, input SendMessageInput, opts nexus.StartOperationOptions) (Task, error) {
			c := temporalnexus.GetClient(ctx)
			return sendMessageTurn(ctx, c, cfg, input.Message, opts.RequestID)
		},
	)
}

// getTask: point-in-time snapshot, no cursor consumed.
func newGetTaskOperation(cfg Config) nexus.Operation[GetTaskInput, Task] {
	return nexus.NewSyncOperation(
		SubagentService.GetTask.Name(),
		func(ctx context.Context, input GetTaskInput, _ nexus.StartOperationOptions) (Task, error) {
			c := temporalnexus.GetClient(ctx)
			workflowID := cfg.WorkflowIDPrefix + input.TaskID
			qh, err := c.QueryWorkflow(ctx, workflowID, "", AgentStatusQuery)
			if err != nil {
				return Task{}, taskLookupError(input.TaskID, err)
			}
			var status AgentStatus
			if err := qh.Get(&status); err != nil {
				return Task{}, fmt.Errorf("decode agent_status failed with: %w", err)
			}
			return taskFromStatus(input.TaskID, status), nil
		},
	)
}

// cancelTask maps to the harness's generic "stop" operator command.
func newCancelTaskOperation(cfg Config) nexus.Operation[CancelTaskInput, Task] {
	return nexus.NewSyncOperation(
		SubagentService.CancelTask.Name(),
		func(ctx context.Context, input CancelTaskInput, opts nexus.StartOperationOptions) (Task, error) {
			c := temporalnexus.GetClient(ctx)
			workflowID := cfg.WorkflowIDPrefix + input.TaskID
			handle, err := c.UpdateWorkflow(ctx, client.UpdateWorkflowOptions{
				UpdateID:     fmt.Sprintf("cancel-%s", opts.RequestID),
				WorkflowID:   workflowID,
				UpdateName:   ExecuteOperatorCommandUpdate,
				Args:         []any{operatorCommandRequest{Name: StopCommandName}},
				WaitForStage: client.WorkflowUpdateStageCompleted,
			})
			if err != nil {
				return Task{}, taskLookupError(input.TaskID, err)
			}
			var result operatorCommandResult
			if err := handle.Get(ctx, &result); err != nil {
				return Task{}, fmt.Errorf("get stop command result failed with: %w", err)
			}
			return Task{
				ID:        input.TaskID,
				ContextID: input.TaskID,
				Status:    TaskStatus{State: "canceled"},
			}, nil
		},
	)
}

// pollTaskUpdates: async op via update-with-callback (see package doc). Delegates straight to
// the target workflow's own stream-poll update handler, so the output decodes from ITS result.

type pollTaskUpdatesOperation struct {
	nexus.UnimplementedOperation[PollTaskUpdatesInput, PollTaskUpdatesOutput]
	cfg Config
}

func newPollTaskUpdatesOperation(cfg Config) nexus.Operation[PollTaskUpdatesInput, PollTaskUpdatesOutput] {
	return &pollTaskUpdatesOperation{cfg: cfg}
}

func (o *pollTaskUpdatesOperation) Name() string { return SubagentService.PollTaskUpdates.Name() }

// Start attaches a completion callback directly via WorkflowService.UpdateWorkflowExecution
// (same trick as agent_adapter's pollMessages).
func (o *pollTaskUpdatesOperation) Start(
	ctx context.Context,
	input PollTaskUpdatesInput,
	opts nexus.StartOperationOptions,
) (nexus.HandlerStartOperationResult[PollTaskUpdatesOutput], error) {
	c := temporalnexus.GetClient(ctx)
	info := temporalnexus.GetOperationInfo(ctx)
	dc := converter.GetDefaultDataConverter()

	workflowID := o.cfg.WorkflowIDPrefix + input.TaskID
	updateID := fmt.Sprintf("poll-%s", opts.RequestID)
	timeoutSeconds := input.TimeoutSeconds
	if timeoutSeconds <= 0 {
		timeoutSeconds = DefaultPollTimeoutSeconds
	}

	pollInput := streamPollInput{
		FromOffset: input.Cursor,
		Topics:     []string{TurnEventsTopic},
	}
	payload, err := dc.ToPayload(pollInput)
	if err != nil {
		return nil, fmt.Errorf("encode streamPollInput failed with: %w", err)
	}
	resp, err := c.WorkflowService().UpdateWorkflowExecution(ctx, &workflowservice.UpdateWorkflowExecutionRequest{
		Namespace:         info.Namespace,
		WorkflowExecution: &commonpb.WorkflowExecution{WorkflowId: workflowID},
		WaitPolicy: &updatepb.WaitPolicy{
			LifecycleStage: enumspb.UPDATE_WORKFLOW_EXECUTION_LIFECYCLE_STAGE_ACCEPTED,
		},
		Request: &updatepb.Request{
			Meta:                &updatepb.Meta{UpdateId: updateID, Identity: info.TaskQueue},
			Input:               &updatepb.Input{Name: WorkflowStreamPollUpdate, Args: &commonpb.Payloads{Payloads: []*commonpb.Payload{payload}}},
			RequestId:           opts.RequestID,
			CompletionCallbacks: buildCompletionCallbacks(opts),
		},
	})
	if err != nil {
		if isWorkflowCompleted(err) {
			return &nexus.HandlerStartOperationResultSync[PollTaskUpdatesOutput]{
				Value: PollTaskUpdatesOutput{Closed: true, NextOffset: input.Cursor},
			}, nil
		}
		return nil, fmt.Errorf("UpdateWorkflowExecution: %w", err)
	}

	outcome := resp.GetOutcome()

	if failure := outcome.GetFailure(); failure != nil {
		return nil, nexusFailureToHandlerError(failure)
	}

	if success := outcome.GetSuccess(); success != nil {
		var out PollTaskUpdatesOutput
		if err := dc.FromPayloads(success, &out); err != nil {
			return nil, fmt.Errorf("decode PollTaskUpdatesOutput: %w", err)
		}
		return &nexus.HandlerStartOperationResultSync[PollTaskUpdatesOutput]{Value: out}, nil
	}

	token, err := encodePollToken(workflowID, updateID)
	if err != nil {
		return nil, err
	}
	return &nexus.HandlerStartOperationResultAsync{OperationToken: token}, nil
}

func (o *pollTaskUpdatesOperation) Cancel(_ context.Context, _ string, _ nexus.CancelOperationOptions) error {
	return nil
}

func buildCompletionCallbacks(opts nexus.StartOperationOptions) []*commonpb.Callback {
	if opts.CallbackURL == "" {
		return nil
	}
	header := make(map[string]string)
	maps.Copy(header, opts.CallbackHeader)
	return []*commonpb.Callback{{
		Variant: &commonpb.Callback_Nexus_{
			Nexus: &commonpb.Callback_Nexus{Url: opts.CallbackURL, Header: header},
		},
	}}
}

type pollToken struct {
	WorkflowID string `json:"wid"`
	UpdateID   string `json:"uid"`
}

func encodePollToken(workflowID, updateID string) (string, error) {
	data, err := json.Marshal(pollToken{WorkflowID: workflowID, UpdateID: updateID})
	if err != nil {
		return "", fmt.Errorf("marshal poll token: %w", err)
	}
	return base64.URLEncoding.WithPadding(base64.NoPadding).EncodeToString(data), nil
}

func isWorkflowCompleted(err error) bool {
	return strings.Contains(err.Error(), "workflow execution already completed")
}

func nexusFailureToHandlerError(failure *failurepb.Failure) error {
	return nexus.NewHandlerErrorf(nexus.HandlerErrorTypeInternal, "%s", failure.GetMessage())
}
