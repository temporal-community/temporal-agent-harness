// Large-payload offload to S3 (Python's `s3_payload_storage()`), the multi-host backend. It uses
// Temporal's own S3 driver, which keys objects by content hash and checks integrity on read. The
// TS and Python drivers share the driver name and type ("aws.s3driver") and claim keys, so a mixed
// fleet on one bucket reads each other's payloads.

import { S3Client } from "@aws-sdk/client-s3";
import { ExternalStorage } from "@temporalio/common";
import { S3StorageDriver } from "@temporalio/external-storage-s3";
import { AwsSdkS3StorageDriverClient } from "@temporalio/external-storage-s3-aws-sdk";
import { DEFAULT_PAYLOAD_SIZE_THRESHOLD } from "./large-payload.ts";

/**
 * Offload oversized payloads to `bucket`; every client, worker and server of the deployment must
 * use the same one. Region and credentials come from the standard AWS chain, and
 * `AWS_ENDPOINT_URL_S3` overrides the endpoint; pass `client` for anything else (an S3-compatible
 * server usually needs `forcePathStyle: true`). Pass the result as `AgentHarnessPlugin`'s
 * `largePayloadOffload` and as `externalStorage` in your clients' `dataConverter`.
 */
export function s3PayloadStorage({
  bucket,
  client = new S3Client({}),
  payloadSizeThreshold = DEFAULT_PAYLOAD_SIZE_THRESHOLD,
}: {
  bucket: string;
  client?: S3Client;
  payloadSizeThreshold?: number;
}): ExternalStorage {
  return new ExternalStorage({
    drivers: [new S3StorageDriver({ client: new AwsSdkS3StorageDriverClient(client), bucket })],
    payloadSizeThreshold,
  });
}
