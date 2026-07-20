package main

import (
	"context"
	"crypto/tls"
	"os"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack_to_temporal_agent_harness_connector/internal/awssecrets"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/contrib/aws/lambdaworker"
)

const (
	// envAPIKeySecretARN names the env var holding the ARN of a Secrets Manager
	// secret whose plain-string value is the Temporal Cloud API key.
	envAPIKeySecretARN = "TEMPORAL_API_KEY_SECRET_ARN"
	// envSlackSecretsARN names the env var holding the ARN of a Secrets Manager
	// secret whose value is a JSON object of Slack values (SLACK_BOT_TOKEN, ...).
	envSlackSecretsARN = "SLACK_SECRETS_ARN"
	// keySlackBotToken is the field read from the Slack JSON secret.
	keySlackBotToken = "SLACK_BOT_TOKEN"
)

// resolveSecrets sources sensitive configuration for the Lambda worker from AWS
// Secrets Manager and returns the Slack bot token to register the worker with.
//
//   - If TEMPORAL_API_KEY_SECRET_ARN is set, the Temporal Cloud API key is fetched
//     and installed as TLS-enabled API-key credentials on o.ClientOptions.
//   - If SLACK_SECRETS_ARN is set, the Slack bot token is read from the SLACK_BOT_TOKEN
//     field of that JSON secret; otherwise it comes from the SLACK_BOT_TOKEN env var.
//
// Secrets are fetched once per Lambda cold start. When no secret ARNs are set,
// existing env/envconfig behavior is preserved so local and dev-server runs still work.
func resolveSecrets(o *lambdaworker.Options) (slackToken string, err error) {
	slackToken = os.Getenv("SLACK_BOT_TOKEN")
	apiARN := os.Getenv(envAPIKeySecretARN)
	slackARN := os.Getenv(envSlackSecretsARN)
	if apiARN == "" && slackARN == "" {
		return slackToken, nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	fetcher, err := awssecrets.NewFetcher(ctx)
	if err != nil {
		return "", err
	}

	if apiARN != "" {
		key, err := fetcher.GetPlain(ctx, apiARN)
		if err != nil {
			return "", err
		}
		o.ClientOptions.Credentials = client.NewAPIKeyStaticCredentials(key)
		// Temporal Cloud API-key auth requires TLS. Preserve any TLS config
		// envconfig already set (e.g. from TEMPORAL_TLS); otherwise enable it.
		if o.ClientOptions.ConnectionOptions.TLS == nil {
			o.ClientOptions.ConnectionOptions.TLS = &tls.Config{}
		}
	}

	if slackARN != "" {
		slack, err := fetcher.GetJSON(ctx, slackARN)
		if err != nil {
			return "", err
		}
		slackToken = slack[keySlackBotToken]
	}

	return slackToken, nil
}
