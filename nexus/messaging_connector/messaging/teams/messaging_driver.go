package teams

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging"
)

const (
	defaultServiceURL    = "https://smba.trafficmanager.net/teams/"
	initialStreamingText = "Thinking..."

	activityTypeTyping  = "typing"
	activityTypeMessage = "message"

	streamInfoType        = "streaminfo"
	streamTypeInformative = "informative"
	streamTypeStreaming   = "streaming"
	streamTypeFinal       = "final"

	initialStreamSequence   = 1
	minStreamUpdateDelay    = 1500 * time.Millisecond
	streamModeNative        = "native"
	streamModeMessageUpdate = "message-update"
)

// Compile-time check that TeamsPlatform implements MessagingPlatform.
var _ msgiface.MessagingPlatform = (*TeamsPlatform)(nil)

// TeamsPlatform implements MessagingPlatform using the Bot Framework REST connector.
type TeamsPlatform struct {
	bot        *TeamsBot
	serviceURL string
}

// TeamMessageActivity is the Bot Framework Activity JSON shape used by Teams
// webhooks and outbound Teams message sends.
type TeamMessageActivity struct {
	Type         string                   `json:"type"`
	ID           string                   `json:"id,omitempty"`
	ReplyToID    string                   `json:"replyToId,omitempty"`
	Timestamp    string                   `json:"timestamp,omitempty"`
	ServiceURL   string                   `json:"serviceUrl,omitempty"`
	ChannelID    string                   `json:"channelId,omitempty"`
	From         *TeamChannelAccount      `json:"from,omitempty"`
	Conversation *TeamConversationAccount `json:"conversation,omitempty"`
	Recipient    *TeamChannelAccount      `json:"recipient,omitempty"`
	Text         string                   `json:"text,omitempty"`
	TextFormat   string                   `json:"textFormat,omitempty"`
	// Value carries an Adaptive Card Action.Submit's data object on incoming
	// button-click activities (delivered as type "message" with empty text).
	Value       json.RawMessage        `json:"value,omitempty"`
	Attachments []TeamAttachment       `json:"attachments,omitempty"`
	Entities    []TeamStreamInfoEntity `json:"entities,omitempty"`
}

// TeamAttachment is a Bot Framework Attachment, used to send rich cards
// (e.g. Adaptive Cards) as part of a Teams activity.
type TeamAttachment struct {
	ContentType string          `json:"contentType"`
	Content     json.RawMessage `json:"content,omitempty"`
}

type TeamChannelAccount struct {
	ID string `json:"id,omitempty"`
}

type TeamConversationAccount struct {
	ID               string `json:"id,omitempty"`
	ConversationType string `json:"conversationType,omitempty"`
}

type TeamStreamInfoEntity struct {
	Type           string `json:"type"`
	StreamID       string `json:"streamId,omitempty"`
	StreamType     string `json:"streamType,omitempty"`
	StreamSequence *int   `json:"streamSequence,omitempty"`
}

// NewTeamsPlatform creates a TeamsPlatform from a Teams bot and Bot Framework service URL.
func NewTeamsPlatform(bot *TeamsBot, serviceURL string) *TeamsPlatform {
	serviceURL = strings.TrimSpace(serviceURL)
	if serviceURL == "" {
		serviceURL = defaultServiceURL
	}
	return &TeamsPlatform{
		bot:        bot,
		serviceURL: serviceURL,
	}
}

// BeginStream starts a stateless Teams response. Personal chats use Teams'
// native streaming protocol. Channels and group chats use one ordinary message
// followed by updates because native streaming is unavailable in those scopes.
func (p *TeamsPlatform) BeginStream(ctx context.Context, input msgiface.BeginStreamInput) (msgiface.StreamHandle, error) {
	conversationID, err := parseConversation(input.SessionID)
	if err != nil {
		return msgiface.StreamHandle{}, err
	}

	mode := streamModeNative
	var streamID string
	if !isStreamingAllowed(input.ConversationType) {
		mode = streamModeMessageUpdate
		streamID, err = p.beginMessageUpdates(ctx, conversationID, input.ThreadID, input.Text)
	} else {
		streamID, err = p.beginNativeStream(ctx, conversationID, input.Text)
		// Older webhook payloads can omit conversationType. If Teams explicitly
		// rejects native streaming for a non-personal scope, fall back to an
		// ordinary message whose activity can be updated.
		if err != nil && strings.TrimSpace(input.ConversationType) == "" && isNonPersonalStreamingError(err) {
			mode = streamModeMessageUpdate
			streamID, err = p.beginMessageUpdates(ctx, conversationID, input.ThreadID, input.Text)
		}
	}
	if err != nil {
		return msgiface.StreamHandle{}, err
	}

	nextSequence := 0
	if mode == streamModeNative {
		nextSequence = initialStreamSequence + 1
	}
	return msgiface.StreamHandle{
		ID:                  streamID,
		SessionID:           input.SessionID,
		TransportMode:       mode,
		WireTextMode:        msgiface.StreamWireTextFullText,
		MinUpdateInterval:   minStreamUpdateDelay,
		CloseBeforeApproval: true,
		NextSequence:        nextSequence,
	}, nil
}

// UpdateStream sends the latest cumulative text. Agent output arrives as
// deltas, but Teams REST requires every update to contain prior streamed text.
func (p *TeamsPlatform) UpdateStream(ctx context.Context, input msgiface.UpdateStreamInput) error {
	conversationID, err := p.validateStreamInput(input.SessionID, input.Handle)
	if err != nil {
		return err
	}
	if input.FullText == "" {
		return nil
	}
	if input.Handle.TransportMode == streamModeMessageUpdate {
		return p.bot.updateMessageActivity(ctx, p.serviceURL, conversationID, input.Handle.ID, input.FullText)
	}
	if input.Handle.TransportMode != streamModeNative {
		return fmt.Errorf("unknown Teams stream transport mode %q", input.Handle.TransportMode)
	}
	if input.Sequence <= initialStreamSequence {
		return fmt.Errorf("Teams stream update sequence must be greater than %d", initialStreamSequence)
	}
	sequence := input.Sequence
	_, err = p.bot.sendStreamingActivity(ctx, p.serviceURL, streamingActivityInput{
		ConversationID: conversationID,
		ActivityType:   activityTypeTyping,
		Text:           input.FullText,
		StreamID:       input.Handle.ID,
		StreamType:     streamTypeStreaming,
		StreamSequence: &sequence,
	})
	return err
}

// FinishStream finalises the response using the workflow-owned full text.
func (p *TeamsPlatform) FinishStream(ctx context.Context, input msgiface.FinishStreamInput) error {
	conversationID, err := p.validateStreamInput(input.SessionID, input.Handle)
	if err != nil {
		return err
	}
	if input.Handle.TransportMode == streamModeMessageUpdate {
		return p.bot.updateMessageActivity(ctx, p.serviceURL, conversationID, input.Handle.ID, input.FullText)
	}
	if input.Handle.TransportMode != streamModeNative {
		return fmt.Errorf("unknown Teams stream transport mode %q", input.Handle.TransportMode)
	}
	_, err = p.bot.sendStreamingActivity(ctx, p.serviceURL, streamingActivityInput{
		ConversationID: conversationID,
		ActivityType:   activityTypeMessage,
		Text:           input.FullText,
		StreamID:       input.Handle.ID,
		StreamType:     streamTypeFinal,
	})
	return err
}

func (p *TeamsPlatform) validateStreamInput(sessionID string, handle msgiface.StreamHandle) (string, error) {
	if handle.ID == "" {
		return "", errors.New("stream handle ID is required")
	}
	if handle.SessionID != sessionID {
		return "", errors.New("Teams stream handle session does not match input session")
	}
	return parseConversation(sessionID)
}

func isStreamingAllowed(conversationType string) bool {
	switch strings.ToLower(strings.TrimSpace(conversationType)) {
	case "channel", "groupchat":
		return false
	default:
		return true
	}
}

func isNonPersonalStreamingError(err error) bool {
	var httpErr *teamsHTTPError
	if !errors.As(err, &httpErr) || httpErr.StatusCode != http.StatusMethodNotAllowed {
		return false
	}
	body := strings.ToLower(httpErr.Body)
	return strings.Contains(body, "streaming") && strings.Contains(body, "non personal")
}

func (p *TeamsPlatform) beginNativeStream(ctx context.Context, conversationID, text string) (string, error) {
	displayText := text
	if strings.TrimSpace(displayText) == "" {
		displayText = initialStreamingText
	}
	sequence := initialStreamSequence
	streamID, err := p.bot.sendStreamingActivity(ctx, p.serviceURL, streamingActivityInput{
		ConversationID: conversationID,
		ActivityType:   activityTypeTyping,
		Text:           displayText,
		StreamType:     streamTypeInformative,
		StreamSequence: &sequence,
	})
	if err != nil {
		return "", err
	}
	if streamID == "" {
		return "", errors.New("Teams initial streaming response missing stream id")
	}
	return streamID, nil
}

func (p *TeamsPlatform) beginMessageUpdates(ctx context.Context, conversationID, threadID, text string) (string, error) {
	displayText := text
	if strings.TrimSpace(displayText) == "" {
		displayText = initialStreamingText
	}
	streamID, err := p.bot.postActivity(ctx, p.serviceURL, conversationID, threadID, displayText)
	if err != nil {
		return "", err
	}
	return streamID, nil
}

func (p *TeamsPlatform) PostMessage(ctx context.Context, input msgiface.TextMetadata) error {
	if p.bot == nil {
		return errors.New("Teams bot is required")
	}
	conversationID, err := parseConversation(input.SessionID)
	if err != nil {
		return err
	}
	if strings.TrimSpace(input.Text) == "" {
		return errors.New("text is required")
	}

	_, err = p.bot.postActivity(ctx, p.serviceURL, conversationID, input.ThreadID, input.Text)
	return err
}

// PostApprovalPrompt posts an Adaptive Card with Approve/Deny buttons.
// The decision comes back to the webhook as a message activity whose value
// field carries the clicked button's ApprovalButtonValue.
func (p *TeamsPlatform) PostApprovalPrompt(ctx context.Context, input msgiface.ApprovalPromptInput) error {
	conversationID, err := parseConversation(input.SessionID)
	if err != nil {
		return err
	}
	card, err := buildApprovalCard(input)
	if err != nil {
		return fmt.Errorf("build Teams approval card: %w", err)
	}
	_, err = p.bot.sendActivity(ctx, http.MethodPost, p.serviceURL, conversationID, input.ThreadID, TeamMessageActivity{
		Type: activityTypeMessage,
		Attachments: []TeamAttachment{{
			ContentType: adaptiveCardContentType,
			Content:     card,
		}},
	})
	return err
}

// UpdateActivity replaces an existing activity's content, e.g. to swap an
// approval card for its outcome so the buttons can't be clicked again.
func (p *TeamsPlatform) UpdateActivity(ctx context.Context, sessionID, activityID, text string) error {
	if activityID == "" {
		return errors.New("activity ID is required")
	}
	conversationID, err := parseConversation(sessionID)
	if err != nil {
		return err
	}
	return p.bot.updateMessageActivity(ctx, p.serviceURL, conversationID, activityID, text)
}

func parseConversation(sessionID string) (string, error) {
	provider, conversationID, found := strings.Cut(sessionID, ":")
	if !found || provider != "teams" || conversationID == "" {
		return "", fmt.Errorf("invalid session ID %q: expected \"teams:<conversationID>\" format", sessionID)
	}
	return conversationID, nil
}
