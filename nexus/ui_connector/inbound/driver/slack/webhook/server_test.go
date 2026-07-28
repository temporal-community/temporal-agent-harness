package webhook

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

const testSigningSecret = "test-signing-secret"

func TestServeHTTPRejectsUnsignedSlackRequests(t *testing.T) {
	t.Parallel()

	server := NewServer(nil, "test-task-queue", testSigningSecret, "")
	for _, path := range []string{routeEvents, routeInteractions, routeCommands} {
		path := path
		t.Run(path, func(t *testing.T) {
			t.Parallel()

			req := httptest.NewRequest(http.MethodPost, path, strings.NewReader("{}"))
			rec := httptest.NewRecorder()

			server.ServeHTTP(rec, req)

			if rec.Code != http.StatusUnauthorized {
				t.Fatalf("expected status %d, got %d", http.StatusUnauthorized, rec.Code)
			}
		})
	}
}

func TestServeHTTPRejectsInvalidSlackSignature(t *testing.T) {
	t.Parallel()

	server := NewServer(nil, "test-task-queue", testSigningSecret, "")
	req := httptest.NewRequest(http.MethodPost, routeEvents, strings.NewReader("{}"))
	req.Header.Set("X-Slack-Request-Timestamp", strconv.FormatInt(time.Now().Unix(), 10))
	req.Header.Set("X-Slack-Signature", "v0="+strings.Repeat("0", sha256.Size*2))
	rec := httptest.NewRecorder()

	server.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected status %d, got %d", http.StatusUnauthorized, rec.Code)
	}
}

func TestServeHTTPAcceptsSignedURLVerification(t *testing.T) {
	t.Parallel()

	server := NewServer(nil, "test-task-queue", testSigningSecret, "")
	body := `{"type":"url_verification","challenge":"expected-challenge"}`
	req := signedSlackRequest(http.MethodPost, routeEvents, body, time.Now())
	rec := httptest.NewRecorder()

	server.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected status %d, got %d: %s", http.StatusOK, rec.Code, rec.Body.String())
	}
	if got := rec.Body.String(); got != "expected-challenge" {
		t.Fatalf("expected challenge response, got %q", got)
	}
}

func signedSlackRequest(method, path, body string, timestamp time.Time) *http.Request {
	ts := strconv.FormatInt(timestamp.Unix(), 10)
	mac := hmac.New(sha256.New, []byte(testSigningSecret))
	_, _ = mac.Write([]byte("v0:" + ts + ":" + body))

	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("X-Slack-Request-Timestamp", ts)
	req.Header.Set("X-Slack-Signature", "v0="+hex.EncodeToString(mac.Sum(nil)))
	return req
}
