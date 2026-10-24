// Open Knowledge Format (OKF v0.2) as workflow-safe models and text.
// Parsing YAML and walking a bundle need a YAML library and a filesystem, so both stay in
// worker activities. Rendering a concept runs in the workflow, with a small block-style emitter
// instead of a Node YAML package.

import { z } from "zod";

export const OKF_VERSION = "0.2";

export const RESERVED_NAMES = new Set(["index.md", "log.md"]);

/** The most a walk reads, so a huge bundle can't stall the activity or the console. */
export const MAX_OKF_CONCEPTS = 1_000;
export const MAX_OKF_FILE_BYTES = 256_000;
export const MAX_OKF_TOTAL_BYTES = 8_000_000;
/** Without links only frontmatter is needed, so a walk reads each file's head first. */
export const OKF_HEAD_BYTES = 8_192;

export const UNCLOSED_FRONTMATTER = "frontmatter is never closed with a `---` line";

const TrustTier = z.enum(["unverified", "machine-confirmed", "human-reviewed"]);

export const OKFConcept = z
  .object({
    id: z.string(),
    path: z.string(),
    type: z.string(),
    title: z.string(),
    description: z.string().default(""),
    tags: z.array(z.string()).default([]),
    status: z.string().default("stable"),
    trust_tier: TrustTier.default("unverified"),
    stale: z.boolean().default(false),
    size: z.number(),
    problem: z.string().nullable().default(null),
  })
  .meta({ title: "OKFConcept" });
export type OKFConcept = z.output<typeof OKFConcept>;

export const OKFLink = z
  .object({
    source: z.string(),
    target: z.string(),
    dangling: z.boolean().default(false),
  })
  .meta({ title: "OKFLink" });
export type OKFLink = z.output<typeof OKFLink>;

export const OKFLinks = z
  .object({
    id: z.string(),
    links_to: z.array(OKFLink),
    cited_by: z.array(OKFLink),
  })
  .meta({ title: "OKFLinks" });
export type OKFLinks = z.output<typeof OKFLinks>;

export const OKFGraph = z
  .object({
    concepts: z.array(OKFConcept),
    links: z.array(OKFLink).default([]),
    truncated: z.boolean().default(false),
    walked_at: z.number(),
  })
  .meta({ title: "OKFGraph" });
export type OKFGraph = z.output<typeof OKFGraph>;

/** `(frontmatter, body, problem)`. `frontmatter` is null when there is none. */
export function splitFrontmatter(text: string): [string | null, string, string | null] {
  const lines = splitKeep(text);
  if (!lines.length || stripLine(lines[0]!) !== "---")
    return [null, text, "no frontmatter: the file doesn't start with a `---` line"];
  for (let i = 1; i < lines.length; i++) {
    if (stripLine(lines[i]!) === "---")
      return [lines.slice(1, i).join(""), lines.slice(i + 1).join(""), null];
  }
  return [null, text, UNCLOSED_FRONTMATTER];
}

const FENCE = /^\s{0,3}(```|~~~)/;
const SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const SPACE = /\s/;
const TARGET_END = /[)\s>]/;

/**
 * The targets of the `](target "title")` links in `line`, exactly as
 * `/\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/g` finds them, in one pass: that regex retries at
 * every `](`, which is quadratic in a long line of them.
 */
function linkTargetsIn(line: string): string[] {
  const found: string[] = [];
  const skipSpace = (at: number) => {
    while (at < line.length && SPACE.test(line[at]!)) at++;
    return at;
  };
  for (let open = line.indexOf("]("); open >= 0; ) {
    let at = skipSpace(open + 2);
    const angle = line[at] === "<";
    if (angle) at++;
    let start = at;
    while (at < line.length && !TARGET_END.test(line[at]!)) at++;
    // A `<` with nothing after it is the target itself.
    if (at === start && angle) start--;
    let end = -1;
    if (at > start) {
      const afterTarget = line[at] === ">" ? at + 1 : at;
      const titleAt = skipSpace(afterTarget);
      if (titleAt > afterTarget && line[titleAt] === '"') {
        const close = line.indexOf('"', titleAt + 1);
        if (close >= 0) {
          const paren = skipSpace(close + 1);
          if (line[paren] === ")") end = paren + 1;
        }
      }
      if (end < 0) {
        const paren = skipSpace(afterTarget);
        if (line[paren] === ")") end = paren + 1;
      }
    }
    if (end >= 0) found.push(line.slice(start, at));
    // After a failed `](`, every later one whose target starts inside this target ends where it
    // did and fails too; the next to try is the one that ends just before that end.
    open = line.indexOf("](", end >= 0 ? end : Math.max(open + 1, at - 2));
  }
  return found;
}

/**
 * Concept ids `body` links to, in order, without repeats. Relative targets resolve against
 * `conceptPath`; a target starting with `/` is relative to the bundle root. Links inside
 * fenced code blocks, and links to the concept itself, are left out.
 */
export function linkTargets(body: string, conceptPath: string): string[] {
  const found: string[] = [];
  const here = parentPosix(conceptPath);
  let fence: string | null = null;
  for (const line of body.split(/\r\n|\n|\r/)) {
    const opened = FENCE.exec(line);
    if (opened) {
      const marker = opened[1]!;
      fence = fence === marker ? null : (fence ?? marker);
      continue;
    }
    if (fence) continue;
    for (const raw of linkTargetsIn(line)) {
      const target = resolveLink(decodePercent(raw), here);
      if (target !== null && !found.includes(target)) found.push(target);
    }
  }
  const selfId = conceptPath.endsWith(".md") ? conceptPath.slice(0, -3) : conceptPath;
  return found.filter((id) => id !== selfId);
}

/**
 * A concept's full text: `frontmatter` as YAML between `---` lines, then `body`.
 * Records `generated` unless it is already present, and stamps a `verified` entry that has no `at`.
 */
export function renderConcept(
  frontmatter: Record<string, unknown>,
  body: string,
  generated: { by: string; at: Date },
): string {
  const stamp = generated.at.toISOString().replace(/\.\d{3}Z$/, "Z");
  const fields: Record<string, unknown> = { ...frontmatter };
  if (!Object.hasOwn(fields, "generated")) fields.generated = { by: generated.by, at: stamp };
  const verified = fields.verified;
  if (isPlainObject(verified)) fields.verified = stampVerified(verified, stamp);
  else if (Array.isArray(verified))
    fields.verified = verified.map((entry) =>
      isPlainObject(entry) ? stampVerified(entry, stamp) : entry,
    );
  const text = body.replace(/^\n+/, "");
  const block = dumpMap(fields, 0);
  return `---\n${block}\n---\n\n${text}${text.endsWith("\n") ? "" : "\n"}`;
}

function stampVerified(entry: Record<string, unknown>, stamp: string): Record<string, unknown> {
  return { ...entry, at: Object.hasOwn(entry, "at") ? entry.at : stamp };
}

function splitKeep(text: string): string[] {
  const lines: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch !== "\n" && ch !== "\r") continue;
    const end = ch === "\r" && text[i + 1] === "\n" ? i + 2 : i + 1;
    lines.push(text.slice(start, end));
    start = end;
    i = end - 1;
  }
  if (start < text.length || text.length === 0) lines.push(text.slice(start));
  if (text.length === 0) return [];
  return lines;
}

const stripLine = (line: string) => line.replace(/[\r\n]+$/, "").trim();

function parentPosix(path: string): string {
  const i = path.lastIndexOf("/");
  return i === -1 ? "." : path.slice(0, i);
}

function decodePercent(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function resolveLink(target: string, here: string): string | null {
  const hash = target.indexOf("#");
  const bare = hash === -1 ? target : target.slice(0, hash);
  if (!bare || SCHEME.test(bare) || !bare.endsWith(".md")) return null;
  const joined = bare.startsWith("/")
    ? bare.replace(/^\/+/, "")
    : here === "."
      ? bare
      : `${here}/${bare}`;
  const normal = posixNorm(joined);
  if (normal === "." || normal === "" || normal === ".." || normal.startsWith("../")) return null;
  const name = normal.slice(normal.lastIndexOf("/") + 1);
  if (RESERVED_NAMES.has(name)) return null;
  return normal.slice(0, -3);
}

function posixNorm(path: string): string {
  const parts: string[] = [];
  let up = 0;
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (parts.length) parts.pop();
      else up++;
    } else parts.push(part);
  }
  if (!parts.length) return up ? `..${"/..".repeat(up - 1)}` : ".";
  return `${up ? "../".repeat(up) : ""}${parts.join("/")}`;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Quoted when YAML 1.1 would otherwise retype the string (a date, a bool, a number).
const BOOL_OR_NULL =
  /^(?:~|null|Null|NULL|y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$/;
const NUMBER = /^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}(?:[Tt ]|$)/;

function needsQuote(value: string): boolean {
  if (value === "" || value !== value.trim()) return true;
  if (/[\n\r\t#{}[\],&*!|>'"%@`]/.test(value)) return true;
  if (/: /.test(value) || /^[-?:]/.test(value)) return true;
  return BOOL_OR_NULL.test(value) || NUMBER.test(value) || TIMESTAMP.test(value);
}

function emitString(value: string): string {
  if (!needsQuote(value)) return value;
  return `'${value.replaceAll("'", "''")}'`;
}

function dumpScalar(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "null";
  if (typeof value === "string") return emitString(value);
  return emitString(String(value));
}

function dumpMap(obj: Record<string, unknown>, indent: number): string {
  const pad = " ".repeat(indent);
  const lines: string[] = [];
  for (const [key, value] of Object.entries(obj)) {
    if (value === undefined) continue;
    const name = emitString(key);
    if (Array.isArray(value)) {
      lines.push(value.length ? `${pad}${name}:\n${dumpList(value, indent)}` : `${pad}${name}: []`);
    } else if (isPlainObject(value)) {
      const inner = dumpMap(value, indent + 2);
      lines.push(inner ? `${pad}${name}:\n${inner}` : `${pad}${name}: {}`);
    } else lines.push(`${pad}${name}: ${dumpScalar(value)}`);
  }
  return lines.join("\n");
}

function dumpList(items: unknown[], indent: number): string {
  const pad = " ".repeat(indent);
  const lines: string[] = [];
  for (const item of items) {
    if (isPlainObject(item)) {
      const entries = Object.entries(item).filter(([, value]) => value !== undefined);
      if (!entries.length) {
        lines.push(`${pad}- {}`);
        continue;
      }
      const [firstKey, firstValue] = entries[0]!;
      lines.push(`${pad}- ${emitString(firstKey)}:${inlineOrBreak(firstValue, indent + 2)}`);
      for (const [key, value] of entries.slice(1))
        lines.push(
          `${" ".repeat(indent + 2)}${emitString(key)}:${inlineOrBreak(value, indent + 4)}`,
        );
    } else if (Array.isArray(item)) {
      lines.push(item.length ? `${pad}-\n${dumpList(item, indent + 2)}` : `${pad}- []`);
    } else lines.push(`${pad}- ${dumpScalar(item)}`);
  }
  return lines.join("\n");
}

function inlineOrBreak(value: unknown, indent: number): string {
  if (Array.isArray(value)) return value.length ? `\n${dumpList(value, indent - 2)}` : " []";
  if (isPlainObject(value)) {
    const inner = dumpMap(value, indent);
    return inner ? `\n${inner}` : " {}";
  }
  return ` ${dumpScalar(value)}`;
}
