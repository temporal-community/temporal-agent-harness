package teams

import (
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
	tokenURLFormat  = "https://login.microsoftonline.com/%s/oauth2/v2.0/token"
	defaultScope    = "https://api.botframework.com/.default"
	tokenExpirySkew = time.Minute
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

type tokenResponse struct {
	AccessToken string `json:"access_token"`
	ExpiresIn   int64  `json:"expires_in"`
}
