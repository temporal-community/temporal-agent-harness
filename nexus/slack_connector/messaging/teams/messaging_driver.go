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
	"sync"
	"time"

	msgiface "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging"
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

	initialStreamSequence = 1
	minStreamUpdateDelay  = time.Second
)

// Compile-time check that TeamsPlatform implements MessagingPlatform.
var _ msgiface.MessagingPlatform = (*TeamsPlatform)(nil)

// TeamsPlatform implements MessagingPlatform using the Bot Framework REST connector.
type TeamsPlatform struct {
	bot        *TeamsBot
	serviceURL string

	mu      sync.Mutex
	streams map[string]streamState
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
		streams:    make(map[string]streamState),
	}
}

// Stream starts, appends to, or finalises a Teams streaming message.
// Teams has no server-side append, so accumulated text is tracked per stream
// and each update re-sends the full text, throttled to one update per
// minStreamUpdateDelay as required by the Bot Framework.
func (p *TeamsPlatform) Stream(ctx context.Context, input msgiface.StreamInput) (string, error) {
	conversationID, err := parseConversation(input.SessionID)
	if err != nil {
		return "", err
	}
	if input.DeltaType != msgiface.DeltaTypeStart && input.StreamID == "" {
		return "", errors.New("StreamID is required for Append and End phases")
	}

	switch input.DeltaType {
	case msgiface.DeltaTypeStart:
		return p.startStream(ctx, conversationID, input.Text)
	case msgiface.DeltaTypeAppend:
		if err := p.appendStream(ctx, conversationID, input.StreamID, input.Text); err != nil {
			return "", err
		}
		return input.StreamID, nil
	case msgiface.DeltaTypeEnd:
		if err := p.endStream(ctx, conversationID, input.StreamID); err != nil {
			return "", err
		}
		return input.StreamID, nil
	default:
		return "", fmt.Errorf("unknown delta type: %d", input.DeltaType)
	}
}

func (p *TeamsPlatform) startStream(ctx context.Context, conversationID, text string) (string, error) {
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

	p.mu.Lock()
	defer p.mu.Unlock()
	if p.streams == nil {
		p.streams = make(map[string]streamState)
	}
	// Track the original text, not the placeholder: the placeholder is an
	// informative status message, not part of the streamed response.
	p.streams[streamID] = streamState{
		ConversationID: conversationID,
		Text:           text,
		NextSequence:   initialStreamSequence + 1,
		LastSentAt:     time.Now(),
	}
	return streamID, nil
}

func (p *TeamsPlatform) appendStream(ctx context.Context, conversationID, streamID, delta string) error {
	if delta == "" {
		return nil
	}
	text, sequence, send, err := p.bufferDelta(conversationID, streamID, delta)
	if err != nil || !send {
		return err
	}
	_, err = p.sendStreamingActivity(ctx, streamingActivityInput{
		ConversationID: conversationID,
		ActivityType:   activityTypeTyping,
		Text:           text,
		StreamID:       streamID,
		StreamType:     streamTypeStreaming,
		StreamSequence: &sequence,
	})
	return err
}

func (p *TeamsPlatform) endStream(ctx context.Context, conversationID, streamID string) error {
	p.mu.Lock()
	state, err := p.lookupStreamLocked(conversationID, streamID)
	p.mu.Unlock()
	if err != nil {
		return err
	}

	if _, err := p.sendStreamingActivity(ctx, streamingActivityInput{
		ConversationID: conversationID,
		ActivityType:   activityTypeMessage,
		Text:           state.Text,
		StreamID:       streamID,
		StreamType:     streamTypeFinal,
	}); err != nil {
		return err
	}

	p.mu.Lock()
	delete(p.streams, streamID)
	p.mu.Unlock()
	return nil
}

// bufferDelta appends delta to the stream's accumulated text and decides
// whether to send an update now. It returns send=false when the update is
// throttled; buffered text is flushed by the next unthrottled Append or by End.
func (p *TeamsPlatform) bufferDelta(conversationID, streamID, delta string) (text string, sequence int, send bool, err error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	state, err := p.lookupStreamLocked(conversationID, streamID)
	if err != nil {
		return "", 0, false, err
	}
	state.Text += delta
	now := time.Now()
	if now.Sub(state.LastSentAt) >= minStreamUpdateDelay {
		send = true
		sequence = state.NextSequence
		state.NextSequence++
		state.LastSentAt = now
	}
	p.streams[streamID] = state
	return state.Text, sequence, send, nil
}

// lookupStreamLocked returns the state for streamID, validating that it
// belongs to conversationID. The caller must hold p.mu.
func (p *TeamsPlatform) lookupStreamLocked(conversationID, streamID string) (streamState, error) {
	state, ok := p.streams[streamID]
	if !ok {
		return streamState{}, fmt.Errorf("unknown Teams stream ID %q", streamID)
	}
	if state.ConversationID != conversationID {
		return streamState{}, errors.New("Teams stream ID conversation does not match session ID")
	}
	return state, nil
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
		return resourceResponse{}, fmt.Errorf("send Teams activity: status %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
	}

	var out resourceResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil && !errors.Is(err, io.EOF) {
		return resourceResponse{}, fmt.Errorf("decode Teams activity response: %w", err)
	}
	return out, nil
}

type resourceResponse struct {
	ID string `json:"id"`
}

type streamState struct {
	ConversationID string
	Text           string
	NextSequence   int
	LastSentAt     time.Time
}

type streamingActivityInput struct {
	ConversationID string
	ActivityType   string
	Text           string
	StreamID       string
	StreamType     string
	StreamSequence *int
}
