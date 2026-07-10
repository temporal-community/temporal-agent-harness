package handler

import (
	"fmt"
	"testing"

	"github.com/stretchr/testify/assert"
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

func TestIsWorkflowCompleted(t *testing.T) {
	assert.True(t, isWorkflowCompleted(fmt.Errorf("workflow execution already completed: foo")))
	assert.False(t, isWorkflowCompleted(fmt.Errorf("some other error")))
}
