package connector

import (
	"context"
	"testing"

	"github.com/nexus-rpc/sdk-go/nexus"
	"github.com/stretchr/testify/require"
	agentiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent"
	agentgen "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent/generated"
	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
)

type mockHandler struct {
	called bool
}

func TestConnectorWorkflow_ApprovalUpdatesTeamsCardViaActivity(t *testing.T) {
	c := NewConnectorWorkflow(&mockHandler{})
	s := testsuite.WorkflowTestSuite{}
	env := s.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(c.Run)

	service := nexus.NewService(agentgen.AgentService.ServiceName)
	service.MustRegister(nexus.NewSyncOperation(
		agentgen.AgentService.ApproveToolCall.Name(),
		func(context.Context, agentgen.ApproveToolCallInput, nexus.StartOperationOptions) (agentgen.ApproveToolCallOutput, error) {
			return agentgen.ApproveToolCallOutput{Accepted: true, ToolID: "tool-1"}, nil
		},
	))
	env.RegisterNexusService(service)

	var update msgiface.UpdateActivityInput
	env.RegisterActivityWithOptions(
		func(_ context.Context, input msgiface.UpdateActivityInput) error {
			update = input
			return nil
		},
		activity.RegisterOptions{Name: msgiface.UpdateActivityActivity},
	)

	env.ExecuteWorkflow(c.Run, agentiface.ConnectorWorkflowInput{
		Identity:  "default",
		SessionID: "teams:conversation-1",
		Approval: &agentiface.ApprovalDecision{
			ToolID:     "tool-1",
			ToolName:   "deploy",
			Approved:   true,
			ActivityID: "card-1",
			ServiceURL: "https://example.test/teams/",
			ChannelID:  "msteams",
		},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Equal(t, "card-1", update.ActivityID)
	require.Equal(t, "teams:conversation-1", update.SessionID)
	require.Equal(t, "https://example.test/teams/", update.ServiceURL)
	require.Equal(t, "msteams", update.ChannelID)
	require.Equal(t, "🔐 Tool `deploy`: ✅ Approved", update.Text)
}

func (m *mockHandler) ReceiveMessageFromPlatform(ctx workflow.Context, input agentiface.ConnectorWorkflowInput) (agentiface.TurnHandle, error) {
	return agentiface.TurnHandle{}, nil
}

func (m *mockHandler) RespondToPlatform(ctx workflow.Context, handle agentiface.TurnHandle, input agentiface.ConnectorWorkflowInput) error {
	m.called = true
	return nil
}

func TestConnectorWorkflow_DelegatesToHandler(t *testing.T) {
	handler := &mockHandler{}
	c := NewConnectorWorkflow(handler)

	s := testsuite.WorkflowTestSuite{}
	env := s.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(c.Run)

	env.ExecuteWorkflow(c.Run, agentiface.ConnectorWorkflowInput{
		Identity:  "default",
		SessionID: "slack:C12345",
		Message:   &agentiface.IncomingMessage{MessageID: "m1", Text: "hello"},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.True(t, handler.called, "expected handler.OnMessageFromPlatform to be called")
}
