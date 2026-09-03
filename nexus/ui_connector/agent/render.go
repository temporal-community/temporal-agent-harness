// Package agent binds the connector tunnel to A2A over Nexus and provides an
// optional harness-extension decoder for edge drivers.
package agent

import (
	"encoding/base64"
	"encoding/json"
	"fmt"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	commonpb "go.temporal.io/api/common/v1"
	"google.golang.org/protobuf/proto"
)

type streamItem struct {
	TurnID     string    `json:"turn_id"`
	TurnNumber int       `json:"turn_number"`
	Timestamp  float64   `json:"timestamp"`
	Event      turnEvent `json:"event"`
}

type turnEvent struct {
	Type       string         `json:"type"`
	Text       string         `json:"text"`
	ToolID     string         `json:"tool_id"`
	ToolName   string         `json:"tool_name"`
	ToolInput  map[string]any `json:"tool_input"`
	ToolOutput string         `json:"tool_output"`
	Message    string         `json:"message"`
	Delta      map[string]any `json:"delta"`
}

func decodeEncodedTurnEvent(data string) (int, *turnEvent, error) {
	b, err := base64.StdEncoding.DecodeString(data)
	if err != nil {
		b, err = base64.URLEncoding.DecodeString(data)
		if err != nil {
			return 0, nil, fmt.Errorf("base64: %w", err)
		}
	}
	var payload commonpb.Payload
	if err := proto.Unmarshal(b, &payload); err != nil {
		return 0, nil, fmt.Errorf("unmarshal Payload: %w", err)
	}
	var item streamItem
	if err := json.Unmarshal(payload.Data, &item); err != nil {
		return 0, nil, fmt.Errorf("unmarshal stream item: %w", err)
	}
	return item.TurnNumber, &item.Event, nil
}

func turnEventToDelta(event turnEvent) *router.Delta {
	switch event.Type {
	case "reply_delta":
		return &router.Delta{Text: event.Text}
	case "thought_summary":
		if text, ok := event.Delta["text"].(string); ok && text != "" {
			return &router.Delta{ThoughtSummary: text}
		}
	case "tool_start":
		return &router.Delta{ToolStatus: &router.ToolStatus{ToolID: event.ToolID, ToolName: event.ToolName, Status: router.ToolStarted}}
	case "tool_end":
		return &router.Delta{ToolStatus: &router.ToolStatus{ToolID: event.ToolID, ToolName: event.ToolName, Status: router.ToolCompleted}}
	case "tool_error":
		return &router.Delta{ToolStatus: &router.ToolStatus{ToolID: event.ToolID, ToolName: event.ToolName, Status: router.ToolErrored, Message: event.Message}}
	case "text_annotation":
		if citations := extractCitations(event.Delta); len(citations) > 0 {
			return &router.Delta{Citations: citations}
		}
	case "reply":
		return &router.Delta{IsFinal: true}
	case "error":
		return &router.Delta{Text: "[error] " + event.Message, IsFinal: true}
	case "tool_approval_requested":
		inputJSON, _ := json.Marshal(event.ToolInput)
		return &router.Delta{ApprovalRequested: &router.ApprovalRequest{
			ToolID: event.ToolID, ToolName: event.ToolName, ToolInputJSON: string(inputJSON),
		}}
	}
	return nil
}

func extractCitations(delta map[string]any) []router.Citation {
	raw, _ := delta["annotations"].([]any)
	citations := make([]router.Citation, 0, len(raw))
	for _, item := range raw {
		annotation, ok := item.(map[string]any)
		if !ok {
			continue
		}
		metadata, _ := annotation["custom_metadata"].(map[string]any)
		url, _ := metadata["deep_url"].(string)
		if url == "" {
			url, _ = annotation["document_uri"].(string)
		}
		title, _ := metadata["heading"].(string)
		if title == "" {
			title, _ = metadata["title"].(string)
		}
		if title == "" {
			title, _ = annotation["file_name"].(string)
		}
		if title == "" {
			title = "Source"
		}
		endIndex := -1
		if value, ok := annotation["end_index"].(float64); ok {
			endIndex = int(value)
		}
		citations = append(citations, router.Citation{URL: url, Title: title, EndIndex: endIndex})
	}
	return citations
}
