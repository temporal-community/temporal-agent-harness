package slackinbound

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
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	slackoutbound "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/outbound"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
)

const (
	routeEvents       = "/slack/events"
	routeInteractions = "/slack/interactions"
	routeCommands     = "/slack/commands"
	defaultIdentity   = "default"
)

type webhookServer struct {
	tc                 client.Client
	taskQueue          string
	identity           string
	signingSecret      string
	botUserID          string
	slashCommandPrefix string
	allowedBotIDs      map[string]struct{}
	mux                *http.ServeMux
}

// NewServer wires up a Slack webhook handler. identity distinguishes this
// server's router workflows from any other identity sharing the same
// Temporal namespace (e.g. running prod/staging/ondemand environments
// against one "connector" namespace) — it's baked into every router
// workflow ID this server starts. Pass "" to use defaultIdentity.
//
// slashCommandPrefix removes a "<prefix>-" prefix from the command name.
// Use this when several bots share one Slack workspace. Slack requires
// unique command names per workspace, so each bot's manifest can register
// prefixed commands, like "/bot-dev-cmd". This strips the prefix so the
// command matches a known name, like "cmd". Pass "" if this bot has no
// prefix.
//
// allowedBotIDs are other bots allowed to trigger this server. Every other
// bot message is still ignored, including this bot's own echoes. Pass nil
// or empty to allow none (today's behavior).
func NewServer(tc client.Client, taskQueue, identity, signingSecret, botUserID, slashCommandPrefix string, allowedBotIDs []string) *webhookServer {
	if identity == "" {
		identity = defaultIdentity
	}
	allowedBotIDSet := make(map[string]struct{}, len(allowedBotIDs))
	for _, id := range allowedBotIDs {
		allowedBotIDSet[id] = struct{}{}
	}
	s := &webhookServer{
		tc:                 tc,
		taskQueue:          taskQueue,
		identity:           identity,
		signingSecret:      signingSecret,
		botUserID:          botUserID,
		slashCommandPrefix: slashCommandPrefix,
		allowedBotIDs:      allowedBotIDSet,
		mux:                http.NewServeMux(),
	}
	s.mux.HandleFunc(routeEvents, s.handleEvents)
	s.mux.HandleFunc(routeInteractions, s.handleInteractions)
	s.mux.HandleFunc(routeCommands, s.handleSlashCommands)
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
			// Ignore bot messages, including this bot's own echoes, except
			// allowedBotIDs. Always fall through to WriteHeader(200) below -
			// a bare return reads as a 5xx to the Lambda proxy and Slack retries.
			if ev.BotID == "" || s.isAllowedBot(ev.BotID) {
				mentioned := s.isMentioned(ev)
				switch {
				case mentioned:
					s.signalIncomingMessage(r.Context(), ev, mentioned)
				case ev.ThreadTimeStamp != "" && s.threadHasBotSession(r.Context(), ev.Channel, ev.ThreadTimeStamp):
					s.signalIncomingMessage(r.Context(), ev, mentioned)
				}
			}
		}
	}
	w.WriteHeader(http.StatusOK)
}

func (s *webhookServer) isMentioned(ev *slackevents.MessageEvent) bool {
	return s.botUserID == "" || strings.Contains(ev.Text, "<@"+s.botUserID+">")
}

func (s *webhookServer) isAllowedBot(botID string) bool {
	_, ok := s.allowedBotIDs[botID]
	return ok
}

// threadSessionID scopes an agent session to one Slack thread: the channel plus
// the thread root. A top-level message uses its own ts as the root (it starts a
// new thread), so each conversation gets an isolated session.
func threadSessionID(channel, threadRoot string) string {
	return fmt.Sprintf("slack:%s:%s", channel, threadRoot)
}

// threadHasBotSession reports whether any router workflow was ever started for this
// thread - which only happens when some message in it mentioned the bot, since a
// mention-free message only reaches signalIncomingMessage once this check already
// passed. The bot may have been mentioned on the thread root or on any reply deep in
// the thread, so the triggering message's own ts (baked into the trailing segment of
// its workflow ID) isn't known in advance; every router workflow for this thread
// shares the same "connector-<identity>-<sessionID>-" prefix though, since sessionID
// is scoped to the thread rather than the message. A prefix search on that avoids
// starting a router workflow, and querying the backend, for every reply in threads
// that never involved the bot.
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

func (s *webhookServer) signalIncomingMessage(ctx context.Context, ev *slackevents.MessageEvent, mentioned bool) {
	threadRoot := ev.ThreadTimeStamp
	if threadRoot == "" {
		threadRoot = ev.TimeStamp
	}
	sessionID := threadSessionID(ev.Channel, threadRoot)
	msg := router.IncomingMessage{
		MessageID:               ev.TimeStamp,
		Sender:                  ev.User,
		Text:                    ev.Text,
		Timestamp:               ev.TimeStamp,
		ThreadID:                threadRoot,
		RequiresExistingSession: !mentioned,
	}
	wfID := router.RouterWorkflowID(s.identity, sessionID, ev.TimeStamp)
	if _, err := s.tc.ExecuteWorkflow(ctx,
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		router.WorkflowName,
		router.Input{
			Identity:  s.identity,
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
	if s.slashCommandPrefix != "" {
		// Strip the prefix if present. If not present, leave the command
		// as-is. It will not match a known command later.
		command = strings.TrimPrefix(command, s.slashCommandPrefix+"-")
	}
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

	wfID := router.RouterWorkflowID(s.identity, sessionID, triggerID)
	if _, err := s.tc.ExecuteWorkflow(r.Context(),
		client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
		router.WorkflowName,
		router.Input{
			Identity:  s.identity,
			SessionID: sessionID,
			Slash: &router.SlashCommand{
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
		var val slackoutbound.ApprovalButtonValue
		if err := json.Unmarshal([]byte(action.Value), &val); err != nil {
			log.Printf("handleInteractions: failed to decode button value: %v", err)
			continue
		}

		// Start a dedicated workflow to call approveToolCall via Nexus.
		wfID := router.RouterWorkflowID(s.identity, val.SessionID, "approval-"+val.ToolID)
		if _, err := s.tc.ExecuteWorkflow(r.Context(),
			client.StartWorkflowOptions{ID: wfID, TaskQueue: s.taskQueue},
			router.WorkflowName,
			router.Input{
				SessionID: val.SessionID,
				Identity:  s.identity,
				Approval: &router.ApprovalDecision{
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
