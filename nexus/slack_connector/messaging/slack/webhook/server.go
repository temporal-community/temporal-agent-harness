package webhook

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"

	slackapi "github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
	agentiface "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/agent"
	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/connector"
	slackmsg "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/slack"
	"go.temporal.io/sdk/client"
)

const (
	routeEvents       = "/slack/events"
	routeInteractions = "/slack/interactions"
	routeCommands     = "/slack/commands"
	defaultIdentity   = "default"
)

type webhookServer struct {
	tc            client.Client
	taskQueue     string
	signingSecret string
	botUserID     string
	mux           *http.ServeMux
}

func NewServer(tc client.Client, taskQueue, signingSecret, botUserID string) *webhookServer {
	s := &webhookServer{
		tc:            tc,
		taskQueue:     taskQueue,
		signingSecret: signingSecret,
		botUserID:     botUserID,
		mux:           http.NewServeMux(),
	}
	s.mux.HandleFunc(routeEvents, s.handleEvents)
	s.mux.HandleFunc(routeInteractions, s.handleInteractions)
	s.mux.HandleFunc(routeCommands, s.handleSlashCommands)
	return s
}

func (s *webhookServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Verify the Slack signature before routing. Slack signs every request with
	// the app signing secret over the raw body, so we must read and verify the
	// body here, then hand a fresh reader to the route handlers.
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "failed to read body", http.StatusBadRequest)
		return
	}
	if err := s.verifySignature(r.Header, body); err != nil {
		log.Printf("webhook: signature verification failed: %v", err)
		http.Error(w, "invalid request signature", http.StatusUnauthorized)
		return
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	s.mux.ServeHTTP(w, r)
}

// verifySignature validates the Slack request signature and timestamp using the
// signing secret (HMAC-SHA256 over "v0:{timestamp}:{body}", with a 5-minute
// timestamp window enforced by NewSecretsVerifier). See
// https://api.slack.com/authentication/verifying-requests-from-slack.
func (s *webhookServer) verifySignature(header http.Header, body []byte) error {
	verifier, err := slackapi.NewSecretsVerifier(header, s.signingSecret)
	if err != nil {
		return err
	}
	if _, err := verifier.Write(body); err != nil {
		return err
	}
	return verifier.Ensure()
}

func (s *webhookServer) handleEvents(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "failed to read body", http.StatusBadRequest)
		return
	}
	evt, err := slackevents.ParseEvent(json.RawMessage(body), slackevents.OptionNoVerifyToken())
	if err != nil {
		http.Error(w, "failed to parse event", http.StatusBadRequest)
		return
	}

	switch evt.Type {
	case slackevents.URLVerification:
		var challenge slackevents.EventsAPIURLVerificationEvent
		if err := json.Unmarshal(body, &challenge); err != nil {
			http.Error(w, "failed to parse challenge", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "text/plain")
		_, _ = fmt.Fprint(w, challenge.Challenge)

	case slackevents.CallbackEvent:
		if ev, ok := evt.InnerEvent.Data.(*slackevents.MessageEvent); ok {
			if ev.BotID != "" {
				return
			}
			if s.botUserID != "" && !strings.Contains(ev.Text, "<@"+s.botUserID+">") {
				return
			}
			s.signalIncomingMessage(r.Context(), ev)
		}
	}
	w.WriteHeader(http.StatusOK)
}

func (s *webhookServer) signalIncomingMessage(ctx context.Context, ev *slackevents.MessageEvent) {
	sessionID := fmt.Sprintf("slack:%s", ev.Channel)
	msg := agentiface.IncomingMessage{
		MessageID: ev.TimeStamp,
		Sender:    ev.User,
		Text:      ev.Text,
		Timestamp: ev.TimeStamp,
	}
	wfID := agentiface.ConnectorWorkflowID(defaultIdentity, sessionID, ev.TimeStamp)
	if _, err := s.tc.ExecuteWorkflow(ctx,
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		connector.WorkflowName,
		agentiface.ConnectorWorkflowInput{
			Identity:  defaultIdentity,
			SessionID: sessionID,
			Message:   &msg,
		},
	); err != nil {
		log.Printf("Failed to start connector workflow: %v", err)
	}
}

func (s *webhookServer) handleSlashCommands(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		http.Error(w, "failed to parse form", http.StatusBadRequest)
		return
	}

	command := strings.TrimPrefix(r.FormValue("command"), "/")
	channelID := r.FormValue("channel_id")
	triggerID := r.FormValue("trigger_id")
	userID := r.FormValue("user_id")
	arg := strings.TrimSpace(r.FormValue("text"))
	threadTS := r.FormValue("thread_ts") // non-empty if command was used inside a thread

	if command == "" || channelID == "" {
		http.Error(w, "missing required fields", http.StatusBadRequest)
		return
	}

	sessionID := fmt.Sprintf("slack:%s", channelID)

	wfID := agentiface.ConnectorWorkflowID(defaultIdentity, sessionID, triggerID)
	if _, err := s.tc.ExecuteWorkflow(r.Context(),
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		connector.WorkflowName,
		agentiface.ConnectorWorkflowInput{
			Identity:  defaultIdentity,
			SessionID: sessionID,
			Slash: &agentiface.SlashCommand{
				Name:     command,
				Arg:      arg,
				ThreadID: threadTS,
				SenderID: userID,
			},
		},
	); err != nil {
		log.Printf("Failed to start connector workflow for slash command: %v", err)
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
}

// handleInteractions handles POST /slack/interactions — Block Kit button clicks.
// Currently handles tool-approval buttons; responds inline to replace the prompt.
func (s *webhookServer) handleInteractions(w http.ResponseWriter, r *http.Request) {
	var payload slackapi.InteractionCallback
	if err := json.Unmarshal([]byte(r.FormValue("payload")), &payload); err != nil {
		http.Error(w, "failed to parse interaction payload", http.StatusBadRequest)
		return
	}

	if payload.Type != slackapi.InteractionTypeBlockActions {
		w.WriteHeader(http.StatusOK)
		return
	}

	for _, action := range payload.ActionCallback.BlockActions {
		if action.ActionID != "tool_approval_approve" && action.ActionID != "tool_approval_deny" {
			continue
		}
		var val slackmsg.ApprovalButtonValue
		if err := json.Unmarshal([]byte(action.Value), &val); err != nil {
			log.Printf("handleInteractions: failed to decode button value: %v", err)
			continue
		}

		// Start a dedicated workflow to call approveToolCall via Nexus.
		wfID := agentiface.ConnectorWorkflowID(defaultIdentity, val.SessionID, "approval-"+val.ToolID)
		if _, err := s.tc.ExecuteWorkflow(r.Context(),
			client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
			connector.WorkflowName,
			agentiface.ConnectorWorkflowInput{
				SessionID: val.SessionID,
				Identity:  defaultIdentity,
				Approval: &agentiface.ApprovalDecision{
					ToolID:   val.ToolID,
					ToolName: val.ToolName,
					Approved: val.Approved,
				},
			},
		); err != nil {
			log.Printf("handleInteractions: failed to start connector workflow for approval: %v", err)
		}

		// Replace the approval prompt via response_url so the buttons can't be clicked again.
		// ExecuteWorkflow and this POST are both fast (sub-100ms each), well within Slack's 3s window.
		decision := "✅ Approved"
		if !val.Approved {
			decision = "❌ Denied"
		}
		if responseURL := payload.ResponseURL; responseURL != "" {
			body, _ := json.Marshal(map[string]any{
				"replace_original": true,
				"text":             fmt.Sprintf("🔐 Tool `%s`: %s", val.ToolName, decision),
			})
			resp, err := http.Post(responseURL, "application/json", strings.NewReader(string(body))) //nolint:noctx
			if err != nil {
				log.Printf("handleInteractions: response_url POST failed: %v", err)
			} else {
				resp.Body.Close()
			}
		}
		w.WriteHeader(http.StatusOK)
		return
	}

	w.WriteHeader(http.StatusOK)
}
