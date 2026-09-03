// Package a2anexus defines the harness-independent A2A protocol binding for
// Temporal Nexus.
package a2anexus

import (
	"github.com/a2aproject/a2a-go/a2apb"
	"github.com/nexus-rpc/sdk-go/nexus"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const (
	ServiceName     = "A2AService"
	ProtocolBinding = "TEMPORAL_NEXUS"
	ProtocolVersion = "1.0"
)

var (
	SendMessageOperation          = nexus.NewOperationReference[SendMessageRequest, SendMessageResponse]("SendMessage")
	GetTaskOperation              = nexus.NewOperationReference[GetTaskInput, TaskSnapshot]("GetTask")
	ListTasksOperation            = nexus.NewOperationReference[ListTasksRequest, ListTasksResponse]("ListTasks")
	CancelTaskOperation           = nexus.NewOperationReference[CancelTaskRequest, TaskResponse]("CancelTask")
	SubscribeToTaskOperation      = nexus.NewOperationReference[SubscribeToTaskInput, SubscribeToTaskOutput]("SubscribeToTask")
	GetExtendedAgentCardOperation = nexus.NewOperationReference[GetExtendedAgentCardRequest, AgentCardResponse]("GetExtendedAgentCard")
)

func marshal(value proto.Message) ([]byte, error) { return protojson.Marshal(value) }

func unmarshal(data []byte, value proto.Message) error {
	return protojson.Unmarshal(data, value)
}

// Named wrappers force Temporal's JSON converter to use A2A's package-independent
// JSON representation rather than protobuf descriptors whose package names differ
// between the Go and Python A2A SDKs.
type SendMessageRequest struct{ Value *a2apb.SendMessageRequest }
type SendMessageResponse struct{ Value *a2apb.SendMessageResponse }
type TaskResponse struct{ Value *a2apb.Task }
type ListTasksRequest struct{ Value *a2apb.ListTasksRequest }
type ListTasksResponse struct{ Value *a2apb.ListTasksResponse }
type CancelTaskRequest struct{ Value *a2apb.CancelTaskRequest }
type GetExtendedAgentCardRequest struct {
	Value *a2apb.GetAgentCardRequest
}
type AgentCardResponse struct{ Value *a2apb.AgentCard }

func (value SendMessageRequest) MarshalJSON() ([]byte, error) { return marshal(value.Value) }
func (value *SendMessageRequest) UnmarshalJSON(data []byte) error {
	value.Value = &a2apb.SendMessageRequest{}
	return unmarshal(data, value.Value)
}
func (value SendMessageResponse) MarshalJSON() ([]byte, error) { return marshal(value.Value) }
func (value *SendMessageResponse) UnmarshalJSON(data []byte) error {
	value.Value = &a2apb.SendMessageResponse{}
	return unmarshal(data, value.Value)
}
func (value TaskResponse) MarshalJSON() ([]byte, error) { return marshal(value.Value) }
func (value *TaskResponse) UnmarshalJSON(data []byte) error {
	value.Value = &a2apb.Task{}
	return unmarshal(data, value.Value)
}
func (value ListTasksRequest) MarshalJSON() ([]byte, error) { return marshal(value.Value) }
func (value *ListTasksRequest) UnmarshalJSON(data []byte) error {
	value.Value = &a2apb.ListTasksRequest{}
	return unmarshal(data, value.Value)
}
func (value ListTasksResponse) MarshalJSON() ([]byte, error) { return marshal(value.Value) }
func (value *ListTasksResponse) UnmarshalJSON(data []byte) error {
	value.Value = &a2apb.ListTasksResponse{}
	return unmarshal(data, value.Value)
}
func (value CancelTaskRequest) MarshalJSON() ([]byte, error) { return marshal(value.Value) }
func (value *CancelTaskRequest) UnmarshalJSON(data []byte) error {
	value.Value = &a2apb.CancelTaskRequest{}
	return unmarshal(data, value.Value)
}
func (value GetExtendedAgentCardRequest) MarshalJSON() ([]byte, error) {
	return marshal(value.Value)
}
func (value *GetExtendedAgentCardRequest) UnmarshalJSON(data []byte) error {
	value.Value = &a2apb.GetAgentCardRequest{}
	return unmarshal(data, value.Value)
}
func (value AgentCardResponse) MarshalJSON() ([]byte, error) { return marshal(value.Value) }
func (value *AgentCardResponse) UnmarshalJSON(data []byte) error {
	value.Value = &a2apb.AgentCard{}
	return unmarshal(data, value.Value)
}

type SubscribeToTaskInput struct {
	Tenant         string  `json:"tenant,omitempty"`
	ID             string  `json:"id"`
	Cursor         int64   `json:"cursor"`
	TimeoutSeconds float64 `json:"timeout_seconds"`
}

// GetTaskInput and TaskSnapshot retain the package-independent A2A JSON fields
// shared by the current Go and Python SDKs. Their generated protobuf request types
// come from different A2A revisions (name versus id), so using either language's
// descriptor directly would make the Nexus boundary language-specific.
type GetTaskInput struct {
	Tenant string `json:"tenant,omitempty"`
	ID     string `json:"id"`
}

type TaskStatus struct {
	State string `json:"state"`
}

type TaskSnapshot struct {
	Status   *TaskStatus    `json:"status"`
	Metadata map[string]any `json:"metadata"`
}

type StreamItem struct {
	Offset int64  `json:"offset"`
	Data   string `json:"data"`
}

type SubscribeToTaskOutput struct {
	Items      []StreamItem `json:"items"`
	NextCursor int64        `json:"next_cursor"`
	MoreReady  bool         `json:"more_ready"`
	Closed     bool         `json:"closed"`
}
