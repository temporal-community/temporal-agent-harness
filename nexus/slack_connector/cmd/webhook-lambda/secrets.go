package main

import (
	"context"
	"os"

	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/internal/awssecrets"
)

const (
	envAPIKeySecretARN     = "TEMPORAL_API_KEY_SECRET_ARN"
	envSigningSecretARN    = "SLACK_SIGNING_SECRET_ARN"
	envSlackTokenSecretARN = "SLACK_BOT_TOKEN_SECRET_ARN"
)

// resolveSecrets returns the Temporal Cloud API key, Slack signing secret, and
// Slack bot token. Each value comes from its Secrets Manager secret when the
// corresponding *_ARN env var is set (plain-string secret, fetched once per cold
// start), otherwise from its plain env var (TEMPORAL_API_KEY, SLACK_SIGNING_SECRET,
// SLACK_BOT_TOKEN) for local/dev. The API key may be empty for a local dev server
// with no auth; the caller enforces which values are required.
func resolveSecrets(ctx context.Context) (apiKey, signingSecret, botToken string, err error) {
	apiKey = os.Getenv("TEMPORAL_API_KEY")
	signingSecret = os.Getenv("SLACK_SIGNING_SECRET")
	botToken = os.Getenv("SLACK_BOT_TOKEN")

	apiARN := os.Getenv(envAPIKeySecretARN)
	signARN := os.Getenv(envSigningSecretARN)
	botARN := os.Getenv(envSlackTokenSecretARN)
	if apiARN == "" && signARN == "" && botARN == "" {
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
	if signARN != "" {
		if signingSecret, err = fetcher.GetPlain(ctx, signARN); err != nil {
			return "", "", "", err
		}
	}
	if botARN != "" {
		if botToken, err = fetcher.GetPlain(ctx, botARN); err != nil {
			return "", "", "", err
		}
	}

	return apiKey, signingSecret, botToken, nil
}
