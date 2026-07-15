package teams

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/slack_connector/messaging"
)

const (
	defaultServiceURL    = "https://smba.trafficmanager.net/teams/"
	defaultChannelID     = "msteams"
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
		return p.updateMessageActivity(ctx, conversationID, input.Handle.ID, input.FullText)
	}
	if input.Handle.TransportMode != streamModeNative {
		return fmt.Errorf("unknown Teams stream transport mode %q", input.Handle.TransportMode)
	}
	if input.Sequence <= initialStreamSequence {
		return fmt.Errorf("Teams stream update sequence must be greater than %d", initialStreamSequence)
	}
	sequence := input.Sequence
	_, err = p.sendStreamingActivity(ctx, streamingActivityInput{
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
		return p.updateMessageActivity(ctx, conversationID, input.Handle.ID, input.FullText)
	}
	if input.Handle.TransportMode != streamModeNative {
		return fmt.Errorf("unknown Teams stream transport mode %q", input.Handle.TransportMode)
	}
	_, err = p.sendStreamingActivity(ctx, streamingActivityInput{
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
	streamID, err := p.sendStreamingActivity(ctx, streamingActivityInput{
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
	streamID, err := p.postActivity(ctx, conversationID, threadID, displayText)
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

	_, err = p.postActivity(ctx, conversationID, input.ThreadID, input.Text)
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
	endpoint, err := p.activityURL(conversationID, input.ThreadID)
	if err != nil {
		return err
	}
	_, err = p.sendActivity(ctx, http.MethodPost, endpoint, msgiface.TeamMessageActivity{
		Type: activityTypeMessage,
		Attachments: []msgiface.TeamAttachment{{
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
	endpoint, err := p.activityURL(conversationID, activityID)
	if err != nil {
		return err
	}
	_, err = p.sendActivity(ctx, http.MethodPut, endpoint, msgiface.TeamMessageActivity{
		Type:       activityTypeMessage,
		Text:       text,
		TextFormat: "markdown",
	})
	return err
}

func parseConversation(sessionID string) (string, error) {
	provider, conversationID, found := strings.Cut(sessionID, ":")
	if !found || provider != "teams" || conversationID == "" {
		return "", fmt.Errorf("invalid session ID %q: expected \"teams:<conversationID>\" format", sessionID)
	}
	return conversationID, nil
}

func (p *TeamsPlatform) activityURL(conversationID, replyToID string) (string, error) {
	segments := []string{"v3", "conversations", conversationID, "activities"}
	if replyToID != "" {
		segments = append(segments, replyToID)
	}
	endpoint, err := url.JoinPath(p.serviceURL, segments...)
	if err != nil {
		return "", fmt.Errorf("build Teams activity URL: %w", err)
	}
	return endpoint, nil
}

func (p *TeamsPlatform) postActivity(ctx context.Context, conversationID, replyToID, text string) (string, error) {
	endpoint, err := p.activityURL(conversationID, replyToID)
	if err != nil {
		return "", err
	}
	resp, err := p.sendActivity(ctx, http.MethodPost, endpoint, msgiface.TeamMessageActivity{
		Type:       activityTypeMessage,
		Text:       text,
		TextFormat: "markdown",
	})
	if err != nil {
		return "", err
	}
	if resp.ID == "" {
		return "", errors.New("Teams activity response missing id")
	}
	return resp.ID, nil
}

func (p *TeamsPlatform) updateMessageActivity(ctx context.Context, conversationID, activityID, text string) error {
	endpoint, err := p.activityURL(conversationID, activityID)
	if err != nil {
		return err
	}
	_, err = p.sendActivity(ctx, http.MethodPut, endpoint, msgiface.TeamMessageActivity{
		Type:       activityTypeMessage,
		Text:       text,
		TextFormat: "markdown",
	})
	return err
}

func (p *TeamsPlatform) sendStreamingActivity(ctx context.Context, input streamingActivityInput) (string, error) {
	if p.bot == nil {
		return "", errors.New("Teams bot is required")
	}
	endpoint, err := p.activityURL(input.ConversationID, "")
	if err != nil {
		return "", err
	}
	resp, err := p.sendActivity(ctx, http.MethodPost, endpoint, msgiface.TeamMessageActivity{
		Type:       input.ActivityType,
		ServiceURL: p.serviceURL,
		ChannelID:  defaultChannelID,
		From: &msgiface.TeamChannelAccount{
			ID: p.bot.AppID,
		},
		Conversation: &msgiface.TeamConversationAccount{
			ID: input.ConversationID,
		},
		Text: input.Text,
		Entities: []msgiface.TeamStreamInfoEntity{{
			Type:           streamInfoType,
			StreamID:       input.StreamID,
			StreamType:     input.StreamType,
			StreamSequence: input.StreamSequence,
		}},
	})
	if err != nil {
		return "", err
	}
	return resp.ID, nil
}

func (p *TeamsPlatform) sendActivity(ctx context.Context, method, endpoint string, act msgiface.TeamMessageActivity) (resourceResponse, error) {
	if p.bot == nil {
		return resourceResponse{}, errors.New("Teams bot is required")
	}
	token, err := p.bot.bearerToken(ctx)
	if err != nil {
		return resourceResponse{}, err
	}

	body, err := json.Marshal(act)
	if err != nil {
		return resourceResponse{}, err
	}

	req, err := http.NewRequestWithContext(ctx, method, endpoint, bytes.NewReader(body))
	if err != nil {
		return resourceResponse{}, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := p.bot.httpClient().Do(req)
	if err != nil {
		return resourceResponse{}, fmt.Errorf("send Teams activity: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return resourceResponse{}, &teamsHTTPError{
			StatusCode: resp.StatusCode,
			Body:       strings.TrimSpace(string(respBody)),
		}
	}

	var out resourceResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil && !errors.Is(err, io.EOF) {
		return resourceResponse{}, fmt.Errorf("decode Teams activity response: %w", err)
	}
	return out, nil
}

type teamsHTTPError struct {
	StatusCode int
	Body       string
}

func (e *teamsHTTPError) Error() string {
	return fmt.Sprintf("send Teams activity: status %d: %s", e.StatusCode, e.Body)
}

type resourceResponse struct {
	ID string `json:"id"`
}

type streamingActivityInput struct {
	ConversationID string
	ActivityType   string
	Text           string
	StreamID       string
	StreamType     string
	StreamSequence *int
}
