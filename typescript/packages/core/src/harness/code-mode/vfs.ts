/// <reference path="../../sandbox.d.ts" />
// The Code Mode virtual filesystem: mounts a Python script reads and writes with `open()` and
// `pathlib`. A `Mount` puts a `FileSystem` backend at a virtual path. Backend methods are async and
// run in workflow code, so a backend can reach storage through an activity, a Nexus operation or a
// child workflow, or hold its files in workflow memory like `InMemoryFileSystem`. Nothing here
// touches the worker's disk: workflow tasks may land on any worker, and whatever a script reads
// during a step is read again on replay.
//
// Every file operation a script makes ends its step (see worker.ts). The driver maps Monty's
// operation onto backend calls (`performFileOp`), each made through `runner.runTool` as one of the
// `fs_*` tools below, so file access gets the agent's approval policy and tool lifecycle events
// like any host call.
//
// Workflow-safe.

import { ApplicationFailure, inWorkflowContext, workflowInfo } from "@temporalio/workflow";
import { z } from "zod";
import { injected, tool, type AnyHarnessTool } from "../agent.ts";
import { decodeBase64, encodeBase64 } from "./base64.ts";
import type { StateRef } from "../state/index.ts";

/** What a backend reports about one path. */
export interface FileStat {
  is_dir: boolean;
  size?: number;
  /** Unix seconds. Absent until a stat says; `0` is treated as unknown by an index. */
  mtime?: number;
}

/**
 * Storage behind a `Mount`. Methods are awaited in workflow code, so their bodies must be
 * workflow-safe. Paths are relative to the mount and normalized; the mount's root is ".". Throw
 * `osError(...)` for failures; from an activity, throw an `ApplicationFailure` whose `type` is the
 * built-in name (`FileNotFoundError`, ...), and the script sees the built-in exception.
 * `write`, `mkdir`, `delete` and `rename` are only needed for a writable mount.
 */
export interface FileSystem {
  stat(path: string): Promise<FileStat | null>;
  read(path: string): Promise<Uint8Array>;
  list(path: string): Promise<string[]>;
  write?(path: string, data: Uint8Array): Promise<void>;
  mkdir?(path: string): Promise<void>;
  delete?(path: string): Promise<void>;
  rename?(path: string, target: string): Promise<void>;
}

const WRITE_METHODS = ["write", "mkdir", "delete", "rename"] as const;
export const ROOT = ".";

// ---------------------------------------------------------------- paths

/** `path` with "." and ".." collapsed and no trailing slash; null if ".." climbs out of the root. */
function normalize(path: string): string | null {
  const parts: string[] = [];
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (parts.length === 0) return null;
      parts.pop();
    } else parts.push(part);
  }
  return (path.startsWith("/") ? "/" : "") + parts.join("/");
}

const parentOf = (path: string) =>
  path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ROOT;
const nameOf = (path: string) => path.slice(path.lastIndexOf("/") + 1);
/** Every directory above `path`, outermost first, the mount's root left out. */
function ancestors(path: string): string[] {
  const parts = path.split("/");
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join("/"));
}

// ---------------------------------------------------------------- errors

const STRERROR: Record<number, string> = {
  1: "Operation not permitted",
  2: "No such file or directory",
  13: "Permission denied",
  17: "File exists",
  18: "Invalid cross-device link",
  20: "Not a directory",
  21: "Is a directory",
  22: "Invalid argument",
  28: "No space left on device",
  39: "Directory not empty",
};
const ERRNO = {
  EPERM: 1,
  ENOENT: 2,
  EACCES: 13,
  EEXIST: 17,
  EXDEV: 18,
  ENOTDIR: 20,
  EISDIR: 21,
  EINVAL: 22,
  ENOSPC: 28,
  ENOTEMPTY: 39,
} as const;

/** The Python `OSError` subclasses a failed file operation raises inside the script. */
export type OsErrorType =
  | "OSError"
  | "FileNotFoundError"
  | "FileExistsError"
  | "IsADirectoryError"
  | "NotADirectoryError"
  | "PermissionError";

/** A failure the script sees as the built-in `type`, with Python's message for `errno` and `path`. */
export function osError(
  type: OsErrorType,
  errno: number,
  path: string,
  strerror = STRERROR[errno] ?? "Error",
): ApplicationFailure {
  return ApplicationFailure.create({
    type,
    nonRetryable: true,
    message: `[Errno ${errno}] ${strerror}: '${path}'`,
    details: [{ errno, strerror, path }],
  });
}

/**
 * An OS error thrown by activity filesystem code (disk, a walk). The activity wrapper turns it
 * into an `ApplicationFailure` the script sees as the built-in of the same name.
 */
export class FsError extends Error {
  readonly fsType: OsErrorType;
  readonly errno: number;
  readonly strerror: string;
  readonly path: string;
  constructor(fsType: OsErrorType, errno: number, strerror: string, path: string) {
    super(strerror);
    this.name = fsType;
    this.fsType = fsType;
    this.errno = errno;
    this.strerror = strerror;
    this.path = path;
  }
}

const OS_ERRORS = new Set<string>([
  "OSError",
  "FileNotFoundError",
  "FileExistsError",
  "IsADirectoryError",
  "NotADirectoryError",
  "PermissionError",
]);

// ---------------------------------------------------------------- mounts

/**
 * A `FileSystem` at a virtual path inside the sandbox. `path` is absolute and normalized
 * (`/skills`, not `/skills/`). `readOnly` is enforced before any backend call. `description` is
 * shown to the model next to the mount, so say what is there and how to use it.
 */
export class Mount {
  readonly path: string;
  readonly backend: FileSystem;
  readonly readOnly: boolean;
  readonly description: string;
  /** Set for an activity-backed mount, so successful operations update its FileIndex. */
  readonly tracked: IndexTrack | null;
  constructor(
    path: string,
    backend: FileSystem,
    {
      readOnly = false,
      description = "",
      tracked = null,
    }: { readOnly?: boolean; description?: string; tracked?: IndexTrack | null } = {},
  ) {
    if (!path.startsWith("/") || normalize(path) !== path)
      throw new TypeError(
        `mount path '${path}' must be an absolute, normalized POSIX path, like '/workspace'`,
      );
    if (path === "/")
      throw new TypeError("a mount cannot be the root '/'; mount at a named directory");
    for (const method of ["stat", "read", "list"] as const) {
      if (typeof backend[method] !== "function")
        throw new TypeError(
          `mount '${path}': the backend has no ${method}() method, so it is not a FileSystem`,
        );
    }
    if (!readOnly) {
      const missing = WRITE_METHODS.filter((m) => typeof backend[m] !== "function");
      if (missing.length)
        throw new TypeError(
          `mount '${path}' is writable, but its backend has no ${missing.join("(), ")}() method. Implement them, or mount it with readOnly: true.`,
        );
    }
    this.path = path;
    this.backend = backend;
    this.readOnly = readOnly;
    this.description = description;
    this.tracked = tracked;
  }
}

/** Reject mounts that repeat or nest, so every path belongs to at most one mount. */
export function validateMounts(mounts: readonly Mount[]): readonly Mount[] {
  const ordered = [...mounts].sort((a, b) => (a.path < b.path ? -1 : 1));
  for (let i = 1; i < ordered.length; i++) {
    const [outer, inner] = [ordered[i - 1]!, ordered[i]!];
    if (inner.path === outer.path || inner.path.startsWith(`${outer.path}/`))
      throw new TypeError(`mounts '${outer.path}' and '${inner.path}' overlap`);
  }
  return mounts;
}

/** The mount `path` falls in and the path relative to it, matching whole segments; null outside every mount. */
export function route(
  mounts: readonly Mount[],
  path: string,
): { mount: Mount; relative: string } | null {
  for (const mount of mounts) {
    if (path === mount.path) return { mount, relative: ROOT };
    if (path.startsWith(`${mount.path}/`))
      return { mount, relative: path.slice(mount.path.length + 1) };
  }
  return null;
}

// ---------------------------------------------------------------- the operation a step stopped at

/** Monty's file operations. Every other OS call (clock, entropy, environment) is not a file one. */
export const FILE_OPERATIONS = new Set([
  "open",
  "Path.exists",
  "Path.is_file",
  "Path.is_dir",
  "Path.is_symlink",
  "Path.read_text",
  "Path.read_bytes",
  "Path.write_text",
  "Path.write_bytes",
  "Path.append_text",
  "Path.append_bytes",
  "Path.mkdir",
  "Path.unlink",
  "Path.rmdir",
  "Path.iterdir",
  "Path.stat",
  "Path.rename",
  "Path.resolve",
  "Path.absolute",
]);

const WRITE_OPERATIONS = new Set([
  "Path.write_text",
  "Path.write_bytes",
  "Path.append_text",
  "Path.append_bytes",
  "Path.mkdir",
  "Path.unlink",
  "Path.rmdir",
  "Path.rename",
]);

/** An `open()`'s mode, passed by position or as `mode=`. */
function openMode({ args, kwargs }: FileOpCall): string {
  const mode = kwargs.mode ?? args[0];
  return typeof mode === "string" ? mode : "r";
}

function isWrite(op: FileOpCall): boolean {
  if (op.operation === "open") {
    const mode = openMode(op);
    return mode[0] !== "r" || mode.includes("+");
  }
  return WRITE_OPERATIONS.has(op.operation);
}

/** Bytes as they cross an activity boundary. */
export interface EncodedBytes {
  __monty__: "bytes";
  base64: string;
}
/** An `open` that succeeded: the worker answers it with a Monty file handle. */
export interface OpenedFile {
  __monty__: "handle";
  path: string;
  mode: string;
}
/** A file operation the workflow leaves to Monty's default: the error for an unavailable OS call. */
export interface NotHandled {
  __monty__: "not_handled";
}

export const encodeBytes = (data: Uint8Array): EncodedBytes => ({
  __monty__: "bytes",
  base64: encodeBase64(data),
});
const isEncodedBytes = (v: unknown): v is EncodedBytes =>
  typeof v === "object" && v !== null && (v as EncodedBytes).__monty__ === "bytes";

/** The file operation a script's step stopped at, as the worker hands it over. */
export interface FileOpCall {
  call_id: number;
  operation: string;
  /** The script's absolute path. */
  path: string;
  /** The operation's remaining arguments: the data of a write, the mode of an `open`, a rename's destination. */
  args: unknown[];
  kwargs: Record<string, unknown>;
}

const utf8 = new TextEncoder();
const fromUtf8 = new TextDecoder("utf-8", { fatal: true });
const toBytes = (data: unknown): Uint8Array =>
  isEncodedBytes(data) ? decodeBase64(data.base64) : utf8.encode(String(data));
const dataLength = (data: unknown): number =>
  isEncodedBytes(data) ? decodeBase64(data.base64).length : [...String(data)].length;

// ---------------------------------------------------------------- the fs_* tools

const FsInput = z.object({ mount: z.string(), path: z.string() });
const backendInjection = { backend: injected<FileSystem>() };

const fsSeed = tool({
  name: "fs_seed",
  description:
    "Load an in-memory filesystem's initial files, before the first operation on its mount.",
  inputSchema: z.object({ mount: z.string() }),
  injected: backendInjection,
  execute: async (_input, { backend }) => {
    const [count, size] = await (backend as InMemoryFileSystem).seedNow();
    return `seeded ${count} files (${size} bytes)`;
  },
});
const fsStat = tool({
  name: "fs_stat",
  description: "Look up a path's metadata in a Code Mode filesystem mount.",
  inputSchema: FsInput,
  injected: backendInjection,
  execute: async ({ path }, { backend }) => backend.stat(path),
});
const fsRead = tool({
  name: "fs_read",
  description: "Read a file from a Code Mode filesystem mount.",
  inputSchema: FsInput,
  injected: backendInjection,
  execute: async ({ path }, { backend }) => {
    // Text reads as text in the tool's events; anything else crosses as base64.
    const data = await backend.read(path);
    try {
      return fromUtf8.decode(data);
    } catch {
      return encodeBytes(data);
    }
  },
});
const fsList = tool({
  name: "fs_list",
  description: "List a directory in a Code Mode filesystem mount.",
  inputSchema: FsInput,
  injected: backendInjection,
  execute: async ({ path }, { backend }) => backend.list(path),
});
const fsWrite = tool({
  name: "fs_write",
  description:
    "Write a file in a Code Mode filesystem mount, replacing any existing content. Binary content arrives base64-encoded.",
  inputSchema: FsInput.extend({
    content: z.string(),
    encoding: z.enum(["utf-8", "base64"]).default("utf-8"),
  }),
  injected: backendInjection,
  execute: async ({ path, content, encoding }, { backend }) => {
    const data = encoding === "base64" ? decodeBase64(content) : utf8.encode(content);
    await backend.write!(path, data);
    return data.length;
  },
});
const fsMkdir = tool({
  name: "fs_mkdir",
  description: "Create a directory in a Code Mode filesystem mount.",
  inputSchema: FsInput,
  injected: backendInjection,
  execute: async ({ path }, { backend }) => {
    await backend.mkdir!(path);
    return null;
  },
});
const fsDelete = tool({
  name: "fs_delete",
  description: "Delete a file or an empty directory in a Code Mode filesystem mount.",
  inputSchema: FsInput,
  injected: backendInjection,
  execute: async ({ path }, { backend }) => {
    await backend.delete!(path);
    return null;
  },
});
const fsRename = tool({
  name: "fs_rename",
  description: "Move a file or directory to another path in the same Code Mode filesystem mount.",
  inputSchema: FsInput.extend({ target: z.string() }),
  injected: backendInjection,
  execute: async ({ path, target }, { backend }) => {
    await backend.rename!(path, target);
    return null;
  },
});

/** Runs one fs_* tool against a mount's backend, through the runner. */
export type CallFsTool = (
  tool: AnyHarnessTool,
  input: Record<string, unknown>,
  backend: FileSystem,
) => Promise<unknown>;

/** A mount's backend as a `FileSystem` whose every call is an fs_* tool call. */
function toolCalls(mount: Mount, run: CallFsTool): Required<FileSystem> {
  const m = mount.path;
  const track = mount.tracked;
  const call = (t: AnyHarnessTool, input: Record<string, unknown>) => run(t, input, mount.backend);
  return {
    stat: async (path) => {
      const found = (await call(fsStat as AnyHarnessTool, { mount: m, path })) as FileStat | null;
      track?.noteStat(path, found);
      return found;
    },
    read: async (path) => {
      const data = toBytes(await call(fsRead as AnyHarnessTool, { mount: m, path }));
      track?.noteRead(path, data.length);
      return data;
    },
    list: async (path) => {
      const names = (await call(fsList as AnyHarnessTool, { mount: m, path })) as string[];
      track?.noteList(path, names);
      return names;
    },
    write: async (path, data) => {
      let content: string;
      let encoding: "utf-8" | "base64" = "utf-8";
      try {
        content = fromUtf8.decode(data);
      } catch {
        [content, encoding] = [encodeBase64(data), "base64"];
      }
      await call(fsWrite as AnyHarnessTool, { mount: m, path, content, encoding });
      track?.noteWrite(path, data.length);
    },
    mkdir: async (path) => {
      await call(fsMkdir as AnyHarnessTool, { mount: m, path });
      track?.noteMkdir(path);
    },
    delete: async (path) => {
      await call(fsDelete as AnyHarnessTool, { mount: m, path });
      track?.noteRemove(path);
    },
    rename: async (path, target) => {
      await call(fsRename as AnyHarnessTool, { mount: m, path, target });
      track?.noteRename(path, target);
    },
  };
}

// ---------------------------------------------------------------- carrying out an operation

/**
 * What the script's file operation returns (encoded for the worker), or the failure it raises.
 * Routing, read-only mounts and renames across mounts are decided here, before any backend call.
 * A backend's error that names a mount-relative path is raised naming the virtual path instead.
 */
export async function performFileOp(
  mounts: readonly Mount[],
  op: FileOpCall,
  call: CallFsTool,
): Promise<unknown> {
  const virtual = normalize(op.path);
  const routed = virtual?.startsWith("/") ? route(mounts, virtual) : null;
  if (virtual === null || routed === null) return { __monty__: "not_handled" } satisfies NotHandled;
  const { mount, relative } = routed;
  if (op.operation === "Path.is_symlink") return false;
  if (op.operation === "Path.resolve" || op.operation === "Path.absolute") return virtual;
  if (mount.readOnly && isWrite(op)) throw osError("PermissionError", ERRNO.EACCES, virtual);
  let target: string | null = null;
  if (op.operation === "Path.rename") {
    const destination = normalize(String(op.args[0]));
    const routedTarget = destination === null ? null : route(mounts, destination);
    if (routedTarget === null || routedTarget.mount !== mount)
      throw osError("OSError", ERRNO.EXDEV, destination ?? String(op.args[0]));
    target = routedTarget.relative;
  }
  const fs = toolCalls(mount, async (t, input, backend) => {
    if (backend instanceof InMemoryFileSystem && backend.needsSeed)
      await call(fsSeed as AnyHarnessTool, { mount: mount.path }, backend);
    return call(t, input, backend);
  });
  try {
    return await perform(op, fs, relative, virtual, target);
  } catch (e) {
    const detail =
      e instanceof ApplicationFailure && OS_ERRORS.has(e.type ?? "")
        ? (e.details?.[0] as { errno: number; strerror: string; path: string } | undefined)
        : undefined;
    if (detail && !detail.path.startsWith("/"))
      throw osError(
        e instanceof ApplicationFailure ? (e.type as OsErrorType) : "OSError",
        detail.errno,
        `${mount.path}/${detail.path}`.replace(/\/\.$/, ""),
        detail.strerror,
      );
    throw e;
  }
}

async function perform(
  op: FileOpCall,
  fs: Required<FileSystem>,
  path: string,
  virtual: string,
  target: string | null,
): Promise<unknown> {
  const name = op.operation;
  const existing = async () => {
    const found = await fs.stat(path);
    if (found === null) throw osError("FileNotFoundError", ERRNO.ENOENT, virtual);
    return found;
  };
  switch (name) {
    case "Path.exists":
      return (await fs.stat(path)) !== null;
    case "Path.is_file":
    case "Path.is_dir": {
      const found = await fs.stat(path);
      return found !== null && found.is_dir === (name === "Path.is_dir");
    }
    case "Path.stat":
      throw ApplicationFailure.create({
        type: "NotImplementedError",
        nonRetryable: true,
        message:
          "Path.stat() is not available in this sandbox; use Path.exists(), Path.is_file() or Path.is_dir()",
      });
    case "open": {
      const mode = openMode(op);
      const found = await fs.stat(path);
      if (mode[0] === "x" && found !== null)
        throw osError("FileExistsError", ERRNO.EEXIST, virtual);
      if (found?.is_dir) throw osError("IsADirectoryError", ERRNO.EISDIR, virtual);
      if (mode[0] === "r" && found === null)
        throw osError("FileNotFoundError", ERRNO.ENOENT, virtual);
      if (mode[0] === "w" || ((mode[0] === "a" || mode[0] === "x") && found === null))
        await fs.write(path, new Uint8Array());
      return { __monty__: "handle", path: virtual, mode } satisfies OpenedFile;
    }
    case "Path.read_text":
    case "Path.read_bytes": {
      if ((await existing()).is_dir) throw osError("IsADirectoryError", ERRNO.EISDIR, virtual);
      const data = await fs.read(path);
      return name === "Path.read_text" ? new TextDecoder().decode(data) : encodeBytes(data);
    }
    case "Path.write_text":
    case "Path.write_bytes":
      await fs.write(path, toBytes(op.args[0]));
      return dataLength(op.args[0]);
    case "Path.append_text":
    case "Path.append_bytes": {
      const found = await fs.stat(path);
      if (found?.is_dir) throw osError("IsADirectoryError", ERRNO.EISDIR, virtual);
      const before = found !== null ? await fs.read(path) : new Uint8Array();
      const added = toBytes(op.args[0]);
      const joined = new Uint8Array(before.length + added.length);
      joined.set(before);
      joined.set(added, before.length);
      await fs.write(path, joined);
      return dataLength(op.args[0]);
    }
    case "Path.iterdir": {
      if (!(await existing()).is_dir) throw osError("NotADirectoryError", ERRNO.ENOTDIR, virtual);
      return [...(await fs.list(path))].sort().map((entry) => `${virtual}/${entry}`);
    }
    case "Path.mkdir": {
      const found = await fs.stat(path);
      if (found !== null) {
        if (op.kwargs.exist_ok && found.is_dir) return null;
        throw osError("FileExistsError", ERRNO.EEXIST, virtual);
      }
      const missing: string[] = [];
      for (const ancestor of ancestors(path)) {
        const above = await fs.stat(ancestor);
        if (above === null) {
          if (!op.kwargs.parents) throw osError("FileNotFoundError", ERRNO.ENOENT, virtual);
          missing.push(ancestor);
        } else if (!above.is_dir) throw osError("NotADirectoryError", ERRNO.ENOTDIR, virtual);
      }
      for (const directory of [...missing, path]) await fs.mkdir(directory);
      return null;
    }
    case "Path.unlink":
      if ((await existing()).is_dir) throw osError("IsADirectoryError", ERRNO.EISDIR, virtual);
      await fs.delete(path);
      return null;
    case "Path.rmdir":
      if (path === ROOT) throw osError("PermissionError", ERRNO.EACCES, virtual);
      if (!(await existing()).is_dir) throw osError("NotADirectoryError", ERRNO.ENOTDIR, virtual);
      if ((await fs.list(path)).length) throw osError("OSError", ERRNO.ENOTEMPTY, virtual);
      await fs.delete(path);
      return null;
    case "Path.rename":
      await existing();
      await fs.rename(path, target!);
      return null;
    default:
      return { __monty__: "not_handled" } satisfies NotHandled;
  }
}

// ---------------------------------------------------------------- the in-memory backend

const FileEntry = z.object({
  content: z.string(),
  encoding: z.enum(["utf-8", "base64"]).default("utf-8"),
  size: z.number().int(),
});
/**
 * An `InMemoryFileSystem`'s files as agent state: `files` keyed by mount-relative path
 * (`"notes/todo.md"`), and `directories` lists every directory except the mount's root. Declare
 * it with `states: { workspace: state(FileTree) }` and pass the ref as
 * `new InMemoryFileSystem({ state })`: the files then live in the state, and every change to them
 * is published as a state patch.
 */
export const FileTree = z
  .object({
    files: z.record(z.string(), FileEntry).default({}),
    directories: z.array(z.string()).default([]),
  })
  .meta({ title: "FileTree" });
export type FileTree = z.output<typeof FileTree>;

function entry(data: Uint8Array): z.output<typeof FileEntry> {
  try {
    return { content: fromUtf8.decode(data), encoding: "utf-8", size: data.length };
  } catch {
    return { content: encodeBase64(data), encoding: "base64", size: data.length };
  }
}
const content = (e: z.output<typeof FileEntry>) =>
  e.encoding === "base64" ? decodeBase64(e.content) : utf8.encode(e.content);

/** One change to an in-memory tree, applied in this order: files dropped, directories removed, directories added, files put. */
interface Change {
  drop?: string[];
  rmdirs?: string[];
  mkdirs?: string[];
  put?: Map<string, Uint8Array>;
}

interface Store {
  content(path: string): Uint8Array | null;
  size(path: string): number | null;
  isDir(path: string): boolean;
  paths(): string[];
  total(): number;
  apply(change: Change): void;
}

function memoryStore(): Store {
  const files = new Map<string, Uint8Array>();
  const dirs = new Set<string>();
  return {
    content: (path) => files.get(path) ?? null,
    size: (path) => files.get(path)?.length ?? null,
    isDir: (path) => path === ROOT || dirs.has(path),
    paths: () => [...dirs, ...files.keys()],
    total: () => [...files.values()].reduce((n, d) => n + d.length, 0),
    apply({ drop = [], rmdirs = [], mkdirs = [], put = new Map() }) {
      for (const path of drop) files.delete(path);
      for (const path of rmdirs) dirs.delete(path);
      for (const path of mkdirs) dirs.add(path);
      for (const [path, data] of put) files.set(path, data);
    },
  };
}

function stateStore<T extends { files: FileTree["files"]; directories: string[] }>(
  ref: StateRef<T>,
): Store {
  return {
    content: (path) => {
      const e = ref.current.files[path];
      return e ? content(e) : null;
    },
    size: (path) => ref.current.files[path]?.size ?? null,
    isDir: (path) => path === ROOT || ref.current.directories.includes(path),
    paths: () => [...ref.current.directories, ...Object.keys(ref.current.files)],
    total: () => Object.values(ref.current.files).reduce((n, e) => n + e.size, 0),
    apply({ drop = [], rmdirs = [], mkdirs = [], put = new Map() }) {
      ref.mutate((draft) => {
        for (const path of drop) delete draft.files[path];
        if (rmdirs.length) draft.directories = draft.directories.filter((d) => !rmdirs.includes(d));
        draft.directories.push(...mkdirs);
        for (const [path, data] of put) draft.files[path] = entry(data);
      });
    },
  };
}

/** The initial files of an `InMemoryFileSystem`, as `{ mount-relative path: content }`. */
export type Seed = () => Promise<Record<string, string | Uint8Array>>;

/**
 * A `FileSystem` held in workflow memory. Build it in `create`, so each workflow gets its own; its
 * files then last across the run's scripts, and replay rebuilds them. A continue-as-new carries
 * only declared states, so without `state` the next run starts empty and seeds again.
 *
 * `seed` runs in workflow code, once, before the first operation on the mount, and returns the
 * initial files. Load files from disk inside an activity the seed awaits: the activity's result is
 * recorded, so a running workflow keeps the files it seeded even after a deploy changes them.
 * `maxBytes` caps the total size of the files, seeded ones included.
 *
 * `state` (opt-in) keeps the files in a declared `FileTree` state instead, so every change is
 * published as a state patch the UI can show. A patch carries each written file whole, and anyone
 * who can view the session sees the files, so track only filesystems whose files are worth
 * streaming to every viewer.
 */
export class InMemoryFileSystem<
  T extends { files: FileTree["files"]; directories: string[] } = FileTree,
> implements Required<FileSystem> {
  readonly #seed: Seed | undefined;
  #seeded: boolean;
  readonly #maxBytes: number | undefined;
  readonly #store: Store;

  constructor({
    seed,
    maxBytes,
    state,
  }: { seed?: Seed; maxBytes?: number; state?: StateRef<T> } = {}) {
    this.#seed = seed;
    this.#seeded = seed === undefined;
    this.#maxBytes = maxBytes;
    this.#store = state ? stateStore(state) : memoryStore();
  }

  get needsSeed(): boolean {
    return !this.#seeded;
  }

  /** Run the seed and load its files, as one change. Called by the fs_seed tool. */
  async seedNow(): Promise<[count: number, bytes: number]> {
    const files = await this.#seed!();
    const mkdirs: string[] = [];
    const put = new Map<string, Uint8Array>();
    for (const [raw, data] of Object.entries(files)) {
      const path = normalize(raw);
      if (path === null || path.startsWith("/") || path === "")
        throw new TypeError(`seeded path '${raw}' must name a file inside the mount`);
      for (const ancestor of ancestors(path))
        if (!this.#store.isDir(ancestor) && !mkdirs.includes(ancestor)) mkdirs.push(ancestor);
      put.set(path, typeof data === "string" ? utf8.encode(data) : data);
    }
    this.#checkBudget(put);
    this.#store.apply({ mkdirs, put });
    this.#seeded = true;
    return [put.size, [...put.values()].reduce((n, d) => n + d.length, 0)];
  }

  #checkBudget(put: Map<string, Uint8Array>): void {
    if (this.#maxBytes === undefined) return;
    const replaced = [...put.keys()].reduce((n, p) => n + (this.#store.size(p) ?? 0), 0);
    const total =
      this.#store.total() - replaced + [...put.values()].reduce((n, d) => n + d.length, 0);
    if (total > this.#maxBytes) {
      throw osError(
        "OSError",
        ERRNO.ENOSPC,
        put.keys().next().value!,
        `No space left on device: the filesystem is limited to ${this.#maxBytes} bytes`,
      );
    }
  }

  async stat(path: string): Promise<FileStat | null> {
    if (this.#store.isDir(path)) return { is_dir: true };
    const size = this.#store.size(path);
    return size === null ? null : { is_dir: false, size };
  }

  async read(path: string): Promise<Uint8Array> {
    if (this.#store.isDir(path)) throw osError("IsADirectoryError", ERRNO.EISDIR, path);
    const data = this.#store.content(path);
    if (data === null) throw osError("FileNotFoundError", ERRNO.ENOENT, path);
    return data;
  }

  async list(path: string): Promise<string[]> {
    if (!this.#store.isDir(path)) throw osError("NotADirectoryError", ERRNO.ENOTDIR, path);
    return this.#store
      .paths()
      .filter((p) => p !== path && parentOf(p) === path)
      .map(nameOf)
      .sort();
  }

  async write(path: string, data: Uint8Array): Promise<void> {
    if (this.#store.isDir(path)) throw osError("IsADirectoryError", ERRNO.EISDIR, path);
    if (!this.#store.isDir(parentOf(path))) throw osError("FileNotFoundError", ERRNO.ENOENT, path);
    const put = new Map([[path, data]]);
    this.#checkBudget(put);
    this.#store.apply({ put });
  }

  async mkdir(path: string): Promise<void> {
    if (this.#store.isDir(path) || this.#store.size(path) !== null)
      throw osError("FileExistsError", ERRNO.EEXIST, path);
    if (!this.#store.isDir(parentOf(path))) throw osError("FileNotFoundError", ERRNO.ENOENT, path);
    this.#store.apply({ mkdirs: [path] });
  }

  async delete(path: string): Promise<void> {
    if (this.#store.size(path) !== null) return this.#store.apply({ drop: [path] });
    if (this.#store.isDir(path) && path !== ROOT) {
      if ((await this.list(path)).length) throw osError("OSError", ERRNO.ENOTEMPTY, path);
      return this.#store.apply({ rmdirs: [path] });
    }
    throw osError("FileNotFoundError", ERRNO.ENOENT, path);
  }

  async rename(path: string, target: string): Promise<void> {
    const store = this.#store;
    if (!store.isDir(parentOf(target))) throw osError("FileNotFoundError", ERRNO.ENOENT, target);
    const data = store.content(path);
    if (data !== null) {
      if (store.isDir(target)) throw osError("IsADirectoryError", ERRNO.EISDIR, target);
      return store.apply({ drop: [path], put: new Map([[target, data]]) });
    }
    if (!store.isDir(path) || path === ROOT) throw osError("FileNotFoundError", ERRNO.ENOENT, path);
    if (target === path || target.startsWith(`${path}/`))
      throw osError("OSError", ERRNO.EINVAL, target);
    if (store.size(target) !== null || (store.isDir(target) && (await this.list(target)).length))
      throw osError("OSError", ERRNO.ENOTEMPTY, target);
    const change: Required<Change> = {
      drop: [],
      rmdirs: store.isDir(target) ? [target] : [],
      mkdirs: [],
      put: new Map(),
    };
    for (const old of store.paths()) {
      if (old !== path && !old.startsWith(`${path}/`)) continue;
      const moved = target + old.slice(path.length);
      const bytes = store.content(old);
      if (bytes === null) {
        change.rmdirs.push(old);
        change.mkdirs.push(moved);
      } else {
        change.drop.push(old);
        change.put.set(moved, bytes);
      }
    }
    store.apply(change);
  }
}

// ---------------------------------------------------------------- the file index

// What a session has seen of an activity-backed mount. Contents stay in the store; the index
// only records paths and metadata, and where a viewer can read the files later.


export const IndexEntry = z.object({
  is_dir: z.boolean().nullable().default(null),
  size: z.number().nullable().default(null),
  mtime: z.number().nullable().default(null),
  writes: z.number().int().default(0),
});
export type IndexEntry = z.output<typeof IndexEntry>;

export const IndexSource = z.object({
  filesystem: z.string(),
  config: z.record(z.string(), z.unknown()),
  task_queue: z.string(),
});
export type IndexSource = z.output<typeof IndexSource>;

export const FileIndex = z
  .object({
    kind: z.literal("file_index").default("file_index"),
    mount: z.string().default(""),
    description: z.string().default(""),
    read_only: z.boolean().default(false),
    okf_version: z.string().nullable().default(null),
    source: IndexSource.nullable().default(null),
    entries: z.record(z.string(), IndexEntry).default({}),
  })
  .meta({ title: "FileIndex" });
export type FileIndex = z.output<typeof FileIndex>;

/** Where an activity-backed mount's files live, before the task queue is known. */
export interface FileSource {
  filesystem: string;
  config: Record<string, unknown>;
}

/** Records successful mount operations. A no-op change publishes nothing. */
export interface IndexTrack {
  noteStat(path: string, found: FileStat | null): void;
  noteRead(path: string, size: number): void;
  noteList(path: string, names: readonly string[]): void;
  noteWrite(path: string, size: number): void;
  noteMkdir(path: string): void;
  noteRemove(path: string): void;
  noteRename(path: string, target: string): void;
}

const blank = (): IndexEntry => ({ is_dir: null, size: null, mtime: null, writes: 0 });

const same = (a: IndexEntry | undefined, b: IndexEntry) =>
  a !== undefined &&
  a.is_dir === b.is_dir &&
  a.size === b.size &&
  a.mtime === b.mtime &&
  a.writes === b.writes;

function put(entries: Record<string, IndexEntry>, key: string, entry: IndexEntry): void {
  if (!same(entries[key], entry)) entries[key] = entry;
}

function putDir(entries: Record<string, IndexEntry>, path: string): void {
  if (path === "." || path === "") return;
  const parts = path.split("/");
  for (let i = 1; i <= parts.length; i++) {
    const directory = parts.slice(0, i).join("/");
    const found = entries[directory];
    if (found === undefined || found.is_dir !== true)
      put(entries, directory, { ...blank(), is_dir: true });
  }
}

function drop(entries: Record<string, IndexEntry>, path: string): void {
  for (const key of Object.keys(entries)) {
    if (key === path || key.startsWith(`${path}/`)) delete entries[key];
  }
}

function putFile(
  entries: Record<string, IndexEntry>,
  path: string,
  size: number,
  mtime: number | null,
): void {
  const known = entries[path];
  putDir(entries, parentOf(path));
  put(entries, path, {
    is_dir: false,
    size,
    mtime,
    writes: known?.writes ?? 0,
  });
}


function inWorkflow(): boolean {
  try {
    return inWorkflowContext();
  } catch {
    return false;
  }
}

export function indexTrack(ref: StateRef<FileIndex>, source: FileSource): IndexTrack {
  const change = (recipe: (entries: Record<string, IndexEntry>) => void) => {
    ref.mutate((index) => {
      if (index.source === null && inWorkflow()) {
        index.source = {
          filesystem: source.filesystem,
          config: source.config,
          task_queue: workflowInfo().taskQueue,
        };
      }
      recipe(index.entries);
    });
  };
  return {
    noteStat(path, found) {
      if (path === ".") return;
      change((entries) => {
        if (found === null) drop(entries, path);
        else if (found.is_dir) putDir(entries, path);
        else putFile(entries, path, found.size ?? 0, found.mtime ? found.mtime : null);
      });
    },
    noteRead(path, size) {
      change((entries) => {
        const known = entries[path];
        const mtime = known !== undefined && known.size === size ? known.mtime : null;
        putFile(entries, path, size, mtime);
      });
    },
    noteList(path, names) {
      change((entries) => {
        putDir(entries, path);
        const listed = new Set(names.map((name) => (path === "." ? name : `${path}/${name}`)));
        for (const key of Object.keys(entries)) {
          if (parentOf(key) === path && !listed.has(key)) drop(entries, key);
        }
        for (const key of [...listed].sort()) if (!(key in entries)) entries[key] = blank();
      });
    },
    noteWrite(path, size) {
      change((entries) => {
        const known = entries[path];
        putDir(entries, parentOf(path));
        entries[path] = {
          is_dir: false,
          size,
          mtime: null,
          writes: (known?.writes ?? 0) + 1,
        };
      });
    },
    noteMkdir(path) {
      change((entries) => putDir(entries, path));
    },
    noteRemove(path) {
      change((entries) => drop(entries, path));
    },
    noteRename(path, target) {
      change((entries) => {
        const moving = Object.entries(entries).filter(
          ([key]) => key === path || key.startsWith(`${path}/`),
        );
        drop(entries, target);
        drop(entries, path);
        putDir(entries, parentOf(target));
        for (const [key, entry] of moving) {
          const rest = key === path ? "" : key.slice(path.length + 1);
          entries[rest ? `${target}/${rest}` : target] = { ...entry };
        }
      });
    },
  };
}
