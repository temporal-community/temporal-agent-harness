// Hand-written mirror of subagent.nexusrpc.yaml (no generator yet — keep in sync manually).
package handler

import "github.com/nexus-rpc/sdk-go/nexus"

// SubagentService fronts one harness agent as an A2A-shaped subagent. See subagent.nexusrpc.yaml.
var SubagentService = struct {
	ServiceName     string
	SendMessage     nexus.OperationReference[SendMessageInput, Task]
	GetTask         nexus.OperationReference[GetTaskInput, Task]
	PollTaskUpdates nexus.OperationReference[PollTaskUpdatesInput, PollTaskUpdatesOutput]
	CancelTask      nexus.OperationReference[CancelTaskInput, Task]
}{
	ServiceName:     "SubagentService",
	SendMessage:     nexus.NewOperationReference[SendMessageInput, Task]("sendMessage"),
	GetTask:         nexus.NewOperationReference[GetTaskInput, Task]("getTask"),
	PollTaskUpdates: nexus.NewOperationReference[PollTaskUpdatesInput, PollTaskUpdatesOutput]("pollTaskUpdates"),
	CancelTask:      nexus.NewOperationReference[CancelTaskInput, Task]("cancelTask"),
}

// Part is one piece of a Message or Artifact. A "data" part's Data field JSON-encodes
// {handler, input} — our own dispatch convention, not standard A2A.
type Part struct {
	Kind string `json:"kind"`
	Text string `json:"text,omitempty"`
	Data string `json:"data,omitempty"`
}

// Message: parts[0] is always a "data" part carrying {handler, input}.
type Message struct {
	Role      string `json:"role"`
	Parts     []Part `json:"parts"`
	TaskID    string `json:"taskId"`
	MessageID string `json:"messageId,omitempty"`
}

type TaskStatus struct {
	State   string   `json:"state"`
	Message *Message `json:"message,omitempty"`
}

type Artifact struct {
	Name  string `json:"name"`
	Parts []Part `json:"parts"`
}

type Task struct {
	ID               string     `json:"id"`
	ContextID        string     `json:"contextId"`
	Status           TaskStatus `json:"status"`
	Artifacts        []Artifact `json:"artifacts,omitempty"`
	StreamHeadOffset int64      `json:"streamHeadOffset,omitempty"`
	// TurnNumber: harness extension, not standard A2A. Needed by NexusTransport/FIFO gate.
	// Only set on sendMessage's response.
	TurnNumber int64 `json:"turnNumber,omitempty"`
}

type SendMessageInput struct {
	Message Message `json:"message"`
}

type GetTaskInput struct {
	TaskID string `json:"taskId"`
}

type PollTaskUpdatesInput struct {
	TaskID         string  `json:"taskId"`
	Cursor         int64   `json:"cursor"`
	TimeoutSeconds float64 `json:"timeoutSeconds,omitempty"`
}

// PollTaskUpdatesOutput mirrors WorkflowStream's PollResult wire format directly (no TaskState
// field — the caller derives task state from the decoded items).
type PollTaskUpdatesOutput struct {
	Items      []ItemElement `json:"items"`
	NextOffset int64         `json:"next_offset"`
	MoreReady  bool          `json:"more_ready"`
	Closed     bool          `json:"closed,omitempty"`
}

// ItemElement: one WorkflowStream._log event. Data is base64(proto Payload{...AgentEvent JSON}).
type ItemElement struct {
	Topic  string `json:"topic"`
	Data   string `json:"data"`
	Offset int64  `json:"offset"`
}

type CancelTaskInput struct {
	TaskID string `json:"taskId"`
}
