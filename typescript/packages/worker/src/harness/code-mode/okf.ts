// Walk an OKF bundle and parse concept frontmatter. YAML parsing stays here, on the worker:
// the workflow only renders concepts.

import { parse } from "yaml";
import type { FileStat } from "@temporalio/agent-harness";
import {
  MAX_OKF_CONCEPTS,
  MAX_OKF_FILE_BYTES,
  MAX_OKF_TOTAL_BYTES,
  OKF_HEAD_BYTES,
  RESERVED_NAMES,
  UNCLOSED_FRONTMATTER,
  linkTargets,
  splitFrontmatter,
  type OKFConcept,
  type OKFGraph,
  type OKFLink,
} from "@temporalio/agent-harness/harness/code-mode/okf";
import { osFailureOf } from "./os-error.ts";

export interface OkfReadable {
  stat(path: string): Promise<FileStat | null>;
  read(path: string, offset?: number, length?: number | null): Promise<Uint8Array>;
  list(path: string): Promise<string[]>;
}

export interface OkfLimits {
  concepts: number;
  fileBytes: number;
  totalBytes: number;
}

const utf8 = new TextDecoder("utf-8", { fatal: false });

/**
 * Every concept under `options.prefix`, read through `fs`, and the links between them when
 * `options.links` is set. As in Python, a directory that can't be listed or an entry that can't be
 * stat'ed (an `FsError` or a Node fs error) is skipped, and a concept that can't be read is listed
 * with the problem.
 */
export async function buildOkfGraph(
  fs: OkfReadable,
  options: { prefix: string; links: boolean; now: Date; limits?: Partial<OkfLimits> },
): Promise<OKFGraph> {
  const limits: OkfLimits = {
    concepts: options.limits?.concepts ?? MAX_OKF_CONCEPTS,
    fileBytes: options.limits?.fileBytes ?? MAX_OKF_FILE_BYTES,
    totalBytes: options.limits?.totalBytes ?? MAX_OKF_TOTAL_BYTES,
  };
  const concepts: OKFConcept[] = [];
  const bodies = new Map<string, string>();
  let total = 0;
  let truncated = false;

  const readConcept = async (path: string, size: number) => {
    const budget = Math.min(limits.fileBytes, limits.totalBytes - total);
    if (budget <= 0) {
      truncated = true;
      return;
    }
    const conceptId = path.endsWith(".md") ? path.slice(0, -3) : path;
    let data: Uint8Array;
    try {
      if (options.links) data = await fs.read(path, 0, budget);
      else {
        data = await fs.read(path, 0, Math.min(OKF_HEAD_BYTES, budget));
        const head = utf8.decode(data);
        if (data.length < size && splitFrontmatter(head)[2] === UNCLOSED_FRONTMATTER)
          data = await fs.read(path, 0, budget);
      }
    } catch (e) {
      const os = osFailureOf(e);
      if (os === null) throw e;
      concepts.push(unknownConcept(conceptId, path, size, `unreadable: ${os.strerror}`));
      return;
    }
    total += data.length;
    const parsed = parseConcept(conceptId, path, utf8.decode(data), size, options.now);
    concepts.push(parsed.concept);
    if (options.links) bodies.set(conceptId, parsed.body);
  };

  const walk = async (directory: string) => {
    let names: string[];
    try {
      names = [...(await fs.list(directory))].sort();
    } catch (e) {
      if (osFailureOf(e) !== null) return;
      throw e;
    }
    for (const name of names) {
      if (truncated) return;
      const path = directory === "." ? name : `${directory}/${name}`;
      let found: FileStat | null;
      try {
        found = await fs.stat(path);
      } catch (e) {
        if (osFailureOf(e) !== null) continue;
        throw e;
      }
      if (found === null) continue;
      if (found.is_dir) await walk(path);
      else if (name.endsWith(".md") && !RESERVED_NAMES.has(name)) {
        if (concepts.length >= limits.concepts) {
          truncated = true;
          return;
        }
        await readConcept(path, found.size ?? 0);
      }
    }
  };

  await walk(options.prefix);
  const links: OKFLink[] = [];
  if (options.links) {
    const known = new Set(concepts.map((concept) => concept.id));
    for (const concept of concepts) {
      for (const target of linkTargets(bodies.get(concept.id) ?? "", concept.path))
        links.push({ source: concept.id, target, dangling: !known.has(target) });
    }
  }
  return {
    concepts,
    links,
    truncated,
    walked_at: options.now.getTime() / 1000,
  };
}

export function parseConcept(
  conceptId: string,
  path: string,
  text: string,
  size: number,
  now: Date,
): { concept: OKFConcept; body: string } {
  let [block, body, problem] = splitFrontmatter(text);
  let fields: Record<string, unknown> = {};
  if (block !== null) {
    try {
      const loaded = parse(block, { schema: "yaml-1.1" }) as unknown;
      if (isPlainObject(loaded))
        fields = Object.fromEntries(
          Object.entries(loaded).map(([key, value]) => [String(key), value]),
        );
      else if (loaded == null) problem = "frontmatter is empty";
      else problem = "frontmatter is not a mapping of keys to values";
    } catch (e) {
      problem = `frontmatter is not valid YAML: ${firstLine(e)}`;
    }
  }
  const kind = textOf(fields.type);
  if (!kind && problem === null) problem = "frontmatter has no `type`";
  const staleAfter = instant(fields.stale_after);
  const stem = path.slice(path.lastIndexOf("/") + 1).replace(/\.md$/, "");
  return {
    body,
    concept: {
      id: conceptId,
      path,
      type: kind || "Unknown",
      title: textOf(fields.title) || stem,
      description: textOf(fields.description),
      tags: tagsOf(fields.tags),
      status: textOf(fields.status) || "stable",
      trust_tier: trustTier(fields.verified),
      stale: staleAfter !== null && now.getTime() >= staleAfter.getTime(),
      size,
      problem,
    },
  };
}

function unknownConcept(id: string, path: string, size: number, problem: string): OKFConcept {
  const stem = path.slice(path.lastIndexOf("/") + 1).replace(/\.md$/, "");
  return {
    id,
    path,
    type: "Unknown",
    title: stem,
    description: "",
    tags: [],
    status: "stable",
    trust_tier: "unverified",
    stale: false,
    size,
    problem,
  };
}

function textOf(value: unknown): string {
  return value == null ? "" : String(value).trim();
}

function tagsOf(value: unknown): string[] {
  const raw = Array.isArray(value)
    ? value.map(textOf)
    : typeof value === "string"
      ? value.split(",").map((tag) => tag.trim())
      : [];
  const tags: string[] = [];
  for (const tag of raw) if (tag && !tags.includes(tag)) tags.push(tag);
  return tags;
}

function trustTier(verified: unknown): OKFConcept["trust_tier"] {
  const events = isPlainObject(verified) ? [verified] : verified;
  if (!Array.isArray(events)) return "unverified";
  const actors = events
    .filter(isPlainObject)
    .map((event) => textOf(event.by))
    .filter((actor) => actor);
  if (actors.some((actor) => actor.startsWith("human:"))) return "human-reviewed";
  return actors.length ? "machine-confirmed" : "unverified";
}

function instant(value: unknown): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value !== "string") return null;
  const text = value.trim().replace(" ", "T").replace(/Z$/, "+00:00");
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[+-]\d{2}:\d{2})?)?$/.test(text))
    return null;
  const iso = /^\d{4}-\d{2}-\d{2}$/.test(text) ? `${text}T00:00:00Z` : text;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstLine(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  const line = text.trim().split("\n")[0];
  return line || (error instanceof Error ? error.name : "Error");
}
