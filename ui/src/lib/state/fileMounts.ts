/**
 * Code Mode mounts as file trees, folded out of the agent states that track them.
 *
 * An agent opts a mount in with one of two states, both published like any other:
 *
 *   - a `FileTree` (`kind: "file_tree"`) holds an in-memory filesystem's files, contents
 *     included, so a file's contents are part of the state and follow the cursor;
 *   - a `FileIndex` (`kind: "file_index"`) holds any mount's paths and metadata, never
 *     contents. When it has a `source`, a file's contents can be fetched from the store,
 *     but only as they are now, never as they were at the cursor.
 *
 * Both carry their mount's path, description and access, so a pane can show every
 * tracked mount without knowing which state belongs to which. An index whose `okf_version`
 * is set is an OKF bundle: the pane can draw its whole graph, read live from the store.
 */
import type { FileSource, JsonValue } from "$lib/api/types";
import type { AgentStateDoc, StateChange } from "./agentState";
import { parsePointer } from "./jsonPatch";

export type MountKind = "tree" | "index";

export interface FileNode {
  /** Mount-relative path; `""` for the mount's root. */
  path: string;
  name: string;
  /** `null` for a name seen only in a listing: not yet known to be a file or a directory. */
  isDir: boolean | null;
  size: number | null;
  mtime: number | null;
  /** How many times the session has written the file, per its index. */
  writes: number;
  /** A `FileTree` file's contents, as of the cursor. Always `null` in an index. */
  content: { text: string; encoding: "utf-8" | "base64" } | null;
  /** The commit at the cursor touched this path, or something under it. */
  changed: boolean;
  /** Differs whenever anything known about the node does, so a viewer can tell it changed. */
  version: string;
  children: FileNode[];
}

export interface MountView {
  /** The state's key: unique across the run's agents. */
  key: string;
  kind: MountKind;
  mount: string;
  description: string;
  readOnly: boolean;
  /** Which agent the mount belongs to. */
  label: string;
  role: "parent" | "subagent";
  /** Where contents can be read now, for an index over an activity-backed mount. */
  source: FileSource | null;
  /** The OKF version of a mount declared as an OKF bundle; `null` for any other mount. */
  okfVersion: string | null;
  /** The tracking state's version: it changes whenever anything about the mount does. */
  stateVersion: number;
  root: FileNode;
  fileCount: number;
  problem: string | null;
}

type Json = { [key: string]: JsonValue };

function record(value: JsonValue | null | undefined): Json | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Json) : null;
}

function str(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}

function num(value: JsonValue | undefined): number | null {
  return typeof value === "number" ? value : null;
}

/** A state that tracks a mount, rather than one an author declared for its own use. */
export function isFileStateDoc(doc: AgentStateDoc): boolean {
  const kind = record(doc.value)?.kind;
  return kind === "file_tree" || kind === "file_index";
}

/** The mount-relative paths a commit touched. Header fields touch none. */
function changedPaths(changes: StateChange[], kind: MountKind, value: Json): Set<string> {
  const paths = new Set<string>();
  for (const change of changes) {
    const [field, key] = parsePointer(change.path);
    if (key === undefined) continue;
    if (kind === "index" && field === "entries") paths.add(key);
    if (kind === "tree" && field === "files") paths.add(key);
    if (kind === "tree" && field === "directories") {
      // An array slot: the directory is the value added or removed there.
      const dir = change.value ?? change.before ?? (value.directories as JsonValue[])?.[Number(key)];
      if (typeof dir === "string") paths.add(dir);
    }
  }
  return paths;
}

function node(path: string): FileNode {
  return {
    path,
    name: path.split("/").pop() ?? path,
    isDir: path === "" ? true : null,
    size: null,
    mtime: null,
    writes: 0,
    content: null,
    changed: false,
    version: "",
    children: []
  };
}

function buildTree(
  entries: Array<[string, Partial<FileNode>]>,
  changed: Set<string>
): { root: FileNode; fileCount: number } {
  const nodes = new Map<string, FileNode>([["", node("")]]);
  const ensure = (path: string): FileNode => {
    let found = nodes.get(path);
    if (found) return found;
    found = node(path);
    nodes.set(path, found);
    const parentPath = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    const parent = ensure(parentPath);
    parent.isDir = true;
    parent.children.push(found);
    return found;
  };
  for (const [path, facts] of entries) Object.assign(ensure(path), facts);

  let fileCount = 0;
  const finish = (current: FileNode): boolean => {
    current.children.sort(
      (a, b) => Number(b.isDir === true) - Number(a.isDir === true) || a.name.localeCompare(b.name)
    );
    let below = false;
    for (const child of current.children) below = finish(child) || below;
    if (current.isDir === false) fileCount += 1;
    current.changed = changed.has(current.path) || below;
    current.version = JSON.stringify([
      current.isDir,
      current.size,
      current.mtime,
      current.writes,
      current.content
    ]);
    return current.changed;
  };
  const root = nodes.get("")!;
  finish(root);
  return { root, fileCount };
}

function indexEntries(value: Json): Array<[string, Partial<FileNode>]> {
  const entries = record(value.entries) ?? {};
  return Object.entries(entries).map(([path, raw]) => {
    const entry = record(raw) ?? {};
    const isDir = typeof entry.is_dir === "boolean" ? entry.is_dir : null;
    return [
      path,
      { isDir, size: num(entry.size), mtime: num(entry.mtime), writes: num(entry.writes) ?? 0 }
    ];
  });
}

function treeEntries(value: Json): Array<[string, Partial<FileNode>]> {
  const dirs = Array.isArray(value.directories) ? value.directories : [];
  const files = record(value.files) ?? {};
  return [
    ...dirs.filter((d): d is string => typeof d === "string").map(
      (d): [string, Partial<FileNode>] => [d, { isDir: true }]
    ),
    ...Object.entries(files).map(([path, raw]): [string, Partial<FileNode>] => {
      const entry = record(raw) ?? {};
      const encoding = entry.encoding === "base64" ? "base64" : "utf-8";
      return [
        path,
        { isDir: false, size: num(entry.size), content: { text: str(entry.content), encoding } }
      ];
    })
  ];
}

function source(value: JsonValue | undefined): FileSource | null {
  const raw = record(value);
  if (!raw) return null;
  return {
    filesystem: str(raw.filesystem),
    config: record(raw.config) ?? {},
    task_queue: str(raw.task_queue)
  };
}

/** Every tracked mount among `docs`, parents' first, in the order the states were declared. */
export function buildMountViews(docs: AgentStateDoc[]): MountView[] {
  const views: MountView[] = [];
  for (const doc of docs) {
    if (!isFileStateDoc(doc)) continue;
    const value = record(doc.value)!;
    const kind: MountKind = value.kind === "file_tree" ? "tree" : "index";
    const { root, fileCount } = buildTree(
      kind === "tree" ? treeEntries(value) : indexEntries(value),
      changedPaths(doc.changed, kind, value)
    );
    views.push({
      key: doc.key,
      kind,
      mount: str(value.mount) || doc.stateId,
      description: str(value.description),
      readOnly: value.read_only === true,
      label: doc.label,
      role: doc.role,
      source: kind === "index" ? source(value.source) : null,
      okfVersion: kind === "index" && str(value.okf_version) ? str(value.okf_version) : null,
      stateVersion: doc.version,
      root,
      fileCount,
      problem: doc.problem
    });
  }
  return views.sort((a, b) => Number(a.role === "subagent") - Number(b.role === "subagent"));
}

/** The node at `path` in `root`, or `null` once it is gone. */
export function findNode(root: FileNode, path: string): FileNode | null {
  let current: FileNode | undefined = root;
  let prefix = "";
  for (const part of path === "" ? [] : path.split("/")) {
    prefix = prefix ? `${prefix}/${part}` : part;
    current = current?.children.find((child) => child.path === prefix);
  }
  return current ?? null;
}

export type FileView =
  | { mode: "markdown" }
  | { mode: "code"; language: string }
  | { mode: "text" };

const CODE_LANGUAGES: Record<string, string> = {
  c: "c",
  cpp: "cpp",
  cs: "csharp",
  css: "css",
  go: "go",
  h: "c",
  html: "html",
  java: "java",
  js: "javascript",
  json: "json",
  jsx: "javascript",
  mjs: "javascript",
  py: "python",
  rb: "ruby",
  rs: "rust",
  sh: "shell",
  sql: "sql",
  svelte: "svelte",
  ts: "typescript",
  tsx: "typescript",
  yaml: "yaml",
  yml: "yaml"
};

/** How to show a file, from its name. */
export function fileView(path: string): FileView {
  const ext = path.includes(".") ? path.slice(path.lastIndexOf(".") + 1).toLowerCase() : "";
  if (ext === "md" || ext === "markdown") return { mode: "markdown" };
  const language = CODE_LANGUAGES[ext];
  return language ? { mode: "code", language } : { mode: "text" };
}

/** Bytes of a file's contents as the wire carries them. */
export function contentBytes(text: string, encoding: "utf-8" | "base64"): Uint8Array {
  if (encoding === "utf-8") return new TextEncoder().encode(text);
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** `bytes` as text, or `null` when they are not UTF-8 text. */
export function decodeText(bytes: Uint8Array): string | null {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

/** A file size as a short label: `512 B`, `2.4 KB`, `1.1 MB`. Empty when it isn't known. */
export function formatSize(size: number | null): string {
  if (size === null) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
