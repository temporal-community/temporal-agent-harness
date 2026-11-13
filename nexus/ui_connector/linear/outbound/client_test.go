package linearoutbound

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestClientCachesAndRenewsAppToken(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, http.MethodPost, r.Method)
		assert.Equal(t, "application/x-www-form-urlencoded", r.Header.Get("Content-Type"))
		assert.Empty(t, r.Header.Get("Authorization"))
		if !assert.NoError(t, r.ParseForm()) {
			return
		}
		assert.Equal(t, "client_credentials", r.Form.Get("grant_type"))
		assert.Equal(t, "client-id", r.Form.Get("client_id"))
		assert.Equal(t, "client-secret", r.Form.Get("client_secret"))
		assert.Equal(t, "read,write,app:mentionable", r.Form.Get("scope"))
		_, _ = fmt.Fprintf(w, `{"access_token":"token-%d","token_type":"Bearer","expires_in":2592000}`, calls.Add(1))
	}))
	defer srv.Close()
	c := NewClient("client-id", "client-secret")
	c.TokenEndpoint = srv.URL
	for range 2 {
		token, err := c.accessToken(context.Background())
		require.NoError(t, err)
		assert.Equal(t, "token-1", token)
	}
	assert.EqualValues(t, 1, calls.Load())
	assert.WithinDuration(t, time.Now().Add(30*24*time.Hour), c.expiresAt, time.Second)

	// Renew before expiry using the same credentials; no refresh token is needed.
	c.expiresAt = time.Now().Add(30 * time.Second)
	token, err := c.accessToken(context.Background())
	require.NoError(t, err)
	assert.Equal(t, "token-2", token)
	assert.EqualValues(t, 2, calls.Load())
}

func TestConcurrentRequestsShareTokenFetch(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		_, _ = w.Write([]byte(`{"access_token":"token","token_type":"Bearer","expires_in":2592000}`))
	}))
	defer srv.Close()
	c := NewClient("id", "secret")
	c.TokenEndpoint = srv.URL
	results := make(chan error, 10)
	for range 10 {
		go func() {
			_, err := c.accessToken(context.Background())
			results <- err
		}()
	}
	for range 10 {
		require.NoError(t, <-results)
	}
	assert.EqualValues(t, 1, calls.Load())
}

func TestTokenFetchFailuresAreNotCached(t *testing.T) {
	for _, tc := range []struct {
		name     string
		status   int
		response string
	}{
		{"unauthorized", 401, "private-secret"},
		{"invalid JSON", 200, "private-secret"},
		{"empty token", 200, `{"access_token":" ","token_type":"Bearer","expires_in":2592000}`},
		{"wrong type", 200, `{"access_token":"private-secret","token_type":"Basic","expires_in":2592000}`},
		{"expired", 200, `{"access_token":"private-secret","token_type":"Bearer","expires_in":0}`},
		{"negative lifetime", 200, `{"access_token":"private-secret","token_type":"Bearer","expires_in":-1}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var calls atomic.Int32
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls.Add(1)
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.response))
			}))
			defer srv.Close()
			c := NewClient("id", "private-secret")
			c.TokenEndpoint = srv.URL
			for range 2 {
				token, err := c.accessToken(context.Background())
				require.Error(t, err)
				assert.NotContains(t, err.Error(), "private-secret")
				assert.Empty(t, token)
			}
			assert.EqualValues(t, 2, calls.Load())
			assert.Empty(t, c.token)
		})
	}
}

func TestClientRequiresCredentials(t *testing.T) {
	for _, pair := range [][2]string{{"", "secret"}, {"id", ""}, {" ", "secret"}} {
		c := NewClient(pair[0], pair[1])
		_, err := c.accessToken(context.Background())
		require.ErrorContains(t, err, "client ID and secret are required")
	}
}

func TestTokenWaitCanBeCancelled(t *testing.T) {
	c := NewClient("id", "secret")
	c.tokenLock <- struct{}{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := c.accessToken(ctx)
	require.ErrorIs(t, err, context.Canceled)
}

func TestActivityRecoversFromRejectedCachedToken(t *testing.T) {
	var tokenCalls atomic.Int32
	var tokens, bodies []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/oauth/token" {
			tokenCalls.Add(1)
			_, _ = w.Write([]byte(`{"access_token":"replacement","token_type":"Bearer","expires_in":2592000}`))
			return
		}
		body, err := io.ReadAll(r.Body)
		assert.NoError(t, err)
		tokens = append(tokens, r.Header.Get("Authorization"))
		bodies = append(bodies, string(body))
		if r.Header.Get("Authorization") == "Bearer rejected" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(`{"data":{"agentActivityCreate":{"success":true}}}`))
	}))
	defer srv.Close()
	c := NewClient("id", "secret")
	c.Endpoint, c.TokenEndpoint = srv.URL, srv.URL+"/oauth/token"
	c.token, c.expiresAt = "rejected", time.Now().Add(30*24*time.Hour)
	content := activityContent{Type: activityTypeResponse, Body: "answer"}
	for range 2 {
		require.NoError(t, c.createActivity(context.Background(), "session", content, false))
	}
	assert.EqualValues(t, 1, tokenCalls.Load())
	assert.Equal(t, []string{"Bearer rejected", "Bearer replacement", "Bearer replacement"}, tokens)
	require.Len(t, bodies, 3)
	assert.Equal(t, bodies[0], bodies[1], "retry preserves the activity payload")
}

func TestActivityRetriesUnauthorizedOnlyOnce(t *testing.T) {
	var tokenCalls, activityCalls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/oauth/token" {
			_, _ = fmt.Fprintf(w, `{"access_token":"token-%d","token_type":"Bearer","expires_in":2592000}`, tokenCalls.Add(1))
			return
		}
		activityCalls.Add(1)
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	c := NewClient("id", "secret")
	c.Endpoint, c.TokenEndpoint = srv.URL, srv.URL+"/oauth/token"
	c.token, c.expiresAt = "rejected", time.Now().Add(30*24*time.Hour)
	err := c.createActivity(context.Background(), "session", activityContent{Type: activityTypeResponse, Body: "answer"}, false)
	require.ErrorContains(t, err, "HTTP 401")
	assert.EqualValues(t, 2, activityCalls.Load())
	assert.EqualValues(t, 1, tokenCalls.Load())
	assert.Empty(t, c.token, "the second rejected token must not remain cached")
	token, err := c.accessToken(context.Background())
	require.NoError(t, err)
	assert.Equal(t, "token-2", token, "the next attempt can obtain new credentials")
}

func TestActivityReturnsTokenReplacementFailure(t *testing.T) {
	var tokenCalls, activityCalls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/oauth/token" {
			tokenCalls.Add(1)
		} else {
			activityCalls.Add(1)
		}
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	c := NewClient("id", "secret")
	c.Endpoint, c.TokenEndpoint = srv.URL, srv.URL+"/oauth/token"
	c.token, c.expiresAt = "rejected", time.Now().Add(30*24*time.Hour)
	err := c.createActivity(context.Background(), "session", activityContent{Type: activityTypeResponse, Body: "answer"}, false)
	require.ErrorContains(t, err, "HTTP 401")
	assert.EqualValues(t, 1, activityCalls.Load())
	assert.EqualValues(t, 1, tokenCalls.Load(), "token endpoint failures do not trigger another renewal")
	assert.Empty(t, c.token)
}

func TestLateUnauthorizedResponseKeepsNewerToken(t *testing.T) {
	var tokenCalls, activityCalls atomic.Int32
	slowReceived, releaseSlow := make(chan struct{}), make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/oauth/token" {
			tokenCalls.Add(1)
			_, _ = w.Write([]byte(`{"access_token":"replacement","token_type":"Bearer","expires_in":2592000}`))
			return
		}
		activityCalls.Add(1)
		var request struct {
			Variables struct {
				Input activityRequest `json:"input"`
			} `json:"variables"`
		}
		if !assert.NoError(t, json.NewDecoder(r.Body).Decode(&request)) {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if r.Header.Get("Authorization") == "Bearer rejected" {
			if request.Variables.Input.Content.Body == "slow" {
				close(slowReceived)
				<-releaseSlow
			}
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		assert.Equal(t, "Bearer replacement", r.Header.Get("Authorization"))
		_, _ = w.Write([]byte(`{"data":{"agentActivityCreate":{"success":true}}}`))
	}))
	defer srv.Close()
	defer close(releaseSlow)
	c := NewClient("id", "secret")
	c.Endpoint, c.TokenEndpoint = srv.URL, srv.URL+"/oauth/token"
	c.token, c.expiresAt = "rejected", time.Now().Add(30*24*time.Hour)
	slowResult := make(chan error, 1)
	go func() {
		slowResult <- c.createActivity(context.Background(), "session", activityContent{Type: activityTypeResponse, Body: "slow"}, false)
	}()
	select {
	case <-slowReceived:
	case <-time.After(5 * time.Second):
		t.Fatal("slow request did not arrive")
	}
	require.NoError(t, c.createActivity(context.Background(), "session", activityContent{Type: activityTypeResponse, Body: "fast"}, false))
	releaseSlow <- struct{}{}
	require.NoError(t, <-slowResult)
	assert.EqualValues(t, 1, tokenCalls.Load(), "the late 401 must reuse the newer token")
	assert.EqualValues(t, 4, activityCalls.Load())
	assert.Equal(t, "replacement", c.token)
}
