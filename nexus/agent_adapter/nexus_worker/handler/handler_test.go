package handler

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"testing"

	"github.com/nexus-rpc/sdk-go/nexus"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
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

func TestBuildCompletionCallbacks_HeaderCopied(t *testing.T) {
	// Mutating the returned header must not affect the original opts.
	original := map[string]string{"k": "v"}
	cbs := buildCompletionCallbacks(nexus.StartOperationOptions{
		CallbackURL:    "http://x.com/cb",
		CallbackHeader: original,
	})
	require.Len(t, cbs, 1)
	cbs[0].GetNexus().Header["k"] = "mutated"
	assert.Equal(t, "v", original["k"])
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
