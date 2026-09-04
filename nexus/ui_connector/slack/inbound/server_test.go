package slackinbound

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
)

const testBotUserID = "BOT123"

type tunnelCall struct {
	sessionID string
	updateID  string
	input     router.SendAndMountInput
}

type fakeTunnel struct {
	exists   bool
	calls    []tunnelCall
	controls []router.ControlInput
	accepted router.TurnAccepted
}

func (f *fakeTunnel) SendAndMount(_ context.Context, sessionID, updateID string, input router.SendAndMountInput) (router.TurnAccepted, error) {
	f.calls = append(f.calls, tunnelCall{sessionID: sessionID, updateID: updateID, input: input})
	return f.accepted, nil
}

func (f *fakeTunnel) Control(_ context.Context, _, _ string, input router.ControlInput) (router.ControlOutput, error) {
	f.controls = append(f.controls, input)
	return router.ControlOutput{Accepted: true}, nil
}

func (f *fakeTunnel) Exists(context.Context, string) bool { return f.exists }

func postEvent(t *testing.T, s *webhookServer, body string) {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, routeEvents, strings.NewReader(body))
	response := httptest.NewRecorder()
	s.handleEvents(response, request)
	require.Equal(t, http.StatusOK, response.Code)
}

func postSlashCommand(t *testing.T, s *webhookServer, form url.Values) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, routeCommands, strings.NewReader(form.Encode()))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	response := httptest.NewRecorder()
	s.handleSlashCommands(response, request)
	require.Equal(t, http.StatusOK, response.Code)
	return response
}

func newTestServer(tunnel router.Client, prefix string, allowed []string) *webhookServer {
	return NewServer(tunnel, "driver-queue", "", testBotUserID, prefix, allowed)
}

func TestHandleEventsMountsMentionOnSharedTunnel(t *testing.T) {
	for _, test := range []struct{ name, ts, thread string }{
		{"top-level", "100.000", ""},
		{"thread-reply", "200.000", "100.000"},
	} {
		t.Run(test.name, func(t *testing.T) {
			tunnel := &fakeTunnel{}
			postEvent(t, newTestServer(tunnel, "", nil), `{
				"type":"event_callback",
				"event":{"type":"message","channel":"C1","user":"U1","ts":"`+test.ts+`","thread_ts":"`+test.thread+`","text":"<@BOT123> hi"}
			}`)
			require.Len(t, tunnel.calls, 1)
			call := tunnel.calls[0]
			require.Equal(t, "ask", call.input.Message.MessageType)
			require.Equal(t, "SlackDeliverA2A", call.input.Subscriber.Delivery.Activity)
			require.Equal(t, "driver-queue", call.input.Subscriber.Delivery.TaskQueue)
		})
	}
}

func TestUnmentionedThreadReplyRequiresExistingTunnel(t *testing.T) {
	for _, exists := range []bool{false, true} {
		tunnel := &fakeTunnel{exists: exists}
		postEvent(t, newTestServer(tunnel, "", nil), `{
			"type":"event_callback",
			"event":{"type":"message","channel":"C1","user":"U1","ts":"200.000","thread_ts":"100.000","text":"just a reply"}
		}`)
		if exists {
			require.Len(t, tunnel.calls, 1)
		} else {
			require.Empty(t, tunnel.calls)
		}
	}
}

func TestBotAllowlist(t *testing.T) {
	allowed := &fakeTunnel{}
	postEvent(t, newTestServer(allowed, "", []string{"HCBOT"}), `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","bot_id":"HCBOT","ts":"100.000","text":"<@BOT123> hi"}
	}`)
	require.Len(t, allowed.calls, 1)

	other := &fakeTunnel{}
	postEvent(t, newTestServer(other, "", []string{"HCBOT"}), `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","bot_id":"OTHER","ts":"100.000","text":"<@BOT123> hi"}
	}`)
	require.Empty(t, other.calls)
}

func TestSlashCommandUsesA2AAndReturnsSynchronousHarnessReply(t *testing.T) {
	tunnel := &fakeTunnel{accepted: router.TurnAccepted{Reply: "scope: docs"}}
	response := postSlashCommand(t, newTestServer(tunnel, "bot", nil), url.Values{
		"command": {"/bot-scope"}, "channel_id": {"C1"}, "trigger_id": {"T1"}, "text": {"docs"},
	})
	require.Len(t, tunnel.calls, 1)
	require.Equal(t, "slash", tunnel.calls[0].input.Message.MessageType)
	require.Equal(t, "scope", tunnel.calls[0].input.Message.Payload["name"])
	var body map[string]string
	require.NoError(t, json.Unmarshal(response.Body.Bytes(), &body))
	require.Equal(t, "scope: docs", body["text"])
}
