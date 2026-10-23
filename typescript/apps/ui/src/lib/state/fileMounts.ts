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
  /** How the commit at the cursor touched this path. A removed node is what stood there before. */
  change: FileChange | null;
  /** The commit at the cursor touched something under this path. */
  changedBelow: boolean;
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
  /** Files the commit at the cursor added, modified and removed. */
  changes: Record<FileChange, number>;
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

/** How the commit at the cursor touched a path. */
export type FileChange = "added" | "modified" | "removed";

function opChange(op: StateChange["op"]): FileChange {
  switch (op) {
    case "add":
      return "added";
    case "replace":
      return "modified";
    case "remove":
      return "removed";
    default: {
      const unhandled: never = op;
      return unhandled;
    }
  }
}

const writesOf = (entry: JsonValue | undefined) => num(record(entry ?? null)?.writes) ?? 0;

/**
 * How a commit changed an index entry. An index records every path the session looks up,
 * lists or reads, so an entry appearing or changing is a write only when its count of
 * writes went up; a path that was only read has no change.
 */
function indexChange(change: StateChange, rest: string[]): FileChange | null {
  if (rest.length > 0) return rest[0] === "writes" ? "modified" : null;
  if (change.op === "remove") return "removed";
  if (writesOf(change.value) <= writesOf(change.before)) return null;
  return change.before === undefined ? "added" : "modified";
}

/**
 * The mount-relative paths a commit touched, how, and for a removed path what stood there,
 * so it can stay in the tree as a removed row. Header fields touch none.
 */
function changedPaths(
  changes: StateChange[],
  kind: MountKind,
  value: Json
): Map<string, { change: FileChange; ghost: Partial<FileNode> | null }> {
  const paths = new Map<string, { change: FileChange; ghost: Partial<FileNode> | null }>();
  for (const change of changes) {
    const [field, key, ...rest] = parsePointer(change.path);
    if (key === undefined) continue;
    /* An op on a field inside an entry edits the entry; only one on the entry itself adds or removes it. */
    const how = rest.length > 0 ? "modified" : opChange(change.op);
    const removed = how === "removed" ? record(change.before ?? null) : null;
    if (kind === "index" && field === "entries") {
      const written = indexChange(change, rest);
      if (written) paths.set(key, { change: written, ghost: removed && indexFacts(removed) });
    }
    if (kind === "tree" && field === "files") {
      paths.set(key, { change: how, ghost: removed && { isDir: false, size: num(removed.size) } });
    }
    if (kind === "tree" && field === "directories") {
      // An array slot: the directory is the value added or removed there.
      const dir = change.value ?? change.before ?? (value.directories as JsonValue[])?.[Number(key)];
      if (typeof dir === "string") paths.set(dir, { change: how, ghost: how === "removed" ? { isDir: true } : null });
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
    change: null,
    changedBelow: false,
    version: "",
    children: []
  };
}

/* Numeric, so `new_4.md` comes before `new_36.md`. */
const byName = new Intl.Collator(undefined, { numeric: true });

function buildTree(
  entries: Array<[string, Partial<FileNode>]>,
  changed: Map<string, { change: FileChange; ghost: Partial<FileNode> | null }>
): { root: FileNode; fileCount: number; changes: Record<FileChange, number> } {
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
  /* A removed path is gone from the state; it stays in the tree as what stood there. */
  for (const [path, { ghost }] of changed) {
    if (ghost && !nodes.has(path)) Object.assign(ensure(path), ghost);
  }

  let fileCount = 0;
  const changes: Record<FileChange, number> = { added: 0, modified: 0, removed: 0 };
  const finish = (current: FileNode): boolean => {
    current.children.sort(
      (a, b) => Number(b.isDir === true) - Number(a.isDir === true) || byName.compare(a.name, b.name)
    );
    let below = false;
    for (const child of current.children) below = finish(child) || below;
    current.change = changed.get(current.path)?.change ?? null;
    current.changedBelow = below;
    if (current.isDir === false && current.change !== "removed") fileCount += 1;
    if (current.isDir !== true && current.change) changes[current.change] += 1;
    current.version = JSON.stringify([
      current.isDir,
      current.size,
      current.mtime,
      current.writes,
      current.content,
      current.change
    ]);
    return current.change !== null || below;
  };
  const root = nodes.get("")!;
  finish(root);
  return { root, fileCount, changes };
}

function indexFacts(entry: Json): Partial<FileNode> {
  const isDir = typeof entry.is_dir === "boolean" ? entry.is_dir : null;
  return { isDir, size: num(entry.size), mtime: num(entry.mtime), writes: num(entry.writes) ?? 0 };
}

function indexEntries(value: Json): Array<[string, Partial<FileNode>]> {
  const entries = record(value.entries) ?? {};
  return Object.entries(entries).map(([path, raw]) => [path, indexFacts(record(raw) ?? {})]);
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
    const readOnly = value.read_only === true;
    /* The session can't write a read-only mount, so nothing in it was added, changed or removed. */
    const { root, fileCount, changes } = buildTree(
      kind === "tree" ? treeEntries(value) : indexEntries(value),
      readOnly ? new Map() : changedPaths(doc.changed, kind, value)
    );
    views.push({
      key: doc.key,
      kind,
      mount: str(value.mount) || doc.stateId,
      description: str(value.description),
      readOnly,
      label: doc.label,
      role: doc.role,
      source: kind === "index" ? source(value.source) : null,
      okfVersion: kind === "index" && str(value.okf_version) ? str(value.okf_version) : null,
      stateVersion: doc.version,
      root,
      fileCount,
      changes,
      problem: doc.problem
    });
  }
  return views.sort((a, b) => Number(a.role === "subagent") - Number(b.role === "subagent"));
}

/** The node at `path` in `root`, or `null` once it is gone (a removed row included). */
export function findNode(root: FileNode, path: string): FileNode | null {
  let current: FileNode | undefined = root;
  let prefix = "";
  for (const part of path === "" ? [] : path.split("/")) {
    prefix = prefix ? `${prefix}/${part}` : part;
    current = current?.children.find((child) => child.path === prefix);
  }
  return current && current.change !== "removed" ? current : null;
}

export type FileView =
  | { mode: "markdown" }
  | { mode: "code"; language: string }
  | { mode: "text" };

const CODE_LANGUAGES: Record<string, string> = {
  c: "c",
  cc: "cpp",
  cpp: "cpp",
  cs: "csharp",
  css: "css",
  diff: "diff",
  env: "env",
  go: "go",
  h: "c",
  hpp: "cpp",
  htm: "html",
  html: "html",
  java: "java",
  js: "javascript",
  json: "json",
  jsx: "javascript",
  mjs: "javascript",
  patch: "diff",
  py: "python",
  rb: "ruby",
  rs: "rust",
  sh: "shell",
  sql: "sql",
  svelte: "svelte",
  toml: "toml",
  ts: "typescript",
  tsx: "typescript",
  yaml: "yaml",
  yml: "yaml"
};

/** How to show a file, from its name. */
export function fileView(path: string): FileView {
  const ext = path.includes(".") ? path.slice(path.lastIndexOf(".") + 1).toLowerCase() : "";
  if (ext === "md" || ext === "markdown") return { mode: "markdown" };
  if (/(^|\/)dockerfile$/i.test(path)) return { mode: "code", language: "dockerfile" };
  const language = CODE_LANGUAGES[ext];
  return language ? { mode: "code", language } : { mode: "text" };
}

const FRONT_MATTER = /^---[ \t]*\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/;
const YAML_KEY = /^(?:[A-Za-z_][\w .-]*|"[^"]*"|'[^']*'):(?:[ \t]+(.*))?$/;
const YAML_ITEM = /^-(?:[ \t]+(.*))?$/;

/** A YAML value without its trailing comment. */
const yamlValue = (value: string | undefined) => (value ?? "").replace(/(^|\s)#.*$/, "").trim();

/**
 * Whether a fenced block reads as YAML front matter, in the shapes agents write: top-level
 * `key: value` lines, and under a `key:` with no value, `- item` lists (flush left or
 * indented) and indented `key: value` maps. Quoted keys and values, comments, blank lines and
 * `|`/`>` block scalars are fine; any other line means it isn't front matter.
 */
function isFrontMatter(block: string): boolean {
  let keys = 0;
  /* The last top-level key has no value, so a list or a map may follow under it. */
  let open = false;
  let blockScalar = false;
  for (const raw of block.split(/\r?\n/)) {
    if (raw.trim() === "") continue;
    const indented = /^[ \t]/.test(raw);
    if (blockScalar && indented) continue;
    const line = raw.trim();
    if (line.startsWith("#")) continue;
    const item = YAML_ITEM.exec(line);
    const pair = YAML_KEY.exec(item ? yamlValue(item[1]) : line);
    if (indented || item) {
      if (!open || (!item && !pair)) return false;
    } else {
      if (!pair) return false;
      keys += 1;
      open = yamlValue(pair[1]) === "";
    }
    blockScalar = /^[|>][-+0-9]*$/.test(yamlValue(pair?.[1]));
  }
  return keys > 0;
}

/**
 * A markdown file's YAML front matter split from its body. It counts only when the file
 * opens with a `---` line, closes it with `---` or `...`, and what's between reads as front
 * matter (see `isFrontMatter`); anything else leaves the file whole.
 */
export function splitFrontMatter(text: string): { frontMatter: string | null; body: string } {
  const match = FRONT_MATTER.exec(text);
  return match && isFrontMatter(match[1]!)
    ? { frontMatter: match[1]!, body: text.slice(match[0].length) }
    : { frontMatter: null, body: text };
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

const SIZE_UNITS = ["KB", "MB", "GB", "TB"];

/** A file size as a short label: `512 B`, `2.4 KB`, `4.7 TB`. Empty when it isn't known. */
export function formatSize(size: number | null): string {
  if (size === null) return "";
  if (size < 1024) return `${size} B`;
  let value = size / 1024;
  let unit = 0;
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${SIZE_UNITS[unit]}`;
}
