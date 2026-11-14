package linearoutbound

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Client struct {
	HTTPClient    *http.Client
	Endpoint      string
	TokenEndpoint string

	clientID     string
	clientSecret string
	tokenLock    chan struct{}
	token        string
	expiresAt    time.Time
}

func NewClient(clientID, clientSecret string) *Client {
	return &Client{
		clientID: clientID, clientSecret: clientSecret,
		tokenLock:     make(chan struct{}, 1),
		HTTPClient:    &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
		Endpoint:      "https://api.linear.app/graphql",
		TokenEndpoint: "https://api.linear.app/oauth/token",
	}
}

// accessToken caches the app token and replaces it one minute before expiry.
func (c *Client) accessToken(ctx context.Context) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if strings.TrimSpace(c.clientID) == "" || strings.TrimSpace(c.clientSecret) == "" {
		return "", errors.New("Linear client ID and secret are required")
	}
	select {
	case c.tokenLock <- struct{}{}:
		defer func() { <-c.tokenLock }()
	case <-ctx.Done():
		return "", ctx.Err()
	}
	if c.token != "" && time.Now().Before(c.expiresAt.Add(-time.Minute)) {
		return c.token, nil
	}
	// https://linear.app/developers/oauth-2-0-authentication#client-credentials-tokens
	form := url.Values{
		"grant_type": {"client_credentials"}, "client_id": {c.clientID},
		"client_secret": {c.clientSecret}, "scope": {"read,write,app:mentionable"},
	}
	var result struct {
		AccessToken string `json:"access_token"`
		TokenType   string `json:"token_type"`
		ExpiresIn   int64  `json:"expires_in"`
	}
	if err := c.post(ctx, c.TokenEndpoint, "application/x-www-form-urlencoded", "", []byte(form.Encode()), &result); err != nil {
		return "", err
	}
	if strings.TrimSpace(result.AccessToken) == "" || !strings.EqualFold(result.TokenType, "Bearer") || result.ExpiresIn <= 0 {
		return "", errors.New("invalid Linear token response")
	}
	c.token, c.expiresAt = result.AccessToken, time.Now().Add(time.Duration(result.ExpiresIn)*time.Second)
	return c.token, nil
}

// invalidateToken leaves a replacement fetched by another request intact.
func (c *Client) invalidateToken(ctx context.Context, rejected string) error {
	select {
	case c.tokenLock <- struct{}{}:
		defer func() { <-c.tokenLock }()
	case <-ctx.Done():
		return ctx.Err()
	}
	if c.token == rejected {
		c.token = ""
		c.expiresAt = time.Time{}
	}
	return nil
}

// https://linear.app/developers/agent-interaction#activity-content-payload
const (
	activityTypeThought  = "thought"
	activityTypeResponse = "response"
	activityTypeError    = "error"
)

type activityContent struct {
	Type string `json:"type"`
	Body string `json:"body"`
}

func (c *Client) createActivity(ctx context.Context, sessionID string, content activityContent, ephemeral bool) error {
	// https://linear.app/developers/agent-interaction#sending-agent-activities
	// https://linear.app/developers/agent-interaction#ephemeral-activities
	const mutation = `mutation ConnectorActivity($input: AgentActivityCreateInput!) {
		agentActivityCreate(input: $input) { success }
	}`
	body, err := json.Marshal(map[string]any{
		"query": mutation,
		"variables": map[string]any{"input": map[string]any{
			"agentSessionId": sessionID, "content": content, "ephemeral": ephemeral,
		}},
	})
	if err != nil {
		return fmt.Errorf("encode Linear activity: %w", err)
	}
	for attempt := 0; ; attempt++ {
		token, err := c.accessToken(ctx)
		if err != nil {
			return err
		}
		var result struct {
			Data struct {
				Create struct {
					Success bool `json:"success"`
				} `json:"agentActivityCreate"`
			} `json:"data"`
			Errors []json.RawMessage `json:"errors"`
		}
		err = c.post(ctx, c.Endpoint, "application/json", token, body, &result)
		var statusErr *httpStatusError
		if errors.As(err, &statusErr) && statusErr.code == http.StatusUnauthorized {
			if err := c.invalidateToken(ctx, token); err != nil {
				return err
			}
			// Linear requires a new token after a 401. Retry once.
			// https://linear.app/developers/oauth-2-0-authentication#client-credentials-tokens
			if attempt == 0 {
				continue
			}
		}
		if err != nil {
			return err
		}
		if len(result.Errors) > 0 || !result.Data.Create.Success {
			return errors.New("Linear agentActivityCreate failed")
		}
		return nil
	}
}

type httpStatusError struct {
	code int
}

func (e *httpStatusError) Error() string {
	return fmt.Sprintf("Linear request failed (HTTP %d)", e.code)
}

func (c *Client) post(ctx context.Context, endpoint, contentType, token string, body []byte, result any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return errors.New("construct Linear request failed")
	}
	req.Header.Set("Content-Type", contentType)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return errors.New("Linear HTTP request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return &httpStatusError{code: resp.StatusCode}
	}
	if err := json.NewDecoder(resp.Body).Decode(result); err != nil {
		return errors.New("invalid Linear response")
	}
	return nil
}
