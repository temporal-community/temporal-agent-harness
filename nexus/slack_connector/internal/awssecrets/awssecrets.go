// Package awssecrets fetches plain-string secrets from AWS Secrets Manager.
// It is shared by the Lambda binaries (cmd/worker-lambda, cmd/webhook-lambda),
// which source the Temporal Cloud API key, Slack signing secret, and Slack bot
// token from Secrets Manager at cold start.
package awssecrets

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/secretsmanager"
)

// Fetcher retrieves plain-string secret values. Create one per process (it loads
// the AWS config and Secrets Manager client once) and reuse it for each secret.
type Fetcher struct {
	sm *secretsmanager.Client
}

// NewFetcher loads the default AWS config and returns a Fetcher. In Lambda the
// region and credentials come from the execution environment automatically.
func NewFetcher(ctx context.Context) (*Fetcher, error) {
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		return nil, fmt.Errorf("load AWS config: %w", err)
	}
	return &Fetcher{sm: secretsmanager.NewFromConfig(cfg)}, nil
}

// GetPlain returns the plain-string value of the secret identified by arn,
// erroring if the secret has no string value.
func (f *Fetcher) GetPlain(ctx context.Context, arn string) (string, error) {
	out, err := f.sm.GetSecretValue(ctx, &secretsmanager.GetSecretValueInput{SecretId: &arn})
	if err != nil {
		return "", fmt.Errorf("get secret %q: %w", arn, err)
	}
	if out.SecretString == nil || *out.SecretString == "" {
		return "", fmt.Errorf("secret %q has no plain-string value", arn)
	}
	return *out.SecretString, nil
}

// GetJSON fetches a secret whose value is a JSON object of string fields and
// returns it as a map. Use this for a secret that bundles several values (e.g.
// the Slack secret holding SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, ...).
func (f *Fetcher) GetJSON(ctx context.Context, arn string) (map[string]string, error) {
	out, err := f.sm.GetSecretValue(ctx, &secretsmanager.GetSecretValueInput{SecretId: &arn})
	if err != nil {
		return nil, fmt.Errorf("get secret %q: %w", arn, err)
	}
	if out.SecretString == nil || *out.SecretString == "" {
		return nil, fmt.Errorf("secret %q has no string value", arn)
	}
	var m map[string]string
	if err := json.Unmarshal([]byte(*out.SecretString), &m); err != nil {
		return nil, fmt.Errorf("secret %q is not a JSON object of strings: %w", arn, err)
	}
	return m, nil
}
