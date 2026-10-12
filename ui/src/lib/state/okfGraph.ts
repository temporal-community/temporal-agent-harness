/**
 * An OKF bundle's graph, as the console draws it: concepts as nodes, links as edges, plus a
 * ghost node for every link to a concept that doesn't exist (yet). Pure functions only; the
 * layout lives in `OKFGraphView.svelte`.
 */
import type { OKFConcept, OKFGraph, OKFLink } from "$lib/api/types";

export interface GraphNode {
  id: string;
  label: string;
  type: string;
  /** A link target with no concept behind it. */
  ghost: boolean;
  concept: OKFConcept | null;
  /** Radius, from the concept's file size. */
  radius: number;
}

/** A walked graph, and the version of its mount's state when the walk started. */
export interface WalkedGraph {
  graph: OKFGraph;
  stateVersion: number;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  dangling: boolean;
}

/** Colors for concept types, in order of first appearance; `Unknown` is always grey. The
 *  tokens are the theme's own, so they follow light and dark mode, ordered so neighbors
 *  differ in hue. Yellow and green come last: the stale and confirmed badges use them.
 *  `--tool` is left out: it is reserved for tool calls. */
const TYPE_TOKENS = [
  "--accent",
  "--reasoning",
  "--syntax-keyword",
  "--queue",
  "--code-violet",
  "--syntax-number",
  "--syntax-type",
  "--model",
  "--live",
  "--success"
];
export const UNKNOWN_TYPE = "Unknown";
const UNKNOWN_TOKEN = "--text-4";

export function typeColors(concepts: OKFConcept[]): Map<string, string> {
  const colors = new Map<string, string>();
  let next = 0;
  for (const concept of concepts) {
    if (colors.has(concept.type)) continue;
    if (concept.type === UNKNOWN_TYPE) {
      colors.set(concept.type, UNKNOWN_TOKEN);
      continue;
    }
    colors.set(concept.type, TYPE_TOKENS[next % TYPE_TOKENS.length]);
    next += 1;
  }
  return colors;
}

export function colorOf(colors: Map<string, string>, type: string): string {
  return `var(${colors.get(type) ?? UNKNOWN_TOKEN})`;
}

function radius(size: number): number {
  return 9 + Math.min(9, Math.sqrt(Math.max(size, 0)) / 9);
}

/** The graph's nodes (concepts, then ghosts) and edges, each once, in a stable order. */
export function graphElements(graph: OKFGraph): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes: GraphNode[] = graph.concepts.map((concept) => ({
    id: concept.id,
    label: concept.title || concept.id,
    type: concept.type,
    ghost: false,
    concept,
    radius: radius(concept.size)
  }));
  const known = new Set(nodes.map((n) => n.id));
  const edges: GraphEdge[] = [];
  for (const link of graph.links) {
    if (!known.has(link.target)) {
      known.add(link.target);
      nodes.push({
        id: link.target,
        label: link.target.split("/").pop() ?? link.target,
        type: UNKNOWN_TYPE,
        ghost: true,
        concept: null,
        radius: 7
      });
    }
    if (!known.has(link.source) || link.source === link.target) continue;
    edges.push({
      id: `${link.source}\u0000${link.target}`,
      source: link.source,
      target: link.target,
      dangling: link.dangling
    });
  }
  return { nodes, edges };
}

/** The links that point at `id`, and the ones it makes. */
export function linksOf(graph: OKFGraph, id: string): { citedBy: OKFLink[]; linksTo: OKFLink[] } {
  return {
    citedBy: graph.links.filter((link) => link.target === id),
    linksTo: graph.links.filter((link) => link.source === id)
  };
}

/** Whether a node answers a search: its title, id or tags contain the query. */
export function matches(node: GraphNode, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const haystack = [node.label, node.id, ...(node.concept?.tags ?? [])].join("\n").toLowerCase();
  return haystack.includes(q);
}

/** A concept file's body: everything after its frontmatter, or the whole text without one. */
export function stripFrontmatter(text: string): string {
  const lines = text.split(/\r?\n/);
  if (lines[0]?.trim() !== "---") return text;
  const close = lines.findIndex((line, i) => i > 0 && line.trim() === "---");
  return close === -1 ? text : lines.slice(close + 1).join("\n").replace(/^\n+/, "");
}

/** The concept id a link in the concept at `fromPath` points to, as the walk resolves it:
 *  relative to the file, or to the bundle root when it starts with `/`. `null` for anything
 *  that isn't a link to another `.md` file in the bundle. */
export function linkTarget(fromPath: string, href: string): string | null {
  const target = decodeURIComponent(href.split("#")[0] ?? "");
  if (!target.endsWith(".md") || /^[A-Za-z][A-Za-z0-9+.-]*:/.test(target)) return null;
  const base = target.startsWith("/") ? [] : fromPath.split("/").slice(0, -1);
  const parts = [...base];
  for (const part of target.replace(/^\/+/, "").split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (parts.length === 0) return null;
      parts.pop();
    } else parts.push(part);
  }
  const name = parts[parts.length - 1];
  if (!name || name === "index.md" || name === "log.md") return null;
  return parts.join("/").slice(0, -3);
}

/** A small deterministic random source, so a bundle lays out the same way every time. */
export function seededRandom(seed = 1): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
}
