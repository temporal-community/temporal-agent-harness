package connector

import (
	"testing"

	"github.com/stretchr/testify/require"
	agentiface "github.com/temporal-community/temporal-agent-harness/nexus/slack_connector/agent"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
)

type mockHandler struct {
	called bool
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
