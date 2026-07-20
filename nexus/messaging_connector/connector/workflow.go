package connector

import (
	"encoding/json"
	"fmt"
	"time"

	agentiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent"
	agentgen "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent/generated"
	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

// WorkflowName is the single registered name for ConnectorWorkflow.Run.
const WorkflowName = "ConnectorWorkflow"

// ConnectorWorkflow handles all connector interactions through one workflow type.
// The input's non-nil sub-field determines the route: message → ask turn + stream,
// slash → operator or agent slash turn + stream, approval → approveToolCall.
type ConnectorWorkflow struct {
	handler agentiface.MessageHandler
}

func NewConnectorWorkflow(handler agentiface.MessageHandler) *ConnectorWorkflow {
	return &ConnectorWorkflow{handler: handler}
}

// Run is the single entry point for all connector interactions.
func (c *ConnectorWorkflow) Run(ctx workflow.Context, input agentiface.ConnectorWorkflowInput) error {
	switch {
	case input.Message != nil:
		handle, err := c.handler.ReceiveMessageFromPlatform(ctx, input)
		if err != nil {
			workflow.GetLogger(ctx).Warn("ReceiveMessageFromPlatform failed", "error", err)
			return nil
		}
		return c.handler.RespondToPlatform(ctx, handle, input)

	case input.Slash != nil:
		return c.handleSlash(ctx, input)

	case input.Approval != nil:
		return c.handleApproval(ctx, input)

	default:
		workflow.GetLogger(ctx).Warn("ConnectorWorkflow: input has no interaction field set")
		return nil
	}
}

func (c *ConnectorWorkflow) handleSlash(ctx workflow.Context, input agentiface.ConnectorWorkflowInput) error {
	s := input.Slash
	agentClient := workflow.NewNexusClient(agentiface.AgentNexusEndpoint, agentgen.AgentService.ServiceName)
	actCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})

	postMsg := func(text string) {
		_ = workflow.ExecuteActivity(actCtx, msgiface.PostMessageActivity, msgiface.TextMetadata{
			SessionID: input.SessionID,
			ThreadID:  s.ThreadID,
			Text:      text,
		}).Get(ctx, nil)
	}

	// Determine routing by querying the operator interface.
	var ifaceOut agentgen.QueryOperatorInterfaceOutput
	if err := agentClient.ExecuteOperation(ctx, agentgen.AgentService.QueryOperatorInterface,
		agentgen.QuerySessionInput{SessionID: input.SessionID},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 10 * time.Second},
	).Get(ctx, &ifaceOut); err != nil {
		postMsg("_No active session. Start a conversation first before using slash commands._")
		return nil
	}

	// Check whether this is a harness-owned operator command.
	// Agent-owned slash commands (@agent.accepts slash) are NOT in operator_interface —
	// they route directly to sendAgentMessage(type="slash").
	var cmd *agentgen.CommandElement
	for i := range ifaceOut.Commands {
		if ifaceOut.Commands[i].Name == s.Name {
			cmd = &ifaceOut.Commands[i]
			break
		}
	}

	if cmd != nil && cmd.Source == "harness" {
		// Harness operator command: synchronous, no turn, returns text directly.
		var opOut agentgen.ExecuteOperatorCommandOutput
		if err := agentClient.ExecuteOperation(ctx, agentgen.AgentService.ExecuteOperatorCommand,
			agentgen.ExecuteOperatorCommandInput{SessionID: input.SessionID, Name: s.Name, Arg: s.Arg},
			workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second},
		).Get(ctx, &opOut); err != nil {
			postMsg(fmt.Sprintf("_Command failed: %v_", err))
			return nil
		}
		postMsg(opOut.Reply)
		return nil
	}

	// Agent slash command: creates a turn, stream the reply.
	payload, _ := json.Marshal(map[string]string{"name": s.Name, "arg": s.Arg})
	var sendOut agentgen.SendMessageOutput
	if err := agentClient.ExecuteOperation(ctx, agentgen.AgentService.SendAgentMessage,
		agentgen.SendAgentMessageInput{SessionID: input.SessionID, MsgType: "slash", Payload: string(payload)},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 60 * time.Second},
	).Get(ctx, &sendOut); err != nil {
		postMsg(fmt.Sprintf("_Command failed: %v_", err))
		return nil
	}

	handle := agentiface.TurnHandle{TurnNumber: sendOut.TurnNumber, StreamHeadOffset: sendOut.StreamHeadOffset}
	return c.handler.RespondToPlatform(ctx, handle, input)
}

func (c *ConnectorWorkflow) handleApproval(ctx workflow.Context, input agentiface.ConnectorWorkflowInput) error {
	a := input.Approval
	agentClient := workflow.NewNexusClient(agentiface.AgentNexusEndpoint, agentgen.AgentService.ServiceName)
	var out agentgen.ApproveToolCallOutput
	if err := agentClient.ExecuteOperation(ctx, agentgen.AgentService.ApproveToolCall,
		agentgen.ApproveToolCallInput{SessionID: input.SessionID, ToolID: a.ToolID, Approved: a.Approved},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second},
	).Get(ctx, &out); err != nil {
		workflow.GetLogger(ctx).Warn("handleApproval: approveToolCall failed",
			"toolId", a.ToolID, "approved", a.Approved, "error", err)
	}

	// New approval workflow inputs carry the card ID and Bot Framework routing
	// metadata. Older histories deserialize these fields as empty, so they do
	// not schedule this newly introduced activity during replay.
	if a.ActivityID != "" {
		decision := "✅ Approved"
		if !a.Approved {
			decision = "❌ Denied"
		}
		actCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
			StartToCloseTimeout: 5 * time.Minute,
			RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
		})
		if err := workflow.ExecuteActivity(actCtx, msgiface.UpdateActivityActivity, msgiface.UpdateActivityInput{
			TextMetadata: msgiface.TextMetadata{
				SessionID:  input.SessionID,
				Text:       fmt.Sprintf("🔐 Tool `%s`: %s", a.ToolName, decision),
				ServiceURL: a.ServiceURL,
				ChannelID:  a.ChannelID,
			},
			ActivityID: a.ActivityID,
		}).Get(ctx, nil); err != nil {
			workflow.GetLogger(ctx).Warn("handleApproval: update approval card failed", "error", err)
		}
	}
	return nil
}
