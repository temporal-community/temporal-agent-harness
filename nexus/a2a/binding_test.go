package a2anexus

import (
	"encoding/json"
	"testing"

	"github.com/a2aproject/a2a-go/a2apb"
)

func TestSendMessageRoundTripsThroughA2AJSON(t *testing.T) {
	request := SendMessageRequest{Value: &a2apb.SendMessageRequest{Request: &a2apb.Message{
		MessageId: "message-1",
		TaskId:    "task-1",
	}}}
	payload, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}

	var decoded SendMessageRequest
	if err := json.Unmarshal(payload, &decoded); err != nil {
		t.Fatal(err)
	}
	if got := decoded.Value.GetRequest().GetMessageId(); got != "message-1" {
		t.Fatalf("message ID = %q, want message-1", got)
	}
	if got := decoded.Value.GetRequest().GetTaskId(); got != "task-1" {
		t.Fatalf("task ID = %q, want task-1", got)
	}
}

func TestGetTaskUsesCrossSDKIDField(t *testing.T) {
	payload, err := json.Marshal(GetTaskInput{ID: "task-1"})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(payload), `{"id":"task-1"}`; got != want {
		t.Fatalf("GetTask JSON = %s, want %s", got, want)
	}
}
