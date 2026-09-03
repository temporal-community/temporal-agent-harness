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
)

const (
	routeEvents       = "/slack/events"
	routeInteractions = "/slack/interactions"
	routeCommands     = "/slack/commands"
)

type webhookServer struct {
	tunnel             router.Client
	deliveryTaskQueue  string
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
func NewServer(tunnel router.Client, deliveryTaskQueue, signingSecret, botUserID, slashCommandPrefix string, allowedBotIDs []string) *webhookServer {
	allowedBotIDSet := make(map[string]struct{}, len(allowedBotIDs))
	for _, id := range allowedBotIDs {
		allowedBotIDSet[id] = struct{}{}
	}
	s := &webhookServer{
		tunnel:             tunnel,
		deliveryTaskQueue:  deliveryTaskQueue,
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
					s.signalIncomingMessage(r.Context(), ev)
				case ev.ThreadTimeStamp != "" && s.threadHasBotSession(r.Context(), ev.Channel, ev.ThreadTimeStamp):
					s.signalIncomingMessage(r.Context(), ev)
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

// threadHasBotSession asks the A2A/HarnessControl front door whether this thread's
// agent task exists. Mention-free replies only continue threads that already mounted
// the bot; checking the agent avoids retaining a connector workflow between turns.
func (s *webhookServer) threadHasBotSession(ctx context.Context, channel, threadRoot string) bool {
	return s.tunnel.Exists(ctx, threadSessionID(channel, threadRoot))
}

func (s *webhookServer) signalIncomingMessage(ctx context.Context, ev *slackevents.MessageEvent) {
	threadRoot := ev.ThreadTimeStamp
	if threadRoot == "" {
		threadRoot = ev.TimeStamp
	}
	sessionID := threadSessionID(ev.Channel, threadRoot)
	metadata := router.TextMetadata{SenderID: ev.User, SessionID: sessionID, ThreadID: threadRoot}
	context, _ := json.Marshal(slackoutbound.DeliveryContext{Metadata: metadata})
	_, err := s.tunnel.SendAndMount(ctx, sessionID, "slack-message-"+ev.TimeStamp,
		router.SendAndMountInput{
			Subscriber: router.Subscriber{
				ID:       "slack:" + sessionID,
				Mode:     router.Participant,
				Delivery: &router.DeliveryTarget{Activity: slackoutbound.DeliverA2AActivity, TaskQueue: s.deliveryTaskQueue, Context: context},
			},
			Message: router.SendMessageInput{MessageType: "ask", Payload: map[string]any{"text": ev.Text}},
		})
	if err != nil {
		log.Printf("Failed to submit Slack message through UI tunnel: %v", err)
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
	if threadTS != "" {
		sessionID += ":" + threadTS
	}
	metadata := router.TextMetadata{SenderID: userID, SessionID: sessionID, ThreadID: threadTS}
	deliveryContext, _ := json.Marshal(slackoutbound.DeliveryContext{Metadata: metadata})
	accepted, err := s.tunnel.SendAndMount(r.Context(), sessionID, "slack-command-"+triggerID,
		router.SendAndMountInput{
			Subscriber: router.Subscriber{
				ID:       "slack:" + sessionID,
				Mode:     router.Participant,
				Delivery: &router.DeliveryTarget{Activity: slackoutbound.DeliverA2AActivity, TaskQueue: s.deliveryTaskQueue, Context: deliveryContext},
			},
			Message: router.SendMessageInput{MessageType: "slash", Payload: map[string]any{"name": command, "arg": arg}},
		})
	if err != nil {
		log.Printf("Failed to submit slash command through UI tunnel: %v", err)
	}

	w.Header().Set("Content-Type", "application/json")
	if accepted.Reply != "" {
		_ = json.NewEncoder(w).Encode(map[string]string{"response_type": "ephemeral", "text": accepted.Reply})
		return
	}
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

		approval, _ := json.Marshal(map[string]any{"toolId": val.ToolID, "approved": val.Approved})
		if _, err := s.tunnel.Control(r.Context(), val.SessionID, "slack-approval-"+val.ToolID,
			router.ControlInput{Kind: "approve-tool-call", Payload: approval}); err != nil {
			log.Printf("handleInteractions: failed to resolve approval through UI tunnel: %v", err)
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
