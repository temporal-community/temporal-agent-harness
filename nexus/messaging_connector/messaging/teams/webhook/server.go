package webhook

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"

	agentiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent"
	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/connector"
	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging"
	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging/teams"
	"go.temporal.io/sdk/client"
)

const (
	routeMessages   = "/teams/messages"
	defaultIdentity = "default"
)

type webhookServer struct {
	tc        client.Client
	taskQueue string
	platform  *teams.TeamsPlatform
	mux       *http.ServeMux
}

func NewServer(tc client.Client, taskQueue string, platform *teams.TeamsPlatform) *webhookServer {
	s := &webhookServer{
		tc:        tc,
		taskQueue: taskQueue,
		platform:  platform,
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

	var act msgiface.TeamMessageActivity
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

func (s *webhookServer) signalIncomingMessage(ctx context.Context, act msgiface.TeamMessageActivity) {
	wfID, input := messageWorkflowInput(act)
	if _, err := s.tc.ExecuteWorkflow(ctx,
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		connector.WorkflowName,
		input,
	); err != nil {
		log.Printf("Failed to start connector workflow: %v", err)
	}
}

func messageWorkflowInput(act msgiface.TeamMessageActivity) (string, agentiface.ConnectorWorkflowInput) {
	sessionID := fmt.Sprintf("teams:%s", conversationID(act))
	interactionID := act.ID
	if interactionID == "" {
		interactionID = act.Timestamp
	}

	timestamp := act.Timestamp
	if act.ID != "" {
		timestamp = act.ID
	}

	msg := agentiface.IncomingMessage{
		MessageID:        act.ID,
		Sender:           senderID(act),
		Text:             act.Text,
		Timestamp:        timestamp,
		ConversationType: act.Conversation.ConversationType,
	}
	wfID := agentiface.ConnectorWorkflowID(defaultIdentity, sessionID, interactionID)
	return wfID, agentiface.ConnectorWorkflowInput{
		Identity:  defaultIdentity,
		SessionID: sessionID,
		Message:   &msg,
	}
}

// decodeApprovalValue reports whether an activity's value field carries a
// tool-approval button payload.
func decodeApprovalValue(raw json.RawMessage) (teams.ApprovalButtonValue, bool) {
	var val teams.ApprovalButtonValue
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
// workflow, then replaces the card so the buttons can't be clicked again.
// The workflow ID dedupes repeat clicks for the same tool ID.
func (s *webhookServer) handleApprovalSubmit(ctx context.Context, act msgiface.TeamMessageActivity, val teams.ApprovalButtonValue) {
	wfID := agentiface.ConnectorWorkflowID(defaultIdentity, val.SessionID, "approval-"+val.ToolID)
	if _, err := s.tc.ExecuteWorkflow(ctx,
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		connector.WorkflowName,
		agentiface.ConnectorWorkflowInput{
			Identity:  defaultIdentity,
			SessionID: val.SessionID,
			Approval: &agentiface.ApprovalDecision{
				ToolID:   val.ToolID,
				ToolName: val.ToolName,
				Approved: val.Approved,
			},
		},
	); err != nil {
		log.Printf("Failed to start connector workflow for approval: %v", err)
	}

	// Replace the approval card with its outcome. act.ReplyToID is the card's
	// activity ID. Best-effort: the decision is already recorded above.
	if s.platform == nil || act.ReplyToID == "" {
		return
	}
	decision := "✅ Approved"
	if !val.Approved {
		decision = "❌ Denied"
	}
	text := fmt.Sprintf("🔐 Tool `%s`: %s", val.ToolName, decision)
	if err := s.platform.UpdateActivity(ctx, val.SessionID, act.ReplyToID, text); err != nil {
		log.Printf("Failed to update Teams approval card: %v", err)
	}
}

func conversationID(act msgiface.TeamMessageActivity) string {
	if act.Conversation == nil {
		return ""
	}
	return act.Conversation.ID
}

func senderID(act msgiface.TeamMessageActivity) string {
	if act.From == nil {
		return ""
	}
	return act.From.ID
}
