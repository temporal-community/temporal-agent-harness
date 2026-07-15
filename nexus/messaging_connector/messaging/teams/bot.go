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
)

const (
	tokenURLFormat   = "https://login.microsoftonline.com/%s/oauth2/v2.0/token"
	defaultScope     = "https://api.botframework.com/.default"
	defaultChannelID = "msteams"
	tokenExpirySkew  = time.Minute
)

// TeamsBot holds authenticated Microsoft Bot Framework credentials.
type TeamsBot struct {
	Client *http.Client
	AppID  string

	appPassword string
	tokenURL    string
	scope       string

	mu          sync.Mutex
	accessToken string
	expiresAt   time.Time
}

// NewTeamsBot creates a TeamsBot from Microsoft tenant and app credentials.
func NewTeamsBot(tenantID, appID, appPassword string) (*TeamsBot, error) {
	tenantID = strings.TrimSpace(tenantID)
	if tenantID == "" {
		return nil, errors.New("Microsoft tenant ID is required")
	}
	appID = strings.TrimSpace(appID)
	if appID == "" {
		return nil, errors.New("Microsoft app ID is required")
	}
	if strings.TrimSpace(appPassword) == "" {
		return nil, errors.New("Microsoft app password is required")
	}
	return &TeamsBot{
		Client:      http.DefaultClient,
		AppID:       appID,
		appPassword: appPassword,
		tokenURL:    fmt.Sprintf(tokenURLFormat, url.PathEscape(tenantID)),
		scope:       defaultScope,
	}, nil
}

func (b *TeamsBot) bearerToken(ctx context.Context) (string, error) {
	if b == nil {
		return "", errors.New("Teams bot is required")
	}

	b.mu.Lock()
	defer b.mu.Unlock()

	now := time.Now()
	if b.accessToken != "" && now.Before(b.expiresAt.Add(-tokenExpirySkew)) {
		return b.accessToken, nil
	}

	token, expiresAt, err := b.fetchBearerToken(ctx)
	if err != nil {
		return "", err
	}
	b.accessToken = token
	b.expiresAt = expiresAt
	return b.accessToken, nil
}

func (b *TeamsBot) fetchBearerToken(ctx context.Context) (string, time.Time, error) {
	form := url.Values{}
	form.Set("grant_type", "client_credentials")
	form.Set("client_id", b.AppID)
	form.Set("client_secret", b.appPassword)
	form.Set("scope", b.scope)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", time.Time{}, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := b.httpClient().Do(req)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("fetch Teams bearer token: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return "", time.Time{}, fmt.Errorf("fetch Teams bearer token: status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var out tokenResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", time.Time{}, fmt.Errorf("decode Teams bearer token response: %w", err)
	}
	if out.AccessToken == "" {
		return "", time.Time{}, errors.New("Teams bearer token response missing access_token")
	}
	if out.ExpiresIn <= 0 {
		return "", time.Time{}, errors.New("Teams bearer token response missing expires_in")
	}
	return out.AccessToken, time.Now().Add(time.Duration(out.ExpiresIn) * time.Second), nil
}

func (b *TeamsBot) httpClient() *http.Client {
	if b.Client != nil {
		return b.Client
	}
	return http.DefaultClient
}

func activityURL(serviceURL, conversationID, replyToID string) (string, error) {
	segments := []string{"v3", "conversations", conversationID, "activities"}
	if replyToID != "" {
		segments = append(segments, replyToID)
	}
	endpoint, err := url.JoinPath(serviceURL, segments...)
	if err != nil {
		return "", fmt.Errorf("build Teams activity URL: %w", err)
	}
	return endpoint, nil
}

func (b *TeamsBot) postActivity(ctx context.Context, serviceURL, conversationID, replyToID, text string) (string, error) {
	resp, err := b.sendActivity(ctx, http.MethodPost, serviceURL, conversationID, replyToID, TeamMessageActivity{
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

func (b *TeamsBot) updateMessageActivity(ctx context.Context, serviceURL, conversationID, activityID, text string) error {
	_, err := b.sendActivity(ctx, http.MethodPut, serviceURL, conversationID, activityID, TeamMessageActivity{
		Type:       activityTypeMessage,
		Text:       text,
		TextFormat: "markdown",
	})
	return err
}

func (b *TeamsBot) sendStreamingActivity(ctx context.Context, serviceURL string, input streamingActivityInput) (string, error) {
	if b == nil {
		return "", errors.New("Teams bot is required")
	}
	resp, err := b.sendActivity(ctx, http.MethodPost, serviceURL, input.ConversationID, "", TeamMessageActivity{
		Type:       input.ActivityType,
		ServiceURL: serviceURL,
		ChannelID:  defaultChannelID,
		From: &TeamChannelAccount{
			ID: b.AppID,
		},
		Conversation: &TeamConversationAccount{
			ID: input.ConversationID,
		},
		Text: input.Text,
		Entities: []TeamStreamInfoEntity{{
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

func (b *TeamsBot) sendActivity(ctx context.Context, method, serviceURL, conversationID, replyToID string, act TeamMessageActivity) (resourceResponse, error) {
	endpoint, err := activityURL(serviceURL, conversationID, replyToID)
	if err != nil {
		return resourceResponse{}, err
	}
	token, err := b.bearerToken(ctx)
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

	resp, err := b.httpClient().Do(req)
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

type tokenResponse struct {
	AccessToken string `json:"access_token"`
	ExpiresIn   int64  `json:"expires_in"`
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
