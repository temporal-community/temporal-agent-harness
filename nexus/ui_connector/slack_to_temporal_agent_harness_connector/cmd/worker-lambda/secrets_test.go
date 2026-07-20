package main

import (
	"testing"

	"go.temporal.io/sdk/contrib/aws/lambdaworker"
)

// TestResolveSecretsNoARNsUsesEnvToken covers the no-secret path: with neither
// *_SECRET_ARN set, resolveSecrets returns the SLACK_BOT_TOKEN env var and never
// touches AWS or the client credentials.
func TestResolveSecretsNoARNsUsesEnvToken(t *testing.T) {
	t.Setenv(envAPIKeySecretARN, "")
	t.Setenv(envSlackSecretsARN, "")
	t.Setenv("SLACK_BOT_TOKEN", "xoxb-test")

	var o lambdaworker.Options
	token, err := resolveSecrets(&o)
	if err != nil {
		t.Fatalf("resolveSecrets: %v", err)
	}
	if token != "xoxb-test" {
		t.Fatalf("token = %q, want %q", token, "xoxb-test")
	}
	if o.ClientOptions.Credentials != nil {
		t.Fatal("expected no credentials set when no API-key secret ARN is configured")
	}
}
