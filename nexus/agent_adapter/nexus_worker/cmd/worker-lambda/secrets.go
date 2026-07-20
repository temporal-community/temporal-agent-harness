package main

import (
	"context"
	"crypto/tls"
	"os"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/agent_adapter/nexus_worker/internal/awssecrets"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/contrib/aws/lambdaworker"
)

// envAPIKeySecretARN names the env var holding the ARN of a Secrets Manager
// secret whose plain-string value is the Temporal Cloud API key.
const envAPIKeySecretARN = "TEMPORAL_API_KEY_SECRET_ARN"

// resolveSecrets sources the Temporal Cloud API key from AWS Secrets Manager
// when TEMPORAL_API_KEY_SECRET_ARN is set, installing it as TLS-enabled API-key
// credentials on o.ClientOptions. The secret is fetched once per Lambda cold
// start. When the ARN is unset, existing env/envconfig behavior is preserved so
// local and dev-server runs still work.
//
// Unlike the connector worker, this worker needs no Slack secret — it only talks
// to Temporal (Nexus tasks in, workflow updates/queries out).
func resolveSecrets(o *lambdaworker.Options) error {
	apiARN := os.Getenv(envAPIKeySecretARN)
	if apiARN == "" {
		return nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	fetcher, err := awssecrets.NewFetcher(ctx)
	if err != nil {
		return err
	}

	key, err := fetcher.GetPlain(ctx, apiARN)
	if err != nil {
		return err
	}
	o.ClientOptions.Credentials = client.NewAPIKeyStaticCredentials(key)
	// Temporal Cloud API-key auth requires TLS. Preserve any TLS config envconfig
	// already set (e.g. from TEMPORAL_TLS); otherwise enable it.
	if o.ClientOptions.ConnectionOptions.TLS == nil {
		o.ClientOptions.ConnectionOptions.TLS = &tls.Config{}
	}
	return nil
}
