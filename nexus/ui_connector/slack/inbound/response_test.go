package slackinbound

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/interaction"
	"go.temporal.io/sdk/mocks"
)

type responseTransport func(*http.Request) (*http.Response, error)

func (f responseTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestPostResponseUsesSlackHosts(t *testing.T) {
	for _, host := range []string{"hooks.slack.com", "hooks.slack-gov.com"} {
		t.Run(host, func(t *testing.T) {
			server := NewServer(nil, "", "", Options{})
			called := false
			server.responseClient.Transport = responseTransport(func(r *http.Request) (*http.Response, error) {
				called = true
				assert.Equal(t, "https://"+host+"/actions/T1/B1/token", r.URL.String())
				assert.Equal(t, http.MethodPost, r.Method)
				assert.Equal(t, "application/json", r.Header.Get("Content-Type"))
				body, err := io.ReadAll(r.Body)
				require.NoError(t, err)
				assert.JSONEq(t, `{"replace_original":true}`, string(body))
				deadline, ok := r.Context().Deadline()
				assert.True(t, ok)
				assert.LessOrEqual(t, time.Until(deadline), 2*time.Second)
				return &http.Response{StatusCode: http.StatusOK, Body: http.NoBody}, nil
			})
			require.NoError(t, server.postResponse(context.Background(), "https://"+host+"/actions/T1/B1/token", []byte(`{"replace_original":true}`)))
			assert.True(t, called)
		})
	}
}

func TestPostResponseRejectsOtherDestinations(t *testing.T) {
	for _, target := range []string{
		"http://hooks.slack.com/actions/token",
		"https://127.0.0.1/actions/token",
		"http://169.254.169.254/latest/meta-data/",
		"https://localhost/actions/token",
		"https://hooks.slack.com.example.com/actions/token",
		"https://example.com/hooks.slack.com/actions/token",
		"https://hooks.slack.com@evil.example/actions/token",
		"https://user@hooks.slack.com/actions/token",
		"https://hooks.slack.com:8443/actions/token",
		"https://hooks.slack.com./actions/token",
		"https://hooks.slack.com/actions/token#fragment",
		"//hooks.slack.com/actions/token",
		"https://hooks.slack.com%2Fevil.example/actions/token",
		"file:///etc/passwd",
		"%",
	} {
		t.Run(target, func(t *testing.T) {
			server := NewServer(nil, "", "", Options{})
			server.responseClient.Transport = responseTransport(func(*http.Request) (*http.Response, error) {
				t.Fatal("invalid destination reached the HTTP transport")
				return nil, nil
			})
			assert.Error(t, server.postResponse(context.Background(), target, nil))
		})
	}
}

func TestPostResponseDoesNotFollowRedirects(t *testing.T) {
	server := NewServer(nil, "", "", Options{})
	calls := 0
	server.responseClient.Transport = responseTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		assert.Equal(t, "hooks.slack.com", r.URL.Host)
		return &http.Response{
			StatusCode: http.StatusTemporaryRedirect,
			Header:     http.Header{"Location": {"http://127.0.0.1/private"}},
			Body:       http.NoBody,
		}, nil
	})
	assert.ErrorContains(t, server.postResponse(context.Background(), "https://hooks.slack.com/actions/token", nil), "HTTP 307")
	assert.Equal(t, 1, calls)
}

func TestPostResponseTimeoutAndCancellation(t *testing.T) {
	for _, canceled := range []bool{false, true} {
		t.Run(fmt.Sprintf("canceled=%v", canceled), func(t *testing.T) {
			server := NewServer(nil, "", "", Options{})
			server.responseClient.Timeout = 20 * time.Millisecond
			server.responseClient.Transport = responseTransport(func(r *http.Request) (*http.Response, error) {
				<-r.Context().Done()
				return nil, r.Context().Err()
			})
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if canceled {
				cancel()
			}
			start := time.Now()
			err := server.postResponse(ctx, "https://hooks.slack.com/actions/secret-token", nil)
			require.Error(t, err)
			assert.NotContains(t, err.Error(), "secret-token")
			assert.Less(t, time.Since(start), time.Second)
			if canceled {
				assert.ErrorIs(t, err, context.Canceled)
			} else {
				assert.ErrorIs(t, err, context.DeadlineExceeded)
			}
		})
	}
}

func TestApprovalStillStartsWhenResponseURLIsRejected(t *testing.T) {
	mockClient := mocks.NewClient(t)
	got := expectInput(mockClient, "connector-default-slack:C1:1.0-approval-t1")
	server := NewServer(mockClient, "task-queue", "", Options{})
	server.responseClient.Transport = responseTransport(func(*http.Request) (*http.Response, error) {
		t.Fatal("invalid destination reached the HTTP transport")
		return nil, nil
	})
	postInteraction(t, server, map[string]any{
		"type": "block_actions", "user": map[string]any{"id": "U9"},
		"response_url": "http://127.0.0.1/private",
		"actions": []any{map[string]any{
			"block_id":  "tool_approval",
			"action_id": interaction.ActionApprove,
			"value":     `{"s":"slack:C1:1.0","t":"t1","n":"search","a":true}`,
		}},
	})
	require.NotNil(t, got.Request.Approval)
	assert.True(t, got.Request.Approval.Approved)
}

func TestPostResponseReportsSlackErrors(t *testing.T) {
	server := NewServer(nil, "", "", Options{})
	server.responseClient.Transport = responseTransport(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusTooManyRequests, Body: io.NopCloser(strings.NewReader("rate limited"))}, nil
	})
	assert.ErrorContains(t, server.postResponse(context.Background(), "https://hooks.slack.com/actions/token", nil), "HTTP 429")
}
