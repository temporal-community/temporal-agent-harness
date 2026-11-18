// Declaring a Code Mode mount as agent state. An in-memory mount keeps its files in a FileTree.
// An activity-backed mount keeps a FileIndex of the paths the session has touched.

import { z } from "zod";
import type { StateDecl, StateRef } from "../state/index.ts";
import { type ActivityFilesystem } from "./activity-fs.ts";
import { FileIndex, indexTrack } from "./vfs.ts";
import { OKF_VERSION } from "./okf.ts";
import { FileTree, InMemoryFileSystem, Mount, type Seed } from "./vfs.ts";

const DeclaredFileTree = FileTree.extend({
  kind: z.literal("file_tree").default("file_tree"),
  mount: z.string().default(""),
  description: z.string().default(""),
  read_only: z.boolean().default(false),
});
type DeclaredFileTree = z.output<typeof DeclaredFileTree>;

export interface VfsMountDecl<
  S extends z.ZodType<Record<string, unknown>>,
  C,
> extends StateDecl<S> {
  bind(ref: StateRef<z.output<S>>, config: C, options?: { timeout?: string | number }): Mount;
}

interface MemoryBind {
  seed?: Seed;
  maxBytes?: number;
}

function normalized(path: string): string {
  const parts: string[] = [];
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (!parts.length) return path;
      parts.pop();
    } else parts.push(part);
  }
  return `/${parts.join("/")}`;
}

function checkPath(path: string): void {
  if (!path.startsWith("/") || normalized(path) !== path)
    throw new TypeError(
      `mount path '${path}' must be an absolute, normalized POSIX path, like '/workspace'`,
    );
  if (path === "/")
    throw new TypeError("a mount cannot be the root '/'; mount at a named directory");
}

function readOnlyOnReadOnlyFs(
  what: string,
  path: string,
  fs: { name: string; writable: boolean },
  readOnly: boolean,
) {
  if (!readOnly && !fs.writable)
    throw new TypeError(
      `${what}('${path}'): ${fs.name} is read-only (it implements no write methods); declare the mount with readOnly: true`,
    );
}

function isActivityFilesystem(value: unknown): value is ActivityFilesystem<unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { kind?: unknown }).kind === "activity-filesystem" &&
    typeof (value as { backend?: unknown }).backend === "function"
  );
}

export function vfsMount(
  path: string,
  backend: typeof InMemoryFileSystem,
  options: { description: string; readOnly?: boolean },
): VfsMountDecl<typeof DeclaredFileTree, MemoryBind | undefined>;
export function vfsMount<C>(
  path: string,
  backend: ActivityFilesystem<C>,
  options: { description: string; readOnly?: boolean },
): VfsMountDecl<typeof FileIndex, C>;
export function vfsMount(
  path: string,
  backend: typeof InMemoryFileSystem | ActivityFilesystem<unknown>,
  options: { description: string; readOnly?: boolean },
): VfsMountDecl<z.ZodType<Record<string, unknown>>, unknown> {
  checkPath(path);
  const readOnly = options.readOnly ?? false;
  if (backend === InMemoryFileSystem) {
    const decl: VfsMountDecl<typeof DeclaredFileTree, MemoryBind | undefined> = {
      schema: DeclaredFileTree,
      initial: () => ({
        kind: "file_tree",
        mount: path,
        description: options.description,
        read_only: readOnly,
        files: {},
        directories: [],
      }),
      bind(ref, config = {}) {
        return new Mount(path, new InMemoryFileSystem({ state: ref, ...config }), {
          readOnly,
          description: options.description,
        });
      },
    };
    return decl;
  }
  if (!isActivityFilesystem(backend))
    throw new TypeError(
      `vfsMount('${path}'): the backend must be InMemoryFileSystem or an ActivityFileSystem`,
    );
  return activityDecl("vfsMount", path, backend, options.description, readOnly, null);
}

export function okfBundleVfsMount<C>(
  path: string,
  backend: ActivityFilesystem<C>,
  options: { description: string; readOnly?: boolean },
): VfsMountDecl<typeof FileIndex, C> {
  checkPath(path);
  if (!isActivityFilesystem(backend))
    throw new TypeError(`okfBundleVfsMount('${path}'): the backend must be an ActivityFileSystem`);
  return activityDecl<C>(
    "okfBundleVfsMount",
    path,
    backend,
    options.description,
    options.readOnly ?? false,
    OKF_VERSION,
  );
}

function activityDecl<C>(
  what: string,
  path: string,
  backend: ActivityFilesystem<C>,
  description: string,
  readOnly: boolean,
  okfVersion: string | null,
): VfsMountDecl<typeof FileIndex, C> {
  readOnlyOnReadOnlyFs(what, path, backend, readOnly);
  return {
    schema: FileIndex,
    initial: () => ({
      kind: "file_index",
      mount: path,
      description,
      read_only: readOnly,
      okf_version: okfVersion,
      source: null,
      entries: {},
    }),
    bind(ref, config, options) {
      const fs = backend.backend(config, { timeout: options?.timeout, okfVersion });
      return new Mount(path, fs, {
        readOnly,
        description,
        tracked: indexTrack(ref, fs.source),
      });
    },
  };
}
