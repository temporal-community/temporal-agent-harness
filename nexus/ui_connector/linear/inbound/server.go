package linearinbound

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/linear"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/client"
)

const (
	routeEvents     = "/linear/events"
	defaultIdentity = "default"
)

var (
	errInvalidLinearSignature = errors.New("invalid Linear signature")
	errInvalidLinearTimestamp = errors.New("invalid Linear timestamp")
)

type Options struct {
	Identity string
}

// https://linear.app/developers/agent-interaction#session-webhooks
// https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql
// https://linear.app/developers/agent-interaction#activity-content-payload
type agentSessionEvent struct {
	Type           string    `json:"type"`
	Action         string    `json:"action"`
	OrganizationID string    `json:"organizationId"`
	PromptContext  string    `json:"promptContext"`
	AgentSession   session   `json:"agentSession"`
	AgentActivity  *activity `json:"agentActivity"`
}

type session struct {
	ID             string `json:"id"`
	OrganizationID string `json:"organizationId"`
	CreatorID      string `json:"creatorId"`
}

type activity struct {
	ID             string `json:"id"`
	AgentSessionID string `json:"agentSessionId"`
	UserID         string `json:"userId"`
	Signal         string `json:"signal"`
	Content        struct {
		Type string `json:"type"`
		Body string `json:"body"`
	} `json:"content"`
}

type webhookServer struct {
	tc            client.Client
	taskQueue     string
	identity      string
	signingSecret string
	mux           *http.ServeMux
}

func NewServer(tc client.Client, taskQueue, signingSecret string, opts Options) *webhookServer {
	identity := opts.Identity
	if identity == "" {
		identity = defaultIdentity
	}
	s := &webhookServer{
		tc: tc, taskQueue: taskQueue, identity: identity,
		signingSecret: signingSecret, mux: http.NewServeMux(),
	}
	s.mux.HandleFunc("POST "+routeEvents, s.handleEvents)
	return s
}

func (s *webhookServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if err := s.verifyLinearRequest(r); err != nil {
		if errors.Is(err, errInvalidLinearSignature) || errors.Is(err, errInvalidLinearTimestamp) {
			http.Error(w, err.Error(), http.StatusUnauthorized)
		} else {
			http.Error(w, err.Error(), http.StatusBadRequest)
		}
		return
	}
	s.mux.ServeHTTP(w, r)
}

func (s *webhookServer) verifyLinearRequest(r *http.Request) error {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return fmt.Errorf("read Linear request body: %w", err)
	}
	signature, err := hex.DecodeString(r.Header.Get("Linear-Signature"))
	mac := hmac.New(sha256.New, []byte(s.signingSecret))
	_, _ = mac.Write(body)
	if err != nil || s.signingSecret == "" || !hmac.Equal(signature, mac.Sum(nil)) {
		return errInvalidLinearSignature
	}

	var event struct {
		WebhookTimestamp int64 `json:"webhookTimestamp"`
	}
	if err := json.Unmarshal(body, &event); err != nil {
		return fmt.Errorf("parse Linear request body: %w", err)
	}
	// https://linear.app/developers/webhooks#securing-webhooks
	age := time.Since(time.UnixMilli(event.WebhookTimestamp))
	if age > time.Minute || age < -time.Minute {
		return errInvalidLinearTimestamp
	}

	// Restore the exact raw body so the route handler can parse it normally.
	r.Body = io.NopCloser(bytes.NewReader(body))
	return nil
}

func (s *webhookServer) handleEvents(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "failed to read body", http.StatusBadRequest)
		return
	}
	var event agentSessionEvent
	if err := json.Unmarshal(body, &event); err != nil || event.Type == "" {
		http.Error(w, "failed to parse event", http.StatusBadRequest)
		return
	}

	if event.Type != "AgentSessionEvent" || (event.Action != "created" && event.Action != "prompted") {
		w.WriteHeader(http.StatusOK)
		return
	}
	if event.OrganizationID == "" || event.AgentSession.ID == "" ||
		(event.AgentSession.OrganizationID != "" && event.AgentSession.OrganizationID != event.OrganizationID) {
		http.Error(w, "invalid Linear agent session", http.StatusBadRequest)
		return
	}
	var text, sender, interactionID string
	switch event.Action {
	case "created":
		text, sender, interactionID = event.PromptContext, event.AgentSession.CreatorID, "created"
	case "prompted":
		a := event.AgentActivity
		if a == nil || a.AgentSessionID != event.AgentSession.ID || a.ID == "" {
			http.Error(w, "invalid Linear prompt activity", http.StatusBadRequest)
			return
		}
		if a.Content.Type != "prompt" || a.Signal == "stop" {
			w.WriteHeader(http.StatusOK)
			return
		}
		text, sender, interactionID = a.Content.Body, a.UserID, "prompt-"+a.ID
	}
	if strings.TrimSpace(text) == "" {
		http.Error(w, "missing Linear prompt text", http.StatusBadRequest)
		return
	}
	sessionID := fmt.Sprintf("linear:%s:%s", event.OrganizationID, event.AgentSession.ID)
	wfID := router.RouterWorkflowID(s.identity, sessionID, interactionID)
	address, _ := json.Marshal(linear.Address{
		SessionID: sessionID, AgentSessionID: event.AgentSession.ID, OrganizationID: event.OrganizationID,
	})
	input := router.Input{
		Request: protocol.Request{SessionID: sessionID, Message: &protocol.Message{Text: text, Sender: sender}},
		Address: address,
	}
	s.startWorkflow(r.Context(), wfID, input)
	w.WriteHeader(http.StatusOK)
}

func (s *webhookServer) startWorkflow(ctx context.Context, wfID string, input router.Input) {
	if _, err := s.tc.ExecuteWorkflow(ctx, client.StartWorkflowOptions{
		ID: wfID, TaskQueue: s.taskQueue,
	}, router.WorkflowName, input); err != nil {
		log.Printf("Failed to start connector workflow %s: %v", wfID, err)
	}
}
