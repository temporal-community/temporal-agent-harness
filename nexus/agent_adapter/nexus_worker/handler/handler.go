// Package handler implements the AgentService Nexus handler in Go.
//
// The reason why this has to be done in Go is because pollMessages
// relies on the update-with-callback pattern, which is currently only
// only done server-side without native SDK support. Therefore we need
// to manually attach the completion callbacks to the WorkflowService UpdateWorkflowExecution
// request (which would be what the SDK _would do_ once it supports this pattern natively).
//
// The reason why we cannot do this in Python is because the Python SDK
// does not directly makes the gRPC call. When we compose a request,
// the SDK will serialize and hand it to the Rust bridge, which makes the gRPC call.
// This means that the newer completion callbacks that we manually attach
// would get stripped out by the Rust bridge's SerDe.
package handler

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"maps"
	"strings"
	"time"

	"github.com/nexus-rpc/sdk-go/nexus"
	commonpb "go.temporal.io/api/common/v1"
	enumspb "go.temporal.io/api/enums/v1"
	failurepb "go.temporal.io/api/failure/v1"
	updatepb "go.temporal.io/api/update/v1"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/temporalnexus"
)

// ---------------------------------------------------------------------------
// Internal workflow constants
// ---------------------------------------------------------------------------

const (
	AgentStatusQuery             = "agent_status"
	AgentInterfaceQuery          = "agent_interface"
	OperatorInterfaceQuery       = "operator_interface"
	SendAgentMessageUpdate       = "send_agent_message"
	ToolApprovalUpdate           = "tool_approval"
	ExecuteOperatorCommandUpdate = "execute_operator_command"
	// These are hacks that we need for now to invoke the WorkflowStream's
	// update handler and query handler, which allows the pollMessages operation
	// to receive async callbacks and allows us to read the current stream head.
	//
	// Long-term, there should probably be a native solution, but for now we're
	// leaking the abstraction of the stream a bit (by subscribing to its update
	// handler directly).
	WorkflowStreamPollUpdate  = "__temporal_workflow_stream_poll"
	WorkflowStreamOffsetQuery = "__temporal_workflow_stream_offset"
	TurnEventsTopic           = "turn_events"
	DefaultPollTimeoutSeconds = 30
)

// Config parameterises the Nexus handler for a specific harness agent deployment.
// All fields are agent-specific; the protocol constants above are harness-level and fixed.
type Config struct {
	// AgentTaskQueue is the task queue the target agent workflow runs on.
	AgentTaskQueue string
	// WorkflowName is the registered Temporal workflow type name (e.g. "MyAgentWorkflow").
	WorkflowName string
	// WorkflowIDPrefix is prepended to the session ID to form the workflow ID.
	WorkflowIDPrefix string
	// IsMessageQueuingEnabled is forwarded in the workflow start config.
	IsMessageQueuingEnabled bool
}

// ---------------------------------------------------------------------------
// Internal Python workflow wire types (json/plain, snake_case).
// We need to mirror the harness' types here (unless if we generate stubs
// from both sides using the Nexus IDL or something...)
//
// I did not want another middleware to convert the harness into an _inner_
// Nexus service, so I did not go with that approach here, but rather just
// directly mirroring the types for now...
//
// Happy to re-evaluate this approach in the future if it's too much to keep
// in sync.
// ---------------------------------------------------------------------------

// agentStartConfig mirrors the harness AgentConfig wire type for workflow start args.
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
	AgentID                 string            `json:"agent_id"`
	CurrentTurn             int               `json:"current_turn"`
	TurnActive              bool              `json:"turn_active"`
	PendingTurns            []any             `json:"pending_turns"`
	IsMessageQueuingEnabled bool              `json:"is_message_queuing_enabled"`
	PendingApprovals        []pendingApproval `json:"pending_approvals"`
}

// pendingApproval mirrors harness PendingApproval.
type pendingApproval struct {
	ToolID     string         `json:"tool_id"`
	ToolName   string         `json:"tool_name"`
	ToolInput  map[string]any `json:"tool_input"`
	TurnNumber int            `json:"turn_number"`
}

// acceptedFunction mirrors harness AcceptedFunction.
type acceptedFunction struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Parameters  map[string]any `json:"parameters"`
	Output      map[string]any `json:"output"`
}

// streamPollInput matches WorkflowStream PollInput (_types.py).
type streamPollInput struct {
	FromOffset int64    `json:"from_offset"`
	Topics     []string `json:"topics"`
}

// operatorCommandRequest mirrors harness OperatorCommandRequest.
type operatorCommandRequest struct {
	Name string `json:"name"`
	Arg  string `json:"arg,omitempty"`
}

// operatorCommandResult mirrors harness OperatorCommandResult.
type operatorCommandResult struct {
	Text string `json:"text"`
}

// toolApprovalDecision mirrors harness ToolApprovalDecision.
type toolApprovalDecision struct {
	ToolID   string `json:"tool_id"`
	Approved bool   `json:"approved"`
	Reason   string `json:"reason,omitempty"`
	Remember bool   `json:"remember,omitempty"`
}

// toolApprovalResult mirrors harness ToolApprovalResult.
type toolApprovalResult struct {
	ToolID   string `json:"tool_id"`
	Accepted bool   `json:"accepted"`
}

// operatorCommandArgument mirrors harness OperatorCommandArgument.
type operatorCommandArgument struct {
	Kind          string   `json:"kind"`
	Required      bool     `json:"required"`
	Choices       []string `json:"choices,omitempty"`
	Placeholder   string   `json:"placeholder,omitempty"`
	AllowMultiple bool     `json:"allow_multiple,omitempty"`
}

// operatorCommand mirrors harness OperatorCommand (subset of fields the Nexus surface exposes).
type operatorCommand struct {
	Name        string                   `json:"name"`
	Label       string                   `json:"label"`
	Description string                   `json:"description"`
	Source      string                   `json:"source"`
	Argument    *operatorCommandArgument `json:"argument,omitempty"`
}

// ---------------------------------------------------------------------------
// Nexus service assembly
// ---------------------------------------------------------------------------

func NewAgentNexusService(cfg Config) *nexus.Service {
	svc := nexus.NewService(AgentService.ServiceName)
	svc.MustRegister(
		newSendAgentMessageOperation(cfg),
		newExecuteOperatorCommandOperation(cfg),
		newApproveToolCallOperation(cfg),
		newQueryAgentInterfaceOperation(cfg),
		newQueryOperatorInterfaceOperation(cfg),
		newQueryAgentStatusOperation(cfg),
		newPollMessagesOperation(cfg),
	)
	return svc
}

// ---------------------------------------------------------------------------
// sendMessage / sendSlashCommand — shared send_agent_message helper
// ---------------------------------------------------------------------------

// sendAgentMessageTurn delivers an AgentMessage via UpdateWithStartWorkflow and
// returns the turn output. Both sendMessage and sendSlashCommand use this path —
// they differ only in the AgentMessage they construct.
func sendAgentMessageTurn(
	ctx context.Context,
	c client.Client,
	cfg Config,
	workflowID string,
	msg AgentMessage,
	requestID string,
) (SendMessageOutput, error) {
	startCfg := agentStartConfig{IsMessageQueuingEnabled: cfg.IsMessageQueuingEnabled}

	// On the first attempt we optimistically expect turn 1. On retries we query
	// the live status to calculate the correct expected turn.
	expectedTurn := 1
	maxRetries := 5
	for attempt := range maxRetries {
		if attempt > 0 {
			qh, err := c.QueryWorkflow(ctx, workflowID, "", AgentStatusQuery)
			if err != nil {
				return SendMessageOutput{}, fmt.Errorf("query agent_status failed with: %w", err)
			}
			var status AgentStatus
			if err := qh.Get(&status); err != nil {
				return SendMessageOutput{}, fmt.Errorf("decode agent_status failed with: %w", err)
			}
			expectedTurn = status.CurrentTurn + len(status.PendingTurns) + 1
		}

		// Snapshot the stream head before submitting the update so the connector
		// can start polling from the right offset and skip prior-turn events.
		// Zero is correct for a fresh workflow (query returns not-found → offset stays 0).
		streamHeadOffset := 0
		if qh, err := c.QueryWorkflow(ctx, workflowID, "", WorkflowStreamOffsetQuery); err == nil {
			_ = qh.Get(&streamHeadOffset)
		}

		msg.ExpectedTurn = expectedTurn
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
				Args:         []any{msg},
				WaitForStage: client.WorkflowUpdateStageCompleted,
			},
		})
		if err != nil {
			return SendMessageOutput{}, fmt.Errorf("UpdateWithStart failed with: %w", err)
		}

		var result UserInputResult
		if err := updateHandle.Get(ctx, &result); err != nil {
			if isStaleTurn(err) {
				time.Sleep(time.Duration((attempt+1)*50) * time.Millisecond)
				continue
			}
			return SendMessageOutput{}, fmt.Errorf("get update result failed with: %w", err)
		}

		return SendMessageOutput{
			TurnNumber:       int64(result.TurnNumber),
			TurnID:           result.TurnID,
			StreamHeadOffset: int64(streamHeadOffset),
			Pending:          result.Pending,
		}, nil
	}
	return SendMessageOutput{}, fmt.Errorf("sendAgentMessageTurn: exhausted retries")
}

func isStaleTurn(err error) bool {
	var appErr *temporal.ApplicationError
	return errors.As(err, &appErr) && appErr.Type() == "StaleTurn"
}

// newSendAgentMessageOperation is the generic send — mirrors AgentClient.send_message().
// msgType routes to any @agent.accepts handler; payload is its JSON-encoded input model.
func newSendAgentMessageOperation(cfg Config) nexus.Operation[SendAgentMessageInput, SendMessageOutput] {
	return nexus.NewSyncOperation(
		AgentService.SendAgentMessage.Name(),
		func(ctx context.Context, input SendAgentMessageInput, opts nexus.StartOperationOptions) (SendMessageOutput, error) {
			c := temporalnexus.GetClient(ctx)
			workflowID := cfg.WorkflowIDPrefix + input.SessionID
			var payload map[string]any
			if err := json.Unmarshal([]byte(input.Payload), &payload); err != nil {
				return SendMessageOutput{}, fmt.Errorf("invalid payload JSON: %w", err)
			}
			msg := AgentMessage{Type: input.MsgType, Payload: payload}
			return sendAgentMessageTurn(ctx, c, cfg, workflowID, msg, opts.RequestID)
		},
	)
}

// ---------------------------------------------------------------------------
// executeOperatorCommand — harness-level operator commands (no turn)
// ---------------------------------------------------------------------------

func newExecuteOperatorCommandOperation(cfg Config) nexus.Operation[ExecuteOperatorCommandInput, ExecuteOperatorCommandOutput] {
	return nexus.NewSyncOperation(
		AgentService.ExecuteOperatorCommand.Name(),
		func(ctx context.Context, input ExecuteOperatorCommandInput, opts nexus.StartOperationOptions) (ExecuteOperatorCommandOutput, error) {
			c := temporalnexus.GetClient(ctx)
			workflowID := cfg.WorkflowIDPrefix + input.SessionID
			handle, err := c.UpdateWorkflow(ctx, client.UpdateWorkflowOptions{
				UpdateID:     fmt.Sprintf("op-%s", opts.RequestID),
				WorkflowID:   workflowID,
				UpdateName:   ExecuteOperatorCommandUpdate,
				Args:         []any{operatorCommandRequest{Name: input.Name, Arg: input.Arg}},
				WaitForStage: client.WorkflowUpdateStageCompleted,
			})
			if err != nil {
				return ExecuteOperatorCommandOutput{}, fmt.Errorf("UpdateWorkflow failed with: %w", err)
			}
			var result operatorCommandResult
			if err := handle.Get(ctx, &result); err != nil {
				return ExecuteOperatorCommandOutput{}, fmt.Errorf("get operator command result failed with: %w", err)
			}
			return ExecuteOperatorCommandOutput{Reply: result.Text}, nil
		},
	)
}

// ---------------------------------------------------------------------------
// approveToolCall — resolve a pending tool-approval gate
// ---------------------------------------------------------------------------

func newApproveToolCallOperation(cfg Config) nexus.Operation[ApproveToolCallInput, ApproveToolCallOutput] {
	return nexus.NewSyncOperation(
		AgentService.ApproveToolCall.Name(),
		func(ctx context.Context, input ApproveToolCallInput, opts nexus.StartOperationOptions) (ApproveToolCallOutput, error) {
			c := temporalnexus.GetClient(ctx)
			workflowID := cfg.WorkflowIDPrefix + input.SessionID
			handle, err := c.UpdateWorkflow(ctx, client.UpdateWorkflowOptions{
				UpdateID:   fmt.Sprintf("approve-%s", opts.RequestID),
				WorkflowID: workflowID,
				UpdateName: ToolApprovalUpdate,
				Args: []any{toolApprovalDecision{
					ToolID:   input.ToolID,
					Approved: input.Approved,
					Reason:   input.Reason,
					Remember: input.Remember,
				}},
				WaitForStage: client.WorkflowUpdateStageCompleted,
			})
			if err != nil {
				return ApproveToolCallOutput{}, fmt.Errorf("UpdateWorkflow failed with: %w", err)
			}
			var result toolApprovalResult
			if err := handle.Get(ctx, &result); err != nil {
				return ApproveToolCallOutput{}, fmt.Errorf("get tool approval result failed with: %w", err)
			}
			return ApproveToolCallOutput{ToolID: result.ToolID, Accepted: result.Accepted}, nil
		},
	)
}

// ---------------------------------------------------------------------------
// queryOperatorInterface — discover available slash commands
// ---------------------------------------------------------------------------

func newQueryOperatorInterfaceOperation(cfg Config) nexus.Operation[QuerySessionInput, QueryOperatorInterfaceOutput] {
	return nexus.NewSyncOperation(
		AgentService.QueryOperatorInterface.Name(),
		func(ctx context.Context, input QuerySessionInput, _ nexus.StartOperationOptions) (QueryOperatorInterfaceOutput, error) {
			c := temporalnexus.GetClient(ctx)
			workflowID := cfg.WorkflowIDPrefix + input.SessionID
			qh, err := c.QueryWorkflow(ctx, workflowID, "", OperatorInterfaceQuery)
			if err != nil {
				return QueryOperatorInterfaceOutput{}, fmt.Errorf("query operator_interface failed with: %w", err)
			}
			var cmds []operatorCommand
			if err := qh.Get(&cmds); err != nil {
				return QueryOperatorInterfaceOutput{}, fmt.Errorf("decode operator_interface failed with: %w", err)
			}
			out := make([]CommandElement, len(cmds))
			for i, cmd := range cmds {
				el := CommandElement{
					Name:        cmd.Name,
					Label:       cmd.Label,
					Description: cmd.Description,
					Source:      cmd.Source,
				}
				if cmd.Argument != nil {
					el.Argument = &Argument{
						Kind:          cmd.Argument.Kind,
						Required:      cmd.Argument.Required,
						Choices:       cmd.Argument.Choices,
						Placeholder:   cmd.Argument.Placeholder,
						AllowMultiple: cmd.Argument.AllowMultiple,
					}
				}
				out[i] = el
			}
			return QueryOperatorInterfaceOutput{Commands: out}, nil
		},
	)
}

// ---------------------------------------------------------------------------
// queryAgentInterface — discover @agent.accepts handlers (mirrors AgentClient.get_agent_interface)
// ---------------------------------------------------------------------------

func newQueryAgentInterfaceOperation(cfg Config) nexus.Operation[QuerySessionInput, AgentInterfaceOutput] {
	return nexus.NewSyncOperation(
		AgentService.QueryAgentInterface.Name(),
		func(ctx context.Context, input QuerySessionInput, _ nexus.StartOperationOptions) (AgentInterfaceOutput, error) {
			c := temporalnexus.GetClient(ctx)
			workflowID := cfg.WorkflowIDPrefix + input.SessionID
			qh, err := c.QueryWorkflow(ctx, workflowID, "", AgentInterfaceQuery)
			if err != nil {
				return AgentInterfaceOutput{}, fmt.Errorf("query agent_interface failed with: %w", err)
			}
			var fns []acceptedFunction
			if err := qh.Get(&fns); err != nil {
				return AgentInterfaceOutput{}, fmt.Errorf("decode agent_interface failed with: %w", err)
			}
			out := make([]HandlerElement, len(fns))
			for i, fn := range fns {
				params, _ := json.Marshal(fn.Parameters)
				output, _ := json.Marshal(fn.Output)
				out[i] = HandlerElement{
					Name:        fn.Name,
					Description: fn.Description,
					Parameters:  string(params),
					Output:      string(output),
				}
			}
			return AgentInterfaceOutput{Handlers: out}, nil
		},
	)
}

// ---------------------------------------------------------------------------
// queryAgentStatus — session state snapshot (mirrors AgentClient.get_status)
// ---------------------------------------------------------------------------

func newQueryAgentStatusOperation(cfg Config) nexus.Operation[QuerySessionInput, AgentStatusOutput] {
	return nexus.NewSyncOperation(
		AgentService.QueryAgentStatus.Name(),
		func(ctx context.Context, input QuerySessionInput, _ nexus.StartOperationOptions) (AgentStatusOutput, error) {
			c := temporalnexus.GetClient(ctx)
			workflowID := cfg.WorkflowIDPrefix + input.SessionID
			qh, err := c.QueryWorkflow(ctx, workflowID, "", AgentStatusQuery)
			if err != nil {
				return AgentStatusOutput{}, fmt.Errorf("query agent_status failed with: %w", err)
			}
			var status AgentStatus
			if err := qh.Get(&status); err != nil {
				return AgentStatusOutput{}, fmt.Errorf("decode agent_status failed with: %w", err)
			}
			approvals := make([]PendingApprovalElement, len(status.PendingApprovals))
			for i, pa := range status.PendingApprovals {
				input, _ := json.Marshal(pa.ToolInput)
				approvals[i] = PendingApprovalElement{
					ToolID:     pa.ToolID,
					ToolName:   pa.ToolName,
					ToolInput:  string(input),
					TurnNumber: int64(pa.TurnNumber),
				}
			}
			return AgentStatusOutput{
				AgentID:                 status.AgentID,
				CurrentTurn:             int64(status.CurrentTurn),
				TurnActive:              status.TurnActive,
				IsMessageQueuingEnabled: status.IsMessageQueuingEnabled,
				PendingApprovals:        approvals,
			}, nil
		},
	)
}

// ---------------------------------------------------------------------------
// pollMessages — async Nexus operation with update-with-callback
// ---------------------------------------------------------------------------

type pollMessagesOperation struct {
	nexus.UnimplementedOperation[PollMessagesInput, PollMessagesOutput]
	cfg Config
}

func newPollMessagesOperation(cfg Config) nexus.Operation[PollMessagesInput, PollMessagesOutput] {
	return &pollMessagesOperation{cfg: cfg}
}

func (o *pollMessagesOperation) Name() string { return AgentService.PollMessages.Name() }

// Start registers a callback for the pollMessages operation by invoking WorkflowService.UpdateWorkflowExecution
// directly with the appropriate completion callbacks attached. When the update is accepted,
// we return an async result with a token that allows the caller to correlate the eventual callback to this operation.
//
// We directly attach onto the WorkflowStream that is getting published, partially by
// knowing its implementation details (i.e., its update handler name WorkflowStreamPollUpdate).
//
// Note that this means we're registering an update callback on every delta that is published
// by the stream, which is a lot of short-lived callbakcs.
//
// TODO (short-term): look into batching some of these.
func (o *pollMessagesOperation) Start(
	ctx context.Context,
	input PollMessagesInput,
	opts nexus.StartOperationOptions,
) (nexus.HandlerStartOperationResult[PollMessagesOutput], error) {
	c := temporalnexus.GetClient(ctx)
	info := temporalnexus.GetOperationInfo(ctx)
	dc := converter.GetDefaultDataConverter()

	log.Printf("[nexus-debug] pollMessages Start: session=%s cursor=%d callbackURL=%q requestID=%q",
		input.SessionID, input.Cursor, opts.CallbackURL, opts.RequestID)

	workflowID := o.cfg.WorkflowIDPrefix + input.SessionID
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
			return &nexus.HandlerStartOperationResultSync[PollMessagesOutput]{
				Value: PollMessagesOutput{Closed: true, NextOffset: input.Cursor},
			}, nil
		}
		return nil, fmt.Errorf("UpdateWorkflowExecution: %w", err)
	}

	outcome := resp.GetOutcome()

	if failure := outcome.GetFailure(); failure != nil {
		return nil, nexusFailureToHandlerError(failure)
	}

	if success := outcome.GetSuccess(); success != nil {
		var out PollMessagesOutput
		if err := dc.FromPayloads(success, &out); err != nil {
			return nil, fmt.Errorf("decode PollMessagesOutput: %w", err)
		}
		return &nexus.HandlerStartOperationResultSync[PollMessagesOutput]{Value: out}, nil
	}

	token, err := encodePollToken(workflowID, updateID)
	if err != nil {
		return nil, err
	}
	return &nexus.HandlerStartOperationResultAsync{OperationToken: token}, nil
}

func (o *pollMessagesOperation) Cancel(_ context.Context, _ string, _ nexus.CancelOperationOptions) error {
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
