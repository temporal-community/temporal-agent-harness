package teamsinbound

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
)

type fakeTunnel struct {
	sessionID string
	input     router.SendAndMountInput
	control   router.ControlInput
}

func (f *fakeTunnel) SendAndMount(_ context.Context, sessionID, _ string, input router.SendAndMountInput) (router.TurnAccepted, error) {
	f.sessionID, f.input = sessionID, input
	return router.TurnAccepted{}, nil
}

func (f *fakeTunnel) Control(_ context.Context, sessionID, _ string, input router.ControlInput) (router.ControlOutput, error) {
	f.sessionID, f.control = sessionID, input
	return router.ControlOutput{Accepted: true}, nil
}

func (*fakeTunnel) Exists(context.Context, string) bool { return false }

func post(t *testing.T, tunnel *fakeTunnel, body string) *httptest.ResponseRecorder {
	t.Helper()
	server := NewServer(tunnel, "teams-driver-queue")
	request := httptest.NewRequest(http.MethodPost, routeMessages, strings.NewReader(body))
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)
	return response
}

func TestHandleMessagesMountsA2ATunnel(t *testing.T) {
	tunnel := &fakeTunnel{}
	response := post(t, tunnel, `{
		"type":"message", "id":"message-1", "text":"question",
		"serviceUrl":"https://example.test/teams/", "channelId":"msteams",
		"from":{"id":"user-1"},
		"conversation":{"id":"conversation-1","conversationType":"personal"}
	}`)
	require.Equal(t, http.StatusOK, response.Code)
	require.Equal(t, "teams:conversation-1", tunnel.sessionID)
	require.Equal(t, "ask", tunnel.input.Message.MessageType)
	require.Equal(t, "question", tunnel.input.Message.Payload["text"])
	require.Equal(t, "TeamsDeliverA2A", tunnel.input.Subscriber.Delivery.Activity)
	require.Equal(t, "teams-driver-queue", tunnel.input.Subscriber.Delivery.TaskQueue)
}

func TestHandleMessagesResolvesApprovalThroughTunnel(t *testing.T) {
	tunnel := &fakeTunnel{}
	response := post(t, tunnel, `{
		"type":"message", "replyToId":"card-1",
		"value":{"s":"teams:conversation-1","t":"tool-1","n":"deploy","a":true}
	}`)
	require.Equal(t, http.StatusOK, response.Code)
	require.Equal(t, "approve-tool-call", tunnel.control.Kind)
	require.Equal(t, "TeamsAcknowledgeApproval", tunnel.control.Delivery.Activity)
}

func TestHandleMessagesRejectsInvalidOrdinaryMessage(t *testing.T) {
	for _, body := range []string{
		`{"type":"message","id":"message-1","from":{"id":"user-1"},"conversation":{"id":"conversation-1"}}`,
		`{"type":"message","id":"message-1","text":"question","from":{"id":"user-1"}}`,
	} {
		tunnel := &fakeTunnel{}
		response := post(t, tunnel, body)
		require.Equal(t, http.StatusBadRequest, response.Code)
		require.Empty(t, tunnel.sessionID)
	}
}
