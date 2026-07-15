package webhook

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging/teams"
)

func TestMessageWorkflowInputPropagatesConversationType(t *testing.T) {
	for _, conversationType := range []string{"personal", "channel", "groupChat"} {
		t.Run(conversationType, func(t *testing.T) {
			var activity teams.TeamMessageActivity
			require.NoError(t, json.Unmarshal([]byte(`{
				"type":"message",
				"id":"message-1",
				"text":"question",
				"from":{"id":"user-1"},
				"conversation":{"id":"conversation-1","conversationType":"`+conversationType+`"}
			}`), &activity))

			workflowID, input := messageWorkflowInput(activity)

			assert.Equal(t, "connector-default-teams:conversation-1-message-1", workflowID)
			require.NotNil(t, input.Message)
			assert.Equal(t, conversationType, input.Message.ConversationType)
		})
	}
}
