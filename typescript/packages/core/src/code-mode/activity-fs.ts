// The workflow half of an activity-backed filesystem. The worker builds the activities
// (`filesystemActivities`); here a mount only calls them by name, so this module stays sandbox-safe.

import { proxyActivities } from "@temporalio/workflow";
import type { z } from "zod";
import { decodeBase64, encodeBase64 } from "./base64.ts";
import type { FileStat, FileSystem } from "./vfs.ts";
import type { OKFGraph } from "./okf.ts";

const NAME = /^[a-z0-9][a-z0-9_-]*$/;

/** The most one file-view call returns. The console pages through anything larger. */
export const MAX_VIEW_BYTES = 256_000;

const RETRY = { maximumAttempts: 3 };

export function activityName(filesystem: string, operation: string): string {
  return `vfs.${filesystem}.${operation}`;
}

export interface ActivityFilesystem<C> {
  readonly kind: "activity-filesystem";
  readonly name: string;
  readonly writable: boolean;
  readonly configSchema: z.ZodType<C>;
  backend(
    config: C,
    options?: { timeout?: string | number; okfVersion?: string | null },
  ): ActivityBackend;
}

/**
 * A filesystem whose operations run as `vfs.<name>.*` activities. `configSchema` is validated
 * on every activity, not here: a bad config fails the activity that uses it.
 */
export function activityFilesystem<C>(options: {
  name: string;
  configSchema: z.ZodType<C>;
  writable: boolean;
}): ActivityFilesystem<C> {
  if (!NAME.test(options.name))
    throw new TypeError(
      `filesystem name '${options.name}' must be lowercase letters, digits, '-' and '_', like 'local-disk'`,
    );
  return {
    kind: "activity-filesystem",
    name: options.name,
    writable: options.writable,
    configSchema: options.configSchema,
    backend(config, { timeout = "30 seconds", okfVersion = null } = {}) {
      if (config === null || typeof config !== "object" || Array.isArray(config))
        throw new TypeError(`${options.name}.backend() takes a config object`);
      const stored = JSON.parse(JSON.stringify(config)) as Record<string, unknown>;
      const source = { filesystem: options.name, config: stored };
      return options.writable
        ? new WritableActivityBackend(source, timeout, okfVersion)
        : new ActivityBackend(source, timeout, okfVersion);
    },
  };
}

interface WireFile {
  content: string;
  encoding?: "utf-8" | "base64";
}

const utf8 = new TextEncoder();

function unwire(content: string, encoding: "utf-8" | "base64" | undefined): Uint8Array {
  return encoding === "base64" ? decodeBase64(content) : utf8.encode(content);
}

function wire(data: Uint8Array): { content: string; encoding: "utf-8" | "base64" } {
  try {
    return { content: new TextDecoder("utf-8", { fatal: true }).decode(data), encoding: "utf-8" };
  } catch {
    return { content: encodeBase64(data), encoding: "base64" };
  }
}

/**
 * A read-only mount backend. Each operation is one activity on the workflow's task queue.
 * `okfGraph` walks the bundle in a single activity.
 */
export class ActivityBackend implements FileSystem {
  readonly source: { filesystem: string; config: Record<string, unknown> };
  readonly okfVersion: string | null;
  readonly #timeout: string | number;

  constructor(
    source: { filesystem: string; config: Record<string, unknown> },
    timeout: string | number,
    okfVersion: string | null,
  ) {
    this.source = source;
    this.#timeout = timeout;
    this.okfVersion = okfVersion;
  }

  async stat(path: string): Promise<FileStat | null> {
    const result = await this.#call<{ found: FileStat | null }>("stat", { path });
    return result.found;
  }

  async read(path: string): Promise<Uint8Array> {
    const found = await this.#call<WireFile>("read", { path });
    return unwire(found.content, found.encoding);
  }

  async list(path: string): Promise<string[]> {
    return this.#call<string[]>("list", { path });
  }

  okfGraph(prefix: string, links: boolean): Promise<OKFGraph> {
    return this.#call<OKFGraph>("okf_graph", { path: prefix, links });
  }

  async #call<T>(operation: string, extra: Record<string, unknown>): Promise<T> {
    const arg = { config: this.source.config, ...extra };
    const activities = proxyActivities<Record<string, (input: unknown) => Promise<T>>>({
      startToCloseTimeout: this.#timeout,
      retry: RETRY,
    });
    // A failure stays an ActivityFailure, as in Python: the driver gives the script an OS error
    // as its built-in, and any other as the activity's failure.
    return activities[activityName(this.source.filesystem, operation)]!(arg);
  }

  protected do(operation: string, extra: Record<string, unknown>): Promise<void> {
    return this.#call<void>(operation, extra);
  }
}

class WritableActivityBackend extends ActivityBackend {
  async write(path: string, data: Uint8Array): Promise<void> {
    const { content, encoding } = wire(data);
    await this.do("write", { path, content, encoding });
  }

  async mkdir(path: string): Promise<void> {
    await this.do("mkdir", { path });
  }

  async delete(path: string): Promise<void> {
    await this.do("delete", { path });
  }

  async rename(path: string, target: string): Promise<void> {
    await this.do("rename", { path, target });
  }
}
