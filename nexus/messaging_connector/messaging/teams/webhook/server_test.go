package webhook

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestMessageWorkflowInputPropagatesConversationType(t *testing.T) {
	for _, conversationType := range []string{"personal", "channel", "groupChat"} {
		t.Run(conversationType, func(t *testing.T) {
			var activity teamMessageActivity
			require.NoError(t, json.Unmarshal([]byte(`{
				"type":"message",
				"id":"message-1",
				"text":"question",
				"serviceUrl":"https://example.test/teams/",
				"channelId":"msteams",
				"from":{"id":"user-1"},
				"conversation":{"id":"conversation-1","conversationType":"`+conversationType+`"}
			}`), &activity))

			workflowID, input := messageWorkflowInput(activity)

			assert.Equal(t, "connector-default-teams:conversation-1-message-1", workflowID)
			require.NotNil(t, input.Message)
			assert.Equal(t, conversationType, input.Message.ConversationType)
			assert.Equal(t, "https://example.test/teams/", input.Message.ServiceURL)
			assert.Equal(t, "msteams", input.Message.ChannelID)
		})
	}
}

func TestApprovalWorkflowInputPropagatesCardRouting(t *testing.T) {
	activity := teamMessageActivity{
		ReplyToID:  "card-1",
		ServiceURL: "https://example.test/teams/",
		ChannelID:  "msteams",
	}
	value := approvalButtonValue{
		SessionID: "teams:conversation-1",
		ToolID:    "tool-1",
		ToolName:  "deploy",
		Approved:  true,
	}

	workflowID, input := approvalWorkflowInput(activity, value)

	assert.Equal(t, "connector-default-teams:conversation-1-approval-tool-1", workflowID)
	require.NotNil(t, input.Approval)
	assert.Equal(t, "card-1", input.Approval.ActivityID)
	assert.Equal(t, "https://example.test/teams/", input.Approval.ServiceURL)
	assert.Equal(t, "msteams", input.Approval.ChannelID)
}
