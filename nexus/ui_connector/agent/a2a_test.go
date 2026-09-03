package agent

import (
	"encoding/base64"
	"testing"

	"github.com/a2aproject/a2a-go/a2apb"
	"github.com/stretchr/testify/require"
	a2anexus "github.com/temporal-community/temporal-agent-harness/nexus/a2a"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
)

func TestDecodeStreamItemRendersStandardA2AWithoutHarnessExtension(t *testing.T) {
	response := &a2apb.StreamResponse{Payload: &a2apb.StreamResponse_ArtifactUpdate{
		ArtifactUpdate: &a2apb.TaskArtifactUpdateEvent{
			Artifact:  &a2apb.Artifact{Parts: []*a2apb.Part{{Part: &a2apb.Part_Text{Text: "hello"}}}},
			LastChunk: true,
		},
	}}
	raw, err := proto.Marshal(response)
	require.NoError(t, err)
	delta, err := DecodeStreamItem(router.StreamItem{Data: base64.StdEncoding.EncodeToString(raw)})
	require.NoError(t, err)
	require.Equal(t, "hello", delta.Text)
	require.True(t, delta.IsFinal)
}

func TestStandardAgentMessageCompletesBoundedTurn(t *testing.T) {
	response := &a2apb.StreamResponse{Payload: &a2apb.StreamResponse_Msg{Msg: &a2apb.Message{
		Role: a2apb.Role_ROLE_AGENT,
	}}}
	raw, err := proto.Marshal(response)
	require.NoError(t, err)
	require.True(t, completesTurn([]router.StreamItem{{
		Data: base64.StdEncoding.EncodeToString(raw),
	}}, 1))
}

func TestA2ATaskStatusCompletesOnlyTheRequestedTurn(t *testing.T) {
	metadata, err := structpb.NewStruct(map[string]any{"temporal.io/current-turn": float64(1)})
	require.NoError(t, err)
	require.False(t, taskIsPastTurn(&a2apb.Task{
		Status: &a2apb.TaskStatus{State: a2apb.TaskState_TASK_STATE_WORKING},
	}, 1))
	require.False(t, taskIsPastTurn(&a2apb.Task{
		Status:   &a2apb.TaskStatus{State: a2apb.TaskState_TASK_STATE_INPUT_REQUIRED},
		Metadata: metadata,
	}, 2))
	require.True(t, taskIsPastTurn(&a2apb.Task{
		Status:   &a2apb.TaskStatus{State: a2apb.TaskState_TASK_STATE_INPUT_REQUIRED},
		Metadata: metadata,
	}, 1))
}

func TestTaskReplayReadsLosslessGetTaskExtensionFromCursor(t *testing.T) {
	metadata, err := structpb.NewStruct(map[string]any{
		a2anexus.TaskReplayMetadataKey: map[string]any{
			"items": []any{
				map[string]any{"offset": float64(4), "data": "old"},
				map[string]any{"offset": float64(5), "data": "next"},
			},
			"next_cursor": float64(6),
		},
	})
	require.NoError(t, err)

	items, cursor, err := taskReplay(&a2apb.Task{Metadata: metadata}, 5)
	require.NoError(t, err)
	require.Equal(t, []router.StreamItem{{Offset: 5, Data: "next"}}, items)
	require.EqualValues(t, 6, cursor)
}
