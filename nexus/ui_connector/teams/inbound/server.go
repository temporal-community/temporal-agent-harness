package teamsinbound

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
)

const (
	routeMessages = "/teams/messages"
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
	tunnel            router.Client
	deliveryTaskQueue string
	mux               *http.ServeMux
}

func NewServer(tunnel router.Client, deliveryTaskQueue string) *webhookServer {
	s := &webhookServer{
		tunnel:            tunnel,
		deliveryTaskQueue: deliveryTaskQueue,
		mux:               http.NewServeMux(),
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

	if val, ok := decodeApprovalValue(act.Value); ok {
		s.resolveApproval(r.Context(), act, val)
	} else {
		if conversationID(act) == "" || senderID(act) == "" || strings.TrimSpace(act.Text) == "" {
			http.Error(w, "missing required fields", http.StatusBadRequest)
			return
		}
		if act.ID == "" && act.Timestamp == "" {
			http.Error(w, "missing activity id or timestamp", http.StatusBadRequest)
			return
		}
		s.submitMessage(r.Context(), act)
	}
	w.WriteHeader(http.StatusOK)
}

func (s *webhookServer) submitMessage(ctx context.Context, act teamMessageActivity) {
	sessionID := fmt.Sprintf("teams:%s", conversationID(act))
	interactionID := act.ID
	if interactionID == "" {
		interactionID = act.Timestamp
	}
	metadata := map[string]any{
		"SenderID": senderID(act), "SessionID": sessionID, "ThreadID": act.ID,
		"Text": "", "ServiceURL": act.ServiceURL, "ChannelID": act.ChannelID,
	}
	deliveryContext, _ := json.Marshal(map[string]any{
		"metadata":         metadata,
		"conversationType": act.Conversation.ConversationType,
	})
	_, err := s.tunnel.SendAndMount(ctx, sessionID, "teams-message-"+interactionID,
		router.SendAndMountInput{
			Subscriber: router.Subscriber{
				ID:       "teams:" + sessionID,
				Mode:     router.Participant,
				Delivery: &router.DeliveryTarget{Activity: "TeamsDeliverA2A", TaskQueue: s.deliveryTaskQueue, Context: deliveryContext},
			},
			Message: router.SendMessageInput{MessageType: "ask", Payload: map[string]any{"text": act.Text}},
		})
	if err != nil {
		log.Printf("Failed to submit Teams message through UI tunnel: %v", err)
	}
}

func (s *webhookServer) resolveApproval(ctx context.Context, act teamMessageActivity, val approvalButtonValue) {
	payload, _ := json.Marshal(map[string]any{"toolId": val.ToolID, "approved": val.Approved})
	metadata := map[string]any{
		"SenderID": senderID(act), "SessionID": val.SessionID, "ThreadID": act.ReplyToID,
		"Text": "", "ServiceURL": act.ServiceURL, "ChannelID": act.ChannelID,
	}
	deliveryContext, _ := json.Marshal(map[string]any{
		"metadata": metadata, "activityId": act.ReplyToID, "toolName": val.ToolName, "approved": val.Approved,
	})
	_, err := s.tunnel.Control(ctx, val.SessionID, "teams-approval-"+val.ToolID,
		router.ControlInput{
			Kind: "approve-tool-call", Payload: payload,
			Delivery: &router.DeliveryTarget{Activity: "TeamsAcknowledgeApproval", TaskQueue: s.deliveryTaskQueue, Context: deliveryContext},
		})
	if err != nil {
		log.Printf("Failed to resolve Teams approval through UI tunnel: %v", err)
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
