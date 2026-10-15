package slackinbound

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"

	slackapi "github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/interaction"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
)

const (
	routeEvents       = "/slack/events"
	routeInteractions = "/slack/interactions"
	defaultIdentity   = "default"
)

type webhookServer struct {
	tc                  client.Client
	taskQueue           string
	identity            string
	signingSecret       string
	botUserID           string
	allowedBotIDs       map[string]struct{}
	guideOnFirstContact bool
	slack               *slackapi.Client
	responseClient      *http.Client
	mux                 *http.ServeMux
}

// Options configures NewServer.
type Options struct {
	// Identity separates workflow IDs in a shared namespace. Empty uses "default".
	Identity string
	// BotUserID is the bot's own user ID, for mention detection. Empty treats every
	// message as a mention.
	BotUserID string
	// AllowedBotIDs lists the bots whose messages this server accepts.
	AllowedBotIDs []string
	// GuideOnFirstContact posts the handler guide when a mention starts a session.
	GuideOnFirstContact bool
	// Slack opens a form directly from a guide button that carries its fields. Nil
	// sends every form click through a router workflow.
	Slack *slackapi.Client
}

// NewServer wires up a Slack webhook handler.
func NewServer(tc client.Client, taskQueue, signingSecret string, opts Options) *webhookServer {
	identity := opts.Identity
	if identity == "" {
		identity = defaultIdentity
	}
	allowedBotIDSet := make(map[string]struct{}, len(opts.AllowedBotIDs))
	for _, id := range opts.AllowedBotIDs {
		allowedBotIDSet[id] = struct{}{}
	}
	s := &webhookServer{
		tc:                  tc,
		taskQueue:           taskQueue,
		identity:            identity,
		signingSecret:       signingSecret,
		botUserID:           opts.BotUserID,
		allowedBotIDs:       allowedBotIDSet,
		guideOnFirstContact: opts.GuideOnFirstContact,
		slack:               opts.Slack,
		responseClient: &http.Client{
			Timeout: 2 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		mux: http.NewServeMux(),
	}
	s.mux.HandleFunc(routeEvents, s.handleEvents)
	s.mux.HandleFunc(routeInteractions, s.handleInteractions)
	return s
}

func (s *webhookServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if err := s.verifySlackRequest(r); err != nil {
		http.Error(w, "invalid Slack signature", http.StatusUnauthorized)
		return
	}
	s.mux.ServeHTTP(w, r)
}

func (s *webhookServer) verifySlackRequest(r *http.Request) error {
	verifier, err := slackapi.NewSecretsVerifier(r.Header, s.signingSecret)
	if err != nil {
		return fmt.Errorf("create Slack signature verifier: %w", err)
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		return fmt.Errorf("read Slack request body: %w", err)
	}
	if _, err := verifier.Write(body); err != nil {
		return fmt.Errorf("hash Slack request body: %w", err)
	}
	if err := verifier.Ensure(); err != nil {
		return fmt.Errorf("verify Slack request signature: %w", err)
	}

	// Restore the exact raw body after signature verification so the route
	// handler can parse it normally.
	r.Body = io.NopCloser(bytes.NewReader(body))
	return nil
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
			// Return HTTP 200 for ignored events to prevent Slack retries.
			if ev.BotID == "" || s.isAllowedBot(ev.BotID) {
				mentioned := s.isMentioned(ev)
				switch {
				case mentioned:
					s.signalIncomingMessage(r.Context(), ev, mentioned, s.isFirstContact(r.Context(), ev))
				case ev.ThreadTimeStamp != "" && s.threadHasBotSession(r.Context(), ev.Channel, ev.ThreadTimeStamp):
					s.signalIncomingMessage(r.Context(), ev, mentioned, false)
				}
			}
		}
	}
	w.WriteHeader(http.StatusOK)
}

func (s *webhookServer) isMentioned(ev *slackevents.MessageEvent) bool {
	return s.botUserID == "" || strings.Contains(ev.Text, "<@"+s.botUserID+">")
}

// isFirstContact checks for a new thread or a thread with no router workflow.
func (s *webhookServer) isFirstContact(ctx context.Context, ev *slackevents.MessageEvent) bool {
	if !s.guideOnFirstContact {
		return false
	}
	if ev.ThreadTimeStamp == "" || ev.ThreadTimeStamp == ev.TimeStamp {
		return true
	}
	return !s.threadHasBotSession(ctx, ev.Channel, ev.ThreadTimeStamp)
}

func (s *webhookServer) stripBotMention(text string) string {
	if s.botUserID == "" {
		return text
	}
	return strings.TrimSpace(strings.ReplaceAll(text, "<@"+s.botUserID+">", ""))
}

func (s *webhookServer) isAllowedBot(botID string) bool {
	_, ok := s.allowedBotIDs[botID]
	return ok
}

func threadSessionID(channel, threadRoot string) string {
	return fmt.Sprintf("slack:%s:%s", channel, threadRoot)
}

// threadHasBotSession checks for a router workflow in the thread. This avoids
// backend queries for replies in threads that have no bot interaction.
func (s *webhookServer) threadHasBotSession(ctx context.Context, channel, threadRoot string) bool {
	prefix := router.RouterWorkflowIDPrefix(s.identity, threadSessionID(channel, threadRoot))
	resp, err := s.tc.ListWorkflow(ctx, &workflowservice.ListWorkflowExecutionsRequest{
		Query:    fmt.Sprintf("WorkflowId STARTS_WITH %q", prefix),
		PageSize: 1,
	})
	if err != nil {
		log.Printf("threadHasBotSession: list workflows for prefix %s failed: %v", prefix, err)
		return false
	}
	return len(resp.GetExecutions()) > 0
}

func (s *webhookServer) signalIncomingMessage(ctx context.Context, ev *slackevents.MessageEvent, mentioned, firstContact bool) {
	threadRoot := ev.ThreadTimeStamp
	if threadRoot == "" {
		threadRoot = ev.TimeStamp
	}
	sessionID := threadSessionID(ev.Channel, threadRoot)
	req := protocol.Request{SessionID: sessionID, Message: &protocol.Message{
		Text:                    s.stripBotMention(ev.Text),
		Sender:                  ev.User,
		RequiresExistingSession: !mentioned,
		FirstContact:            firstContact,
	}}
	addr := slack.Address{SessionID: sessionID, Channel: ev.Channel, ThreadTS: threadRoot, UserID: ev.User}
	s.startWorkflow(ctx, router.RouterWorkflowID(s.identity, sessionID, ev.TimeStamp), req, addr)
}

func (s *webhookServer) startWorkflow(ctx context.Context, wfID string, req protocol.Request, addr slack.Address) {
	address, err := json.Marshal(addr)
	if err != nil {
		log.Printf("Failed to encode address for %s: %v", wfID, err)
		return
	}
	if _, err := s.tc.ExecuteWorkflow(ctx,
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		router.WorkflowName,
		router.Input{Request: req, Address: address},
	); err != nil {
		log.Printf("Failed to start connector workflow %s: %v", wfID, err)
	}
}

// sessionAddress returns the address of a thread-scoped session ID
// ("slack:<channel>:<threadRoot>").
func sessionAddress(sessionID string) slack.Address {
	addr := slack.Address{SessionID: sessionID}
	parts := strings.SplitN(sessionID, ":", 3)
	if len(parts) > 1 {
		addr.Channel = parts[1]
	}
	if len(parts) > 2 {
		addr.ThreadTS = parts[2]
	}
	return addr
}

// handleInteractions handles POST /slack/interactions: Block Kit button clicks and
// modal submissions.
func (s *webhookServer) handleInteractions(w http.ResponseWriter, r *http.Request) {
	var payload slackapi.InteractionCallback
	if err := json.Unmarshal([]byte(r.FormValue("payload")), &payload); err != nil {
		http.Error(w, "failed to parse interaction payload", http.StatusBadRequest)
		return
	}

	switch payload.Type {
	case slackapi.InteractionTypeBlockActions:
	actions:
		for _, action := range payload.ActionCallback.BlockActions {
			switch {
			case action.ActionID == interaction.ActionApprove ||
				action.ActionID == interaction.ActionAlwaysAllow ||
				action.ActionID == interaction.ActionDeny:
				s.handleApprovalClick(r.Context(), payload, action)
				break actions
			case strings.HasPrefix(action.ActionID, interaction.ActionOpenFormPrefix):
				s.handleFormClick(r.Context(), payload, action)
				break actions
			}
		}
		w.WriteHeader(http.StatusOK)

	case slackapi.InteractionTypeViewSubmission:
		s.handleFormSubmission(w, r.Context(), payload)

	default:
		w.WriteHeader(http.StatusOK)
	}
}

// Remove approval buttons before starting the decision workflow. This prevents
// the response_url update from replacing the backend's final decision later.
func (s *webhookServer) handleApprovalClick(ctx context.Context, payload slackapi.InteractionCallback, action *slackapi.BlockAction) {
	var val interaction.ApprovalButtonValue
	if err := json.Unmarshal([]byte(action.Value), &val); err != nil {
		log.Printf("handleInteractions: failed to decode approval button value: %v", err)
		return
	}

	verdict := "Denied"
	switch {
	case val.Approved && val.Remember:
		verdict = "Always allowed"
	case val.Approved:
		verdict = "Approved"
	}
	reason := fmt.Sprintf("%s by <@%s> in Slack.", verdict, payload.User.ID)

	if responseURL := payload.ResponseURL; responseURL != "" {
		body, _ := json.Marshal(map[string]any{
			"replace_original": true,
			"text":             fmt.Sprintf("🔐 `%s`: %s", val.ToolName, reason),
		})
		if err := s.postResponse(ctx, responseURL, body); err != nil {
			log.Printf("handleInteractions: response_url POST failed: %v", err)
		}
	}

	req := protocol.Request{SessionID: val.SessionID, Approval: &protocol.ApprovalDecision{
		ToolID:   val.ToolID,
		ToolName: val.ToolName,
		Approved: val.Approved,
		Remember: val.Remember,
		Reason:   reason,
	}}
	s.startWorkflow(ctx, router.RouterWorkflowID(s.identity, val.SessionID, "approval-"+val.ToolID), req, sessionAddress(val.SessionID))
}

// postResponse sends an interaction response only to Slack's HTTPS hook hosts.
func (s *webhookServer) postResponse(ctx context.Context, responseURL string, body []byte) error {
	u, err := url.Parse(responseURL)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Opaque != "" || u.Fragment != "" {
		return fmt.Errorf("invalid Slack response URL")
	}
	var host string
	switch u.Host {
	case "hooks.slack.com":
		host = "hooks.slack.com"
	case "hooks.slack-gov.com":
		host = "hooks.slack-gov.com"
	default:
		return fmt.Errorf("invalid Slack response host")
	}
	// Construct the destination with a fixed scheme and host.
	target := url.URL{Scheme: "https", Host: host, Path: u.Path, RawPath: u.RawPath, RawQuery: u.RawQuery}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target.String(), bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("could not create Slack response request")
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.responseClient.Do(req)
	if err != nil {
		// A response URL contains a token. Do not include it in an error log.
		if urlErr, ok := err.(*url.Error); ok {
			err = urlErr.Err
		}
		return fmt.Errorf("Slack response request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Slack response returned HTTP %d", resp.StatusCode)
	}
	return nil
}

// handleFormClick opens a form directly from inline fields or starts a router request.
// Slack's trigger_id expires about 3 seconds after the click.
func (s *webhookServer) handleFormClick(ctx context.Context, payload slackapi.InteractionCallback, action *slackapi.BlockAction) {
	var val interaction.FormButtonValue
	if err := json.Unmarshal([]byte(action.Value), &val); err != nil {
		log.Printf("handleInteractions: failed to decode form button value: %v", err)
		return
	}
	addr := sessionAddress(val.SessionID)
	if view, ok := interaction.FormViewFromButton(val, addr.ThreadTS); ok && s.slack != nil {
		if _, err := s.slack.OpenViewContext(ctx, payload.TriggerID, view); err != nil {
			log.Printf("handleInteractions: views.open for %s failed: %v", val.Handler, err)
		}
		return
	}
	addr.UserID = payload.User.ID
	addr.TriggerID = payload.TriggerID
	req := protocol.Request{SessionID: val.SessionID, OpenForm: &protocol.FormRequest{Handler: val.Handler}}
	s.startWorkflow(ctx, router.RouterWorkflowID(s.identity, val.SessionID, "form-"+payload.TriggerID), req, addr)
}

// Return field errors in the modal, or send the payload to the handler.
func (s *webhookServer) handleFormSubmission(w http.ResponseWriter, ctx context.Context, payload slackapi.InteractionCallback) {
	if payload.View.CallbackID != interaction.FormCallbackID {
		w.WriteHeader(http.StatusOK)
		return
	}
	meta, body, fieldErrors, err := interaction.DecodeFormSubmission(payload.View)
	if err != nil {
		log.Printf("handleInteractions: bad form submission: %v", err)
		w.WriteHeader(http.StatusOK)
		return
	}
	if len(fieldErrors) > 0 {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"response_action": "errors", "errors": fieldErrors})
		return
	}

	// Use the view ID to identify repeated deliveries of this submission.
	addr := sessionAddress(meta.SessionID)
	addr.ThreadTS = meta.ThreadID
	addr.UserID = payload.User.ID
	req := protocol.Request{SessionID: meta.SessionID, Call: &protocol.HandlerCall{Name: meta.Handler, Payload: body}}
	s.startWorkflow(ctx, router.RouterWorkflowID(s.identity, meta.SessionID, "call-"+payload.View.ID), req, addr)
	w.WriteHeader(http.StatusOK)
}
