package agent

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/a2aproject/a2a-go/a2apb"
	"github.com/google/uuid"
	"github.com/nexus-rpc/sdk-go/nexus"
	a2anexus "github.com/temporal-community/temporal-agent-harness/nexus/a2a"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
)

const HarnessControlServiceName = "HarnessControlService"

var (
	queryOperatorOperation   = nexus.NewOperationReference[querySessionInput, queryOperatorOutput]("QueryOperatorInterface")
	queryStatusOperation     = nexus.NewOperationReference[querySessionInput, queryStatusOutput]("QueryAgentStatus")
	executeOperatorOperation = nexus.NewOperationReference[executeOperatorInput, executeOperatorOutput]("ExecuteOperatorCommand")
	approveOperation         = nexus.NewOperationReference[approveInput, approveOutput]("ApproveToolCall")
)

type querySessionInput struct {
	SessionID string `json:"sessionId"`
}

type queryStatusOutput struct {
	CurrentTurn int64 `json:"currentTurn"`
}

type operatorCommand struct {
	Name   string `json:"name"`
	Source string `json:"source"`
}

type queryOperatorOutput struct {
	Commands []operatorCommand `json:"commands"`
}

type executeOperatorInput struct {
	SessionID string  `json:"sessionId"`
	Name      string  `json:"name"`
	Arg       *string `json:"arg,omitempty"`
}

type executeOperatorOutput struct {
	Reply string `json:"reply"`
}

type approveInput struct {
	SessionID string  `json:"sessionId"`
	ToolID    string  `json:"toolId"`
	Approved  bool    `json:"approved"`
	Reason    *string `json:"reason,omitempty"`
	Remember  *bool   `json:"remember,omitempty"`
}

type approveOutput struct {
	ToolID   string `json:"toolId"`
	Accepted bool   `json:"accepted"`
}

// A2ABackend binds the generic tunnel to the standard A2A Nexus service. It does
// not decode StreamResponse records; lossless interpretation belongs to subscribers.
type A2ABackend struct{}

var _ router.AgentBackend = A2ABackend{}

// A2AActions invokes one-shot A2A and harness-control operations directly from a
// platform driver. These calls are standalone Nexus operations, not workflow steps.
type A2AActions struct {
	client   client.Client
	endpoint string
}

var _ router.Actions = (*A2AActions)(nil)

func NewA2AActions(tc client.Client, endpoint string) *A2AActions {
	return &A2AActions{client: tc, endpoint: endpoint}
}

func (a *A2AActions) nexus(service string) (client.NexusClient, error) {
	return a.client.NewNexusClient(client.NexusClientOptions{
		Endpoint: a.endpoint,
		Service:  service,
	})
}

func (a *A2AActions) executeOnce(
	ctx context.Context,
	nexusClient client.NexusClient,
	operation any,
	input any,
	options client.StartNexusOperationOptions,
) (client.NexusOperationHandle, error) {
	options.IDConflictPolicy = enumspb.NEXUS_OPERATION_ID_CONFLICT_POLICY_USE_EXISTING
	options.IDReusePolicy = enumspb.NEXUS_OPERATION_ID_REUSE_POLICY_REJECT_DUPLICATE
	handle, err := nexusClient.ExecuteOperation(ctx, operation, input, options)
	if err == nil {
		return handle, nil
	}
	var alreadyStarted *serviceerror.NexusOperationExecutionAlreadyStarted
	if !errors.As(err, &alreadyStarted) {
		return nil, err
	}
	return a.client.GetNexusOperationHandle(client.GetNexusOperationHandleOptions{
		OperationID: options.ID,
		RunID:       alreadyStarted.RunId,
	}), nil
}

func (a *A2AActions) SendMessage(ctx context.Context, sessionID, operationID string, input router.SendMessageInput) (router.TurnAccepted, error) {
	if input.MessageType == "slash" {
		if reply, handled, err := a.dispatchOperatorCommand(ctx, sessionID, operationID, input); err != nil {
			return router.TurnAccepted{}, err
		} else if handled {
			return router.TurnAccepted{Reply: reply}, nil
		}
	}
	request, err := sendMessageRequest(sessionID, operationID, input)
	if err != nil {
		return router.TurnAccepted{}, err
	}
	nexusClient, err := a.nexus(a2anexus.ServiceName)
	if err != nil {
		return router.TurnAccepted{}, err
	}
	handle, err := a.executeOnce(ctx, nexusClient, a2anexus.SendMessageOperation, request, client.StartNexusOperationOptions{
		ID:                     operationID,
		ScheduleToCloseTimeout: 90 * time.Second,
		Summary:                fmt.Sprintf("send message to agent task %s", sessionID),
	})
	if err != nil {
		return router.TurnAccepted{}, err
	}
	var response a2anexus.SendMessageResponse
	if err := handle.Get(ctx, &response); err != nil {
		return router.TurnAccepted{}, err
	}
	return acceptedTurn(response)
}

func sendMessageRequest(sessionID, messageID string, input router.SendMessageInput) (a2anexus.SendMessageRequest, error) {
	// encoding/json sorts map keys, giving the open-ended driver metadata a stable
	// representation before it enters Temporal history.
	requestMetadataValues := input.Metadata
	if requestMetadataValues == nil {
		requestMetadataValues = map[string]any{}
	}
	encodedRequestMetadata, err := json.Marshal(requestMetadataValues) //workflowcheck:ignore
	if err != nil {
		return a2anexus.SendMessageRequest{}, fmt.Errorf("encode A2A request metadata: %w", err)
	}
	requestMetadata := &structpb.Struct{}
	if err := protojson.Unmarshal(encodedRequestMetadata, requestMetadata); err != nil {
		return a2anexus.SendMessageRequest{}, fmt.Errorf("decode A2A request metadata: %w", err)
	}
	if input.ExpectedTurn > 0 {
		requestMetadata.Fields["expected_turn"] = structpb.NewNumberValue(float64(input.ExpectedTurn))
	}
	// encoding/json sorts map keys, so this serialized harness extension is stable
	// during workflow replay even though Payload is intentionally open-ended.
	encodedPayload, err := json.Marshal(input.Payload) //workflowcheck:ignore
	if err != nil {
		return a2anexus.SendMessageRequest{}, err
	}
	messageMetadata := &structpb.Struct{Fields: map[string]*structpb.Value{
		"temporal.io/message-type": structpb.NewStringValue(input.MessageType),
		"temporal.io/payload":      structpb.NewStringValue(string(encodedPayload)),
	}}
	request := &a2apb.SendMessageRequest{
		Request: &a2apb.Message{
			MessageId: messageID,
			TaskId:    sessionID,
			ContextId: sessionID,
			Role:      a2apb.Role_ROLE_USER,
			Parts:     []*a2apb.Part{{Part: &a2apb.Part_Text{Text: textPayload(input.Payload)}}},
			Metadata:  messageMetadata,
		},
		Metadata: requestMetadata,
	}
	return a2anexus.SendMessageRequest{Value: request}, nil
}

func acceptedTurn(response a2anexus.SendMessageResponse) (router.TurnAccepted, error) {
	if response.Value == nil {
		return router.TurnAccepted{}, fmt.Errorf("A2A SendMessage returned an empty response")
	}
	task := response.Value.GetTask()
	if task == nil || task.Metadata == nil {
		return router.TurnAccepted{}, fmt.Errorf("A2A SendMessage returned no task metadata")
	}
	return router.TurnAccepted{
		TurnNumber:       int64(task.Metadata.Fields["temporal.io/turn-number"].GetNumberValue()),
		TurnID:           task.Metadata.Fields["temporal.io/turn-id"].GetStringValue(),
		StreamHeadOffset: int64(task.Metadata.Fields["temporal.io/accepted-offset"].GetNumberValue()),
		Pending:          task.Metadata.Fields["temporal.io/pending"].GetBoolValue(),
	}, nil
}

func (a *A2AActions) dispatchOperatorCommand(ctx context.Context, sessionID, operationID string, input router.SendMessageInput) (string, bool, error) {
	name, _ := input.Payload["name"].(string)
	arg, _ := input.Payload["arg"].(string)
	nexusClient, err := a.nexus(HarnessControlServiceName)
	if err != nil {
		return "", false, err
	}
	var iface queryOperatorOutput
	queryHandle, err := nexusClient.ExecuteOperation(ctx, queryOperatorOperation,
		querySessionInput{SessionID: sessionID}, client.StartNexusOperationOptions{
			ID: operationID + "-query-interface", ScheduleToCloseTimeout: 30 * time.Second,
		})
	if err != nil {
		return "", false, err
	}
	if err := queryHandle.Get(ctx, &iface); err != nil {
		return "", false, err
	}
	for _, command := range iface.Commands {
		if command.Name != name || command.Source != "harness" {
			continue
		}
		var output executeOperatorOutput
		executeHandle, err := a.executeOnce(ctx, nexusClient, executeOperatorOperation,
			executeOperatorInput{SessionID: sessionID, Name: name, Arg: &arg},
			client.StartNexusOperationOptions{
				ID: operationID, ScheduleToCloseTimeout: 30 * time.Second,
			})
		if err != nil {
			return "", true, err
		}
		if err := executeHandle.Get(ctx, &output); err != nil {
			return "", true, err
		}
		return output.Reply, true, nil
	}
	return "", false, nil
}

func (a *A2AActions) Control(ctx context.Context, sessionID, operationID string, input router.ControlInput) (router.ControlOutput, error) {
	switch input.Kind {
	case "approve-tool-call":
		request, err := decodeApproval(input.Payload)
		if err != nil {
			return router.ControlOutput{}, fmt.Errorf("invalid approval payload: %w", err)
		}
		request.SessionID = sessionID
		nexusClient, err := a.nexus(HarnessControlServiceName)
		if err != nil {
			return router.ControlOutput{}, err
		}
		handle, err := a.executeOnce(ctx, nexusClient, approveOperation, request, client.StartNexusOperationOptions{
			ID:                     operationID,
			ScheduleToCloseTimeout: 30 * time.Second,
		})
		if err != nil {
			return router.ControlOutput{}, err
		}
		var output approveOutput
		if err := handle.Get(ctx, &output); err != nil {
			return router.ControlOutput{}, err
		}
		encoded, err := json.Marshal(output)
		return router.ControlOutput{Accepted: output.Accepted, Payload: encoded}, err
	default:
		return router.ControlOutput{}, fmt.Errorf("unknown control kind %q", input.Kind)
	}
}

func (a *A2AActions) Exists(ctx context.Context, sessionID string) bool {
	nexusClient, err := a.nexus(HarnessControlServiceName)
	if err != nil {
		return false
	}
	handle, err := nexusClient.ExecuteOperation(ctx, queryStatusOperation,
		querySessionInput{SessionID: sessionID}, client.StartNexusOperationOptions{
			ID:                     "connector-status-" + uuid.NewString(),
			ScheduleToCloseTimeout: 15 * time.Second,
		})
	if err != nil {
		return false
	}
	var output queryStatusOutput
	return handle.Get(ctx, &output) == nil
}

func (A2ABackend) Poll(ctx workflow.Context, tunnel router.TunnelInput, cursor int64, timeoutSeconds float64) (router.StreamPage, error) {
	client := workflow.NewNexusClient(tunnel.NexusEndpoint, a2anexus.ServiceName)
	var output a2anexus.SubscribeToTaskOutput
	err := client.ExecuteOperation(ctx, a2anexus.SubscribeToTaskOperation, a2anexus.SubscribeToTaskInput{
		ID:             tunnel.SessionID,
		Cursor:         cursor,
		TimeoutSeconds: timeoutSeconds,
	}, workflow.NexusOperationOptions{ScheduleToCloseTimeout: 2 * time.Minute}).Get(ctx, &output)
	if err != nil {
		return router.StreamPage{}, err
	}
	items := make([]router.StreamItem, len(output.Items))
	for i, item := range output.Items {
		items[i] = router.StreamItem{Offset: item.Offset, Data: item.Data}
	}
	turnComplete := completesTurn(items, tunnel.TurnNumber)
	if !turnComplete && len(output.Items) == 0 && !output.MoreReady {
		var task a2anexus.TaskSnapshot
		err = client.ExecuteOperation(ctx, a2anexus.GetTaskOperation,
			a2anexus.GetTaskInput{ID: tunnel.SessionID},
			workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second},
		).Get(ctx, &task)
		if err != nil {
			return router.StreamPage{}, err
		}
		turnComplete = taskIsPastTurn(task, tunnel.TurnNumber)
	}
	return router.StreamPage{
		Items:        items,
		NextCursor:   output.NextCursor,
		MoreReady:    output.MoreReady,
		Closed:       output.Closed,
		TurnComplete: turnComplete,
	}, nil
}

func taskIsPastTurn(task a2anexus.TaskSnapshot, turnNumber int64) bool {
	if task.Status == nil {
		return false
	}
	switch task.Status.State {
	case "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELLED", "TASK_STATE_REJECTED":
		return true
	}
	if task.Status.State != "TASK_STATE_INPUT_REQUIRED" {
		return false
	}
	current, _ := task.Metadata["temporal.io/current-turn"].(float64)
	return int64(current) >= turnNumber
}

// JSON decoding into a concrete struct is deterministic; workflowcheck cannot
// currently distinguish it from decoding into a map.
//
//workflowcheck:ignore
func decodeApproval(payload json.RawMessage) (approveInput, error) {
	var request approveInput
	err := json.Unmarshal(payload, &request)
	return request, err
}

func textPayload(payload map[string]any) string {
	if text, ok := payload["text"].(string); ok {
		return text
	}
	encoded, _ := json.Marshal(payload) //workflowcheck:ignore -- map keys are sorted
	return string(encoded)
}

// DecodeStreamItem unwraps the harness extension from an otherwise standard A2A
// StreamResponse. The raw record remains available to every driver; this helper is an
// optional rendering adapter for drivers that understand harness-native events.
func DecodeStreamItem(item router.StreamItem) (*router.Delta, error) {
	response, err := decodeStreamResponse(item)
	if err != nil {
		return nil, err
	}
	var metadata *structpb.Struct
	switch body := response.Payload.(type) {
	case *a2apb.StreamResponse_Task:
		metadata = body.Task.GetMetadata()
	case *a2apb.StreamResponse_Msg:
		metadata = body.Msg.GetMetadata()
	case *a2apb.StreamResponse_StatusUpdate:
		metadata = body.StatusUpdate.GetMetadata()
	case *a2apb.StreamResponse_ArtifactUpdate:
		metadata = body.ArtifactUpdate.GetMetadata()
	}
	if metadata == nil {
		return standardA2ADelta(response), nil
	}
	encoded, ok := metadata.AsMap()["temporal.io/agent-event-payload"].(string)
	if !ok || encoded == "" {
		return standardA2ADelta(response), nil
	}
	_, event, err := decodeEncodedTurnEvent(encoded)
	if err != nil {
		return nil, err
	}
	return turnEventToDelta(*event), nil
}

func decodeStreamResponse(item router.StreamItem) (*a2apb.StreamResponse, error) {
	raw, err := base64.StdEncoding.DecodeString(item.Data)
	if err != nil {
		return nil, fmt.Errorf("decode A2A StreamResponse: %w", err)
	}
	var response a2apb.StreamResponse
	if err := proto.Unmarshal(raw, &response); err != nil {
		return nil, fmt.Errorf("unmarshal A2A StreamResponse: %w", err)
	}
	return &response, nil
}

func completesTurn(items []router.StreamItem, turnNumber int64) bool {
	for _, item := range items {
		response, err := decodeStreamResponse(item)
		if err != nil {
			continue
		}
		if status, ok := response.Payload.(*a2apb.StreamResponse_StatusUpdate); ok && status.StatusUpdate.GetFinal() {
			return true
		}
		if message, ok := response.Payload.(*a2apb.StreamResponse_Msg); ok && message.Msg.GetRole() == a2apb.Role_ROLE_AGENT {
			return true
		}
		var metadata *structpb.Struct
		switch body := response.Payload.(type) {
		case *a2apb.StreamResponse_Task:
			metadata = body.Task.GetMetadata()
		case *a2apb.StreamResponse_Msg:
			metadata = body.Msg.GetMetadata()
		case *a2apb.StreamResponse_StatusUpdate:
			metadata = body.StatusUpdate.GetMetadata()
		case *a2apb.StreamResponse_ArtifactUpdate:
			metadata = body.ArtifactUpdate.GetMetadata()
		}
		if metadata == nil {
			continue
		}
		encoded, _ := metadata.AsMap()["temporal.io/agent-event-payload"].(string)
		if encoded == "" {
			continue
		}
		streamTurn, event, err := decodeEncodedTurnEvent(encoded)
		if err != nil || int64(streamTurn) != turnNumber {
			continue
		}
		switch event.Type {
		case "turn_end", "operator_command_completed", "operator_command_failed":
			return true
		}
	}
	return false
}

func standardA2ADelta(response *a2apb.StreamResponse) *router.Delta {
	switch body := response.Payload.(type) {
	case *a2apb.StreamResponse_Msg:
		return &router.Delta{Text: messageText(body.Msg), IsFinal: true}
	case *a2apb.StreamResponse_ArtifactUpdate:
		return &router.Delta{Text: partsText(body.ArtifactUpdate.GetArtifact().GetParts()), IsFinal: body.ArtifactUpdate.GetLastChunk()}
	case *a2apb.StreamResponse_StatusUpdate:
		status := body.StatusUpdate.GetStatus()
		delta := &router.Delta{Text: messageText(status.GetUpdate()), IsFinal: body.StatusUpdate.GetFinal()}
		if status.GetState() == a2apb.TaskState_TASK_STATE_FAILED && delta.Text == "" {
			delta.Text = "[error] A2A task failed"
		}
		return delta
	}
	return nil
}

func messageText(message *a2apb.Message) string {
	if message == nil {
		return ""
	}
	return partsText(message.GetParts())
}

func partsText(parts []*a2apb.Part) string {
	var text string
	for _, part := range parts {
		text += part.GetText()
	}
	return text
}
