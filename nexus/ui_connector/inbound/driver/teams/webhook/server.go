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

	// Adaptive Card Action.Submit clicks arrive as message activities with
	// empty text and the clicked button's data object in value. Check this
	// before the text validation below.
	if val, ok := decodeApprovalValue(act.Value); ok {
		s.handleApprovalSubmit(r.Context(), act, val)
		w.WriteHeader(http.StatusOK)
		return
	}

	if conversationID(act) == "" || senderID(act) == "" || strings.TrimSpace(act.Text) == "" {
		http.Error(w, "missing required fields", http.StatusBadRequest)
		return
	}
	if act.ID == "" && act.Timestamp == "" {
		http.Error(w, "missing activity id or timestamp", http.StatusBadRequest)
		return
	}

	s.signalIncomingMessage(r.Context(), act)
	w.WriteHeader(http.StatusOK)
}

func (s *webhookServer) signalIncomingMessage(ctx context.Context, act teamMessageActivity) {
	wfID, input := messageWorkflowInput(act)
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

// handleApprovalSubmit routes an approval button click to the connector
// workflow. The Python activity worker replaces the card after the decision;
// the workflow ID dedupes repeat clicks for the same tool ID.
func (s *webhookServer) handleApprovalSubmit(ctx context.Context, act teamMessageActivity, val approvalButtonValue) {
	wfID, input := approvalWorkflowInput(act, val)
	if _, err := s.tc.ExecuteWorkflow(ctx,
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		router.WorkflowName,
		input,
	); err != nil {
		log.Printf("Failed to start connector workflow for approval: %v", err)
	}
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
