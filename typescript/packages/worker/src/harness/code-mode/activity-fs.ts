// Worker half of an activity filesystem: each method becomes `vfs.<name>.<method>`, plus `view`
// and `okf_graph`. A fresh instance is built from the call's config, so the config is checked
// every time.

import { ApplicationFailure } from "@temporalio/activity";
import {
  FsError,
  MAX_VIEW_BYTES,
  activityName,
  type ActivityFilesystem,
} from "@temporalio/agent-harness";
import { pyRepr } from "@temporalio/agent-harness/harness/code-mode";
import { ZodError, z } from "zod";
import { buildOkfGraph, type OkfReadable } from "./okf.ts";
import { isBadArgument, osFailureOf, type OsFailure } from "./os-error.ts";

export class ConfigError extends Error {
  override name = "ConfigError";
}

export interface FilesystemOps extends OkfReadable {
  write?(path: string, data: Uint8Array): Promise<void>;
  mkdir?(path: string): Promise<void>;
  delete?(path: string): Promise<void>;
  rename?(path: string, target: string): Promise<void>;
}

export interface FilesystemRegistration {
  readonly kind: "activity-filesystem";
  readonly name: string;
  readonly activities: Record<string, (input: unknown) => Promise<unknown>>;
}

const WRITE = ["write", "mkdir", "delete", "rename"] as const;
const READ = ["stat", "read", "list"] as const;

const FsCall = z.object({
  config: z.record(z.string(), z.unknown()),
  path: z.string(),
});
const FsRead = FsCall.extend({
  offset: z.number().default(0),
  length: z.number().nullable().default(null),
});
const FsWrite = FsCall.extend({
  content: z.string(),
  encoding: z.enum(["utf-8", "base64"]).default("utf-8"),
});
const FsRename = FsCall.extend({ target: z.string() });
const GraphCall = FsCall.extend({ links: z.boolean().default(true) });

function hasMethod(ctor: { prototype: object }, name: string): boolean {
  let proto: object | null = ctor.prototype;
  while (proto && proto !== Object.prototype) {
    if (typeof (proto as Record<string, unknown>)[name] === "function") return true;
    proto = Object.getPrototypeOf(proto) as object | null;
  }
  return false;
}

export function collectFilesystemActivities(
  filesystems: readonly FilesystemRegistration[],
): Record<string, (input: unknown) => Promise<unknown>> {
  const names = new Set<string>();
  const activities: Record<string, (input: unknown) => Promise<unknown>> = {};
  for (const fs of filesystems) {
    if (
      fs?.kind !== "activity-filesystem" ||
      typeof fs.activities !== "object" ||
      fs.activities === null
    )
      throw new TypeError(
        `AgentHarnessPlugin(filesystems=...) got ${String(fs)}, which is not an ActivityFileSystem`,
      );
    if (names.has(fs.name))
      throw new TypeError(
        `AgentHarnessPlugin(filesystems=...) has two filesystems named '${fs.name}'`,
      );
    names.add(fs.name);
    Object.assign(activities, fs.activities);
  }
  return activities;
}

export function filesystemActivities<C>(
  fs: ActivityFilesystem<C>,
  Impl: new (config: C) => FilesystemOps,
): FilesystemRegistration {
  const missing = READ.filter((name) => !hasMethod(Impl, name));
  if (missing.length) throw new TypeError(`${fs.name} does not implement ${missing.join(", ")}()`);
  const present = WRITE.filter((name) => hasMethod(Impl, name));
  if (present.length && present.length !== WRITE.length) {
    const absent = WRITE.filter((name) => !present.includes(name));
    throw new TypeError(
      `${fs.name} implements ${present.join(", ")}() but not ${absent.join(", ")}(); a writable filesystem needs all four`,
    );
  }
  if (fs.writable && !present.length)
    throw new TypeError(
      `${fs.name} does not implement write(), mkdir(), delete(), rename(); a writable filesystem needs all four`,
    );
  if (!fs.writable && present.length)
    throw new TypeError(`${fs.name} implements write methods but was declared read-only`);

  const activities: Record<string, (input: unknown) => Promise<unknown>> = {
    [activityName(fs.name, "stat")]: async (input) => {
      const call = parseCall(FsCall, input, fs.name);
      const found = await run(fs, Impl, call, (disk, path) => disk.stat(path));
      return { found };
    },
    [activityName(fs.name, "read")]: async (input) => {
      const call = parseCall(FsRead, input, fs.name);
      const data = await run(fs, Impl, call, (disk, path) =>
        disk.read(path, call.offset, call.length),
      );
      return wire(data);
    },
    [activityName(fs.name, "list")]: async (input) => {
      const call = parseCall(FsCall, input, fs.name);
      return run(fs, Impl, call, (disk, path) => disk.list(path));
    },
    [activityName(fs.name, "view")]: async (input) => {
      const call = parseCall(FsRead, input, fs.name);
      const length = Math.min(call.length ?? MAX_VIEW_BYTES, MAX_VIEW_BYTES);
      const offset = Math.max(call.offset, 0);
      return run(fs, Impl, call, async (disk, path) => {
        const found = await disk.stat(path);
        if (found === null)
          throw new FsError("FileNotFoundError", 2, "No such file or directory", path);
        if (found.is_dir) throw new FsError("IsADirectoryError", 21, "Is a directory", path);
        const data = (await disk.read(path, offset, length)).subarray(0, length);
        return { ...wire(data), offset, size: found.size ?? data.length, mtime: found.mtime ?? 0 };
      });
    },
    [activityName(fs.name, "okf_graph")]: async (input) => {
      const call = parseCall(GraphCall, input, fs.name);
      const now = new Date();
      return run(fs, Impl, call, (disk, path) =>
        buildOkfGraph(disk, { prefix: path, links: call.links, now }),
      );
    },
  };
  if (fs.writable) {
    activities[activityName(fs.name, "write")] = async (input) => {
      const call = parseCall(FsWrite, input, fs.name);
      const data =
        call.encoding === "base64" ? decodeBase64(call.content) : utf8.encode(call.content);
      await run(fs, Impl, call, (disk, path) => disk.write!(path, data));
    };
    activities[activityName(fs.name, "mkdir")] = async (input) => {
      const call = parseCall(FsCall, input, fs.name);
      await run(fs, Impl, call, (disk, path) => disk.mkdir!(path));
    };
    activities[activityName(fs.name, "delete")] = async (input) => {
      const call = parseCall(FsCall, input, fs.name);
      await run(fs, Impl, call, (disk, path) => disk.delete!(path));
    };
    activities[activityName(fs.name, "rename")] = async (input) => {
      const call = parseCall(FsRename, input, fs.name);
      await run(fs, Impl, call, (disk, path) => disk.rename!(path, call.target));
    };
  }
  return { kind: "activity-filesystem", name: fs.name, activities };
}

const utf8 = new TextEncoder();

function decodeBase64(value: string): Uint8Array {
  const binary = Buffer.from(value, "base64");
  return new Uint8Array(binary.buffer, binary.byteOffset, binary.byteLength);
}

function wire(data: Uint8Array): { content: string; encoding: "utf-8" | "base64" } {
  try {
    return { content: new TextDecoder("utf-8", { fatal: true }).decode(data), encoding: "utf-8" };
  } catch {
    return { content: Buffer.from(data).toString("base64"), encoding: "base64" };
  }
}

function parseCall<S extends z.ZodType>(schema: S, input: unknown, name: string): z.output<S> {
  const parsed = schema.safeParse(input);
  if (!parsed.success) throw valueError(name, parsed.error);
  return parsed.data;
}

/**
 * Build an instance from the call's config and run `op` on it and the call's path, as Python does:
 * an OS error (an `FsError` or a Node fs error) is a non-retryable failure named for its built-in,
 * and a rejected config or argument a non-retryable `ValueError`. Anything else is retried.
 */
async function run<C, T>(
  fs: ActivityFilesystem<C>,
  Impl: new (config: C) => FilesystemOps,
  call: { config: Record<string, unknown>; path: string },
  op: (disk: FilesystemOps, path: string) => Promise<T>,
): Promise<T> {
  try {
    return await op(new Impl(fs.configSchema.parse(call.config)), call.path);
  } catch (e) {
    const os = osFailureOf(e);
    if (os !== null) throw osFailure(os, call.path, e);
    if (e instanceof ZodError || e instanceof ConfigError || isBadArgument(e))
      throw valueError(fs.name, e);
    throw e;
  }
}

function valueError(name: string, error: Error): ApplicationFailure {
  return ApplicationFailure.create({
    type: "ValueError",
    nonRetryable: true,
    message: `${name}: ${error.message}`,
  });
}

function osFailure(
  { type, errno, strerror }: OsFailure,
  path: string,
  cause: unknown,
): ApplicationFailure {
  return ApplicationFailure.create({
    type,
    nonRetryable: true,
    message: `${strerror}: ${pyRepr(path)}`,
    details: [{ errno, strerror, path }],
    ...(cause instanceof Error ? { cause } : {}),
  });
}
