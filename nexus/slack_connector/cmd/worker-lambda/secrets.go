package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"os"
	"time"

	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/secretsmanager"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/contrib/aws/lambdaworker"
)

const (
	// envAPIKeySecretARN names the env var holding the ARN of a Secrets Manager
	// secret whose plain-string value is the Temporal Cloud API key.
	envAPIKeySecretARN = "TEMPORAL_API_KEY_SECRET_ARN"
	// envSlackTokenSecretARN names the env var holding the ARN of a Secrets
	// Manager secret whose plain-string value is the Slack bot token.
	envSlackTokenSecretARN = "SLACK_BOT_TOKEN_SECRET_ARN"
)

// resolveSecrets sources sensitive configuration for the Lambda worker from AWS
// Secrets Manager and returns the Slack bot token to register the worker with.
//
//   - If TEMPORAL_API_KEY_SECRET_ARN is set, the Temporal Cloud API key is fetched
//     and installed as TLS-enabled API-key credentials on o.ClientOptions.
//   - The Slack bot token comes from SLACK_BOT_TOKEN_SECRET_ARN (fetched) when set,
//     otherwise from the SLACK_BOT_TOKEN env var.
//
// Secret values are plain strings, fetched once per Lambda cold start. When no
// secret ARNs are set, existing env/envconfig behavior is preserved so local and
// dev-server runs still work.
func resolveSecrets(o *lambdaworker.Options) (slackToken string, err error) {
	slackToken = os.Getenv("SLACK_BOT_TOKEN")
	apiARN := os.Getenv(envAPIKeySecretARN)
	slackARN := os.Getenv(envSlackTokenSecretARN)
	if apiARN == "" && slackARN == "" {
		return slackToken, nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	awsCfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		return "", fmt.Errorf("load AWS config: %w", err)
	}
	sm := secretsmanager.NewFromConfig(awsCfg)

	if apiARN != "" {
		key, err := getPlainSecret(ctx, sm, apiARN)
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
		slackToken, err = getPlainSecret(ctx, sm, slackARN)
		if err != nil {
			return "", err
		}
	}

	return slackToken, nil
}

// getPlainSecret fetches a plain-string secret value by ARN, erroring if the
// secret is missing a string value.
func getPlainSecret(ctx context.Context, sm *secretsmanager.Client, arn string) (string, error) {
	out, err := sm.GetSecretValue(ctx, &secretsmanager.GetSecretValueInput{SecretId: &arn})
	if err != nil {
		return "", fmt.Errorf("get secret %q: %w", arn, err)
	}
	if out.SecretString == nil || *out.SecretString == "" {
		return "", fmt.Errorf("secret %q has no plain-string value", arn)
	}
	return *out.SecretString, nil
}
