package main

import (
	"context"
	"os"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack_to_temporal_agent_harness_connector/internal/awssecrets"
)

const (
	// envAPIKeySecretARN holds the ARN of a plain-string secret: the Temporal Cloud API key.
	envAPIKeySecretARN = "TEMPORAL_API_KEY_SECRET_ARN"
	// envSlackSecretsARN holds the ARN of a JSON secret bundling the Slack values.
	envSlackSecretsARN = "SLACK_SECRETS_ARN"

	// Fields read from the Slack JSON secret.
	keySigningSecret = "SLACK_SIGNING_SECRET"
	keyBotToken      = "SLACK_BOT_TOKEN"
)

// resolveSecrets returns the Temporal Cloud API key, Slack signing secret, and
// Slack bot token.
//
//   - The API key comes from the plain-string secret at TEMPORAL_API_KEY_SECRET_ARN
//     when set, otherwise the TEMPORAL_API_KEY env var.
//   - The signing secret and bot token come from the SLACK_SIGNING_SECRET and
//     SLACK_BOT_TOKEN fields of the JSON secret at SLACK_SECRETS_ARN when set,
//     otherwise the SLACK_SIGNING_SECRET / SLACK_BOT_TOKEN env vars.
//
// Secrets are fetched once per cold start. The caller enforces which values are
// required. The API key may be empty for a local dev server with no auth.
func resolveSecrets(ctx context.Context) (apiKey, signingSecret, botToken string, err error) {
	apiKey = os.Getenv("TEMPORAL_API_KEY")
	signingSecret = os.Getenv("SLACK_SIGNING_SECRET")
	botToken = os.Getenv("SLACK_BOT_TOKEN")

	apiARN := os.Getenv(envAPIKeySecretARN)
	slackARN := os.Getenv(envSlackSecretsARN)
	if apiARN == "" && slackARN == "" {
		return apiKey, signingSecret, botToken, nil
	}

	fetcher, err := awssecrets.NewFetcher(ctx)
	if err != nil {
		return "", "", "", err
	}

	if apiARN != "" {
		if apiKey, err = fetcher.GetPlain(ctx, apiARN); err != nil {
			return "", "", "", err
		}
	}
	if slackARN != "" {
		slack, err := fetcher.GetJSON(ctx, slackARN)
		if err != nil {
			return "", "", "", err
		}
		signingSecret = slack[keySigningSecret]
		botToken = slack[keyBotToken]
	}

	return apiKey, signingSecret, botToken, nil
}
