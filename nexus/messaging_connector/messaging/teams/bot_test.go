package teams

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// newTestBot returns a TeamsBot with a pre-cached token so tests never hit
// the real OAuth endpoint.
func newTestBot() *TeamsBot {
	return &TeamsBot{
		Client:      http.DefaultClient,
		AppID:       "test-app-id",
		accessToken: "test-token",
		expiresAt:   time.Now().Add(time.Hour),
	}
}

func TestNewTeamsBotUsesTenantTokenEndpoint(t *testing.T) {
	bot, err := NewTeamsBot("tenant-id", "app-id", "app-password")
	require.NoError(t, err)

	assert.Equal(t, "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token", bot.tokenURL)
}

func TestFetchBearerTokenUsesClientCredentials(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, http.MethodPost, r.Method)
		assert.Equal(t, "application/x-www-form-urlencoded", r.Header.Get("Content-Type"))
		if assert.NoError(t, r.ParseForm()) {
			assert.Equal(t, "client_credentials", r.Form.Get("grant_type"))
			assert.Equal(t, "app-id", r.Form.Get("client_id"))
			assert.Equal(t, "app-password", r.Form.Get("client_secret"))
			assert.Equal(t, "https://api.botframework.com/.default", r.Form.Get("scope"))
		}

		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"token","expires_in":3600}`))
	}))
	defer srv.Close()

	bot := &TeamsBot{
		Client:      srv.Client(),
		AppID:       "app-id",
		appPassword: "app-password",
		tokenURL:    srv.URL,
		scope:       defaultScope,
	}
	token, expiresAt, err := bot.fetchBearerToken(context.Background())
	require.NoError(t, err)

	assert.Equal(t, "token", token)
	assert.WithinDuration(t, time.Now().Add(time.Hour), expiresAt, time.Second)
}
