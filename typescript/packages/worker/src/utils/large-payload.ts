// Large-payload offload (Python's utils/large_payload.py) through Temporal's external storage:
// any payload at or over the threshold is written elsewhere and replaced on the wire by a small
// claim, and read back transparently on decode. Temporal caps one payload at 2 MB, and a stream
// poll base64-encodes its items, so without this one 1.6 MB reply, tool result or message makes
// every later poll fail and bricks the session.
//
// The local-file driver uses Python's driver name, type, directory, file format (the serialized
// Payload protobuf, named by its SHA-256) and claim keys, so both harnesses on one host read each
// other's offloaded payloads. It only spans processes that share the filesystem: every client,
// worker and server of a deployment must use the same backend, so use `s3PayloadStorage` (from
// `@temporalio/agent-harness-worker/utils/s3`) for anything multi-host. Nothing deletes offloaded
// files.

import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, unlinkSync, writeFileSync } from "node:fs";
import { mkdir, readFile, rename, stat, unlink, utimes, writeFile } from "node:fs/promises";
import { join } from "node:path";
import {
  ApplicationFailure,
  ExternalStorage,
  StorageDriverClaim,
  type StorageDriver,
} from "@temporalio/common";
import { decodePayloadProto, encodePayloadProto } from "@temporalio/workflow-streams/workflow";

export const DEFAULT_LOCAL_PAYLOAD_DIR = "/tmp/temporal-large-payloads";
/** Just under Temporal's 2 MB limit, so only genuinely oversized payloads leave history. */
export const DEFAULT_PAYLOAD_SIZE_THRESHOLD = 1_500_000;

/** Claim keys are flat file names: reject anything that could escape the directory. */
function safeKey(key: string | undefined): string {
  if (!key || key.includes("/") || key.includes("\\") || key.includes(".."))
    throw new Error(`invalid storage claim key: ${JSON.stringify(key)}`);
  return key;
}

const sha256 = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");

/** A driver that can check, when a worker is created, that it will be able to store. */
export interface CheckableStorageDriver extends StorageDriver {
  checkWritable(): void;
}

export function localFileStorageDriver(
  baseDir: string = DEFAULT_LOCAL_PAYLOAD_DIR,
  name = "local-file",
): CheckableStorageDriver {
  // Offloaded payloads are messages and tool results: readable by this user only.
  const write = async (path: string, raw: Uint8Array) => {
    await mkdir(baseDir, { recursive: true, mode: 0o700 });
    // Written aside and renamed, so a reader never sees a partial file.
    const tmp = `${path}.${process.pid}.${randomUUID()}.tmp`;
    await writeFile(tmp, raw, { mode: 0o600 });
    await rename(tmp, path);
  };
  const fail = (type: string, message: string) =>
    ApplicationFailure.create({ type, nonRetryable: true, message });
  return {
    name,
    type: "local.filedriver",
    checkWritable() {
      try {
        const probe = join(baseDir, `.probe.${process.pid}.${randomUUID()}`);
        mkdirSync(baseDir, { recursive: true, mode: 0o700 });
        writeFileSync(probe, "", { mode: 0o600 });
        unlinkSync(probe);
      } catch (e) {
        throw new Error(
          `The payload storage directory ${baseDir} isn't writable (${(e as Error).message}); fix its permissions or pass another directory.`,
          { cause: e },
        );
      }
    },
    async store(_context, payloads) {
      return Promise.all(
        payloads.map(async (payload) => {
          const raw = encodePayloadProto(payload);
          const digest = sha256(raw);
          const key = `${digest}.bin`;
          const path = join(baseDir, key);
          const existing = await stat(path).catch(() => null);
          // A hit is touched, so an age-based sweep only removes payloads nobody stored again; a
          // file of the wrong size (truncated) is replaced.
          if (existing?.size === raw.length) {
            const now = new Date();
            await utimes(path, now, now);
          } else {
            await write(path, raw);
          }
          return new StorageDriverClaim({ key, hash_algorithm: "sha256", hash_value: digest });
        }),
      );
    },
    async retrieve(_context, claims) {
      return Promise.all(
        claims.map(async (claim) => {
          const key = safeKey(claim.claimData.key);
          const path = join(baseDir, key);
          const raw = await readFile(path).then(
            (bytes) => new Uint8Array(bytes),
            (e: NodeJS.ErrnoException) => {
              if (e.code !== "ENOENT") throw e;
              throw fail(
                "PayloadMissing",
                `Offloaded payload ${path} is missing: it was deleted, or stored on another host (the local-file store only works where it was written; use s3PayloadStorage across hosts).`,
              );
            },
          );
          const expected = claim.claimData.hash_value;
          if (expected && sha256(raw) !== expected) {
            // Removed, so the next store of the same payload writes it afresh instead of deduping.
            await unlink(path).catch(() => {});
            throw fail(
              "PayloadCorrupt",
              `Offloaded payload ${path} is corrupt (integrity check failed) and was removed.`,
            );
          }
          return decodePayloadProto(raw);
        }),
      );
    },
  };
}

/** Offload oversized payloads to the local filesystem: single-host deployments only. */
export function localPayloadStorage({
  baseDir,
  payloadSizeThreshold = DEFAULT_PAYLOAD_SIZE_THRESHOLD,
}: { baseDir?: string; payloadSizeThreshold?: number } = {}): ExternalStorage {
  return new ExternalStorage({ drivers: [localFileStorageDriver(baseDir)], payloadSizeThreshold });
}

/** The default backend of `AgentHarnessPlugin`, `connectClient` and the CLI's workers. */
export const DEFAULT_PAYLOAD_STORAGE = localPayloadStorage();

/** Throws the reason if a driver of `storage` that can check itself can't store. */
export function checkPayloadStorage(storage: ExternalStorage): void {
  for (const driver of storage.drivers)
    (driver as Partial<CheckableStorageDriver>).checkWritable?.();
}
