package webhook

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/wire"
	"go.temporal.io/sdk/client"
)

const (
	routeMessages   = "/teams/messages"
	defaultIdentity = "default"
)

// teamMessageActivity contains only the incoming Bot Framework fields needed
// by the webhook. Outbound activity models live in the Python Teams SDK worker.
type teamMessageActivity struct {
	Type         string                   `json:"type"`
	ID           string                   `json:"id,omitempty"`
	ReplyToID    string                   `json:"replyToId,omitempty"`
	Timestamp    string                   `json:"timestamp,omitempty"`
	ServiceURL   string                   `json:"serviceUrl,omitempty"`
	ChannelID    string                   `json:"channelId,omitempty"`
	From         *teamChannelAccount      `json:"from,omitempty"`
	Conversation *teamConversationAccount `json:"conversation,omitempty"`
	Text         string                   `json:"text,omitempty"`
	Value        json.RawMessage          `json:"value,omitempty"`
}

type teamChannelAccount struct {
	ID string `json:"id,omitempty"`
}

type teamConversationAccount struct {
	ID               string `json:"id,omitempty"`
	ConversationType string `json:"conversationType,omitempty"`
}

// approvalButtonValue is the compact state embedded in the Python worker's
// Adaptive Card Action.Submit data.
type approvalButtonValue struct {
	SessionID string `json:"s"`
	ToolID    string `json:"t"`
	ToolName  string `json:"n"`
	Approved  bool   `json:"a"`
}

type webhookServer struct {
	tc        client.Client
	taskQueue string
	mux       *http.ServeMux
}

func NewServer(tc client.Client, taskQueue string) *webhookServer {
	s := &webhookServer{
		tc:        tc,
		taskQueue: taskQueue,
		mux:       http.NewServeMux(),
	}
	s.mux.HandleFunc(routeMessages, s.handleMessages)
	return s
}

func (s *webhookServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

func (s *webhookServer) handleMessages(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var act teamMessageActivity
	if err := json.NewDecoder(r.Body).Decode(&act); err != nil {
		http.Error(w, "failed to parse activity", http.StatusBadRequest)
		return
	}

	if act.Type != "message" {
		w.WriteHeader(http.StatusOK)
		return
	}

	var wfID string
	var input wire.Input
	if val, ok := decodeApprovalValue(act.Value); ok {
		wfID, input = approvalWorkflowInput(act, val)
	} else {
		if conversationID(act) == "" || senderID(act) == "" || strings.TrimSpace(act.Text) == "" {
			http.Error(w, "missing required fields", http.StatusBadRequest)
			return
		}
		if act.ID == "" && act.Timestamp == "" {
			http.Error(w, "missing activity id or timestamp", http.StatusBadRequest)
			return
		}
		wfID, input = messageWorkflowInput(act)
	}

	s.startConnectorWorkflow(r.Context(), wfID, input)
	w.WriteHeader(http.StatusOK)
}

func (s *webhookServer) startConnectorWorkflow(ctx context.Context, wfID string, input wire.Input) {
	if _, err := s.tc.ExecuteWorkflow(ctx,
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		router.WorkflowName,
		input,
	); err != nil {
		log.Printf("Failed to start connector workflow: %v", err)
	}
}

func messageWorkflowInput(act teamMessageActivity) (string, wire.Input) {
	sessionID := fmt.Sprintf("teams:%s", conversationID(act))
	interactionID := act.ID
	if interactionID == "" {
		interactionID = act.Timestamp
	}

	timestamp := act.Timestamp
	if act.ID != "" {
		timestamp = act.ID
	}

	msg := wire.IncomingMessage{
		MessageID:        act.ID,
		Sender:           senderID(act),
		Text:             act.Text,
		Timestamp:        timestamp,
		ConversationType: act.Conversation.ConversationType,
		ServiceURL:       act.ServiceURL,
		ChannelID:        act.ChannelID,
	}
	wfID := router.RouterWorkflowID(defaultIdentity, sessionID, interactionID)
	return wfID, wire.Input{
		Identity:  defaultIdentity,
		SessionID: sessionID,
		Message:   &msg,
	}
}

// decodeApprovalValue reports whether an activity's value field carries a
// tool-approval button payload.
func decodeApprovalValue(raw json.RawMessage) (approvalButtonValue, bool) {
	var val approvalButtonValue
	if len(raw) == 0 {
		return val, false
	}
	if err := json.Unmarshal(raw, &val); err != nil {
		return val, false
	}
	if val.SessionID == "" || val.ToolID == "" {
		return val, false
	}
	return val, true
}

func approvalWorkflowInput(act teamMessageActivity, val approvalButtonValue) (string, wire.Input) {
	wfID := router.RouterWorkflowID(defaultIdentity, val.SessionID, "approval-"+val.ToolID)
	return wfID, wire.Input{
		Identity:  defaultIdentity,
		SessionID: val.SessionID,
		Approval: &wire.ApprovalDecision{
			ToolID:     val.ToolID,
			ToolName:   val.ToolName,
			Approved:   val.Approved,
			ActivityID: act.ReplyToID,
			ServiceURL: act.ServiceURL,
			ChannelID:  act.ChannelID,
		},
	}
}

func conversationID(act teamMessageActivity) string {
	if act.Conversation == nil {
		return ""
	}
	return act.Conversation.ID
}

func senderID(act teamMessageActivity) string {
	if act.From == nil {
		return ""
	}
	return act.From.ID
}
