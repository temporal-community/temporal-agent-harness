package handler

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	"github.com/nexus-rpc/sdk-go/nexus"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/temporal"
)

func TestIsStaleTurn(t *testing.T) {
	cases := []struct {
		name   string
		err    error
		expect bool
	}{
		{"StaleTurn app error", temporal.NewApplicationError("stale", "StaleTurn"), true},
		{"other app error", temporal.NewApplicationError("other", "Timeout"), false},
		{"nil", nil, false},
		{"plain error", fmt.Errorf("generic"), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.expect, isStaleTurn(tc.err))
		})
	}
}

func TestBuildCompletionCallbacks_WithURL(t *testing.T) {
	opts := nexus.StartOperationOptions{
		CallbackURL: "http://example.com/callback",
		CallbackHeader: map[string]string{
			"temporal-callback-token": "tok123",
		},
	}
	cbs := buildCompletionCallbacks(opts)
	require.Len(t, cbs, 1)
	nexusCB := cbs[0].GetNexus()
	require.NotNil(t, nexusCB)
	assert.Equal(t, "http://example.com/callback", nexusCB.Url)
	assert.Equal(t, "tok123", nexusCB.Header["temporal-callback-token"])
}

func TestBuildCompletionCallbacks_EmptyURL(t *testing.T) {
	cbs := buildCompletionCallbacks(nexus.StartOperationOptions{CallbackURL: ""})
	assert.Nil(t, cbs)
}

func TestEncodePollToken_RoundTrip(t *testing.T) {
	tok, err := encodePollToken("wf-123", "upd-456")
	require.NoError(t, err)
	assert.NotEmpty(t, tok)

	b, err := base64.URLEncoding.WithPadding(base64.NoPadding).DecodeString(tok)
	require.NoError(t, err)
	var pt pollToken
	require.NoError(t, json.Unmarshal(b, &pt))
	assert.Equal(t, "wf-123", pt.WorkflowID)
	assert.Equal(t, "upd-456", pt.UpdateID)
}

func TestIsWorkflowCompleted(t *testing.T) {
	assert.True(t, isWorkflowCompleted(fmt.Errorf("workflow execution already completed: foo")))
	assert.False(t, isWorkflowCompleted(fmt.Errorf("some other error")))
}

// -- Task/Message <-> harness-turn translation -------------------------------------------

func TestDecodeHandlerPart(t *testing.T) {
	msg := Message{
		Role: "user",
		Parts: []Part{
			{Kind: "data", Data: `{"handler":"ask","input":{"text":"hi"}}`},
		},
		TaskID: "t1",
	}
	hp, err := decodeHandlerPart(msg)
	require.NoError(t, err)
	assert.Equal(t, "ask", hp.Handler)
	assert.Equal(t, "hi", hp.Input["text"])
}

func TestDecodeHandlerPart_MissingDataPart(t *testing.T) {
	_, err := decodeHandlerPart(Message{Role: "user", TaskID: "t1"})
	assert.ErrorContains(t, err, "data")
}

func TestDecodeHandlerPart_WrongKind(t *testing.T) {
	_, err := decodeHandlerPart(Message{
		Role:   "user",
		Parts:  []Part{{Kind: "text", Text: "hello"}},
		TaskID: "t1",
	})
	assert.ErrorContains(t, err, "data")
}

func TestDecodeHandlerPart_MissingHandlerField(t *testing.T) {
	_, err := decodeHandlerPart(Message{
		Role:   "user",
		Parts:  []Part{{Kind: "data", Data: `{"input":{}}`}},
		TaskID: "t1",
	})
	assert.ErrorContains(t, err, "handler")
}

func TestTaskFromTurn(t *testing.T) {
	task := taskFromTurn("t1", UserInputResult{TurnNumber: 1, TurnID: "turn-abc"}, 42)
	assert.Equal(t, "t1", task.ID)
	assert.Equal(t, "t1", task.ContextID)
	assert.Equal(t, "working", task.Status.State)
	assert.Equal(t, int64(42), task.StreamHeadOffset)
	assert.Equal(t, int64(1), task.TurnNumber)
	require.NotNil(t, task.Status.Message)
	assert.Equal(t, "turn-abc", task.Status.Message.MessageID)
}

func TestTaskFromStatus_ActiveMapsToWorking(t *testing.T) {
	task := taskFromStatus("t1", AgentStatus{TurnActive: true, CurrentTurn: 2})
	assert.Equal(t, "working", task.Status.State)
}

func TestTaskFromStatus_IdleMapsToCompleted(t *testing.T) {
	task := taskFromStatus("t1", AgentStatus{TurnActive: false, CurrentTurn: 2})
	assert.Equal(t, "completed", task.Status.State)
}

// -- Error classification (non-retryable vs. default-retryable) -------------------------

func TestTaskLookupError_NotFoundIsNonRetryable(t *testing.T) {
	err := taskLookupError("bogus-task", serviceerror.NewNotFound("workflow not found"))
	var herr *nexus.HandlerError
	require.ErrorAs(t, err, &herr)
	assert.Equal(t, nexus.HandlerErrorTypeNotFound, herr.Type)
	assert.False(t, herr.Retryable())
}

func TestTaskLookupError_OtherErrorsStayGenericAndRetryable(t *testing.T) {
	err := taskLookupError("t1", fmt.Errorf("transient RPC blip"))
	var herr *nexus.HandlerError
	assert.False(t, errors.As(err, &herr), "should not be classified as NOT_FOUND")
}

func TestSendMessageTurn_BadHandlerPartIsNonRetryableBadRequest(t *testing.T) {
	_, err := sendMessageTurn(context.Background(), nil, Config{}, Message{Role: "user", TaskID: "t1"}, "req-1")
	var herr *nexus.HandlerError
	require.ErrorAs(t, err, &herr)
	assert.Equal(t, nexus.HandlerErrorTypeBadRequest, herr.Type)
	assert.False(t, herr.Retryable())
}
