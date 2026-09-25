package agent

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// The harness ends a turn with turn_end since #137. It no longer emits reply.
func TestTurnEventToDelta_CurrentProtocolTerminalEvents(t *testing.T) {
	end := turnEventToDelta(turnEvent{Type: "turn_end"})
	if assert.NotNil(t, end) {
		assert.True(t, end.IsFinal, "turn_end must end the stream")
		assert.Empty(t, end.Text)
	}

	msgErr := turnEventToDelta(turnEvent{Type: "message_handler_error", Message: "boom"})
	if assert.NotNil(t, msgErr) {
		assert.False(t, msgErr.IsFinal, "message_handler_error ends one message, not the turn")
		assert.Equal(t, "[error] boom", msgErr.Text)
	}

	for _, ignored := range []string{"message_handler_end", "turn_started", "message_accepted"} {
		assert.Nil(t, turnEventToDelta(turnEvent{Type: ignored}), ignored)
	}
}
