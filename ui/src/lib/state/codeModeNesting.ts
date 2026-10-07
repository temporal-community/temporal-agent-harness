/**
 * Which Code Mode script a tool call ran under.
 *
 * A `run_temporal_code` host publishes its own tool frames, and the calls its
 * script makes publish theirs in between, with nothing on a child naming its
 * host. The relation is recovered from order alone: a call belongs under the
 * innermost host that has started and not yet ended. The chat indents child
 * rows by this rule.
 */
import type { ToolId } from "$lib/api/types";
import type { ReplayLogRow } from "./replayLog";

/** The script a Code Mode host was called with, or null for any other call. */
export function codeModeScript(input: unknown): string | null {
  if (typeof input !== "object" || input == null || Array.isArray(input)) return null;
  const script = (input as Record<string, unknown>).script;
  return typeof script === "string" && script.trim() ? script : null;
}

/** One scope's worth of the rule — one agent's tool frames, reset at each turn. */
export class CodeModeNesting {
  readonly #hosts = new Set<ToolId>();
  readonly #parents = new Map<ToolId, ToolId>();
  readonly #running: ToolId[] = [];

  /**
   * Read one tool frame and return the host the call belongs under, if any.
   *
   * The host is looked up on every frame rather than only on `tool_requested`,
   * because a child's request is not always published: its first frame can be
   * `tool_start`. Once a call has a host it keeps it.
   */
  place(event: string, toolId: ToolId, script: string | null): ToolId | undefined {
    if (script != null) this.#hosts.add(toolId);
    if (this.#hosts.has(toolId)) {
      if (event === "tool_start" && !this.#running.includes(toolId)) {
        this.#running.push(toolId);
      } else if (event === "tool_end" || event === "tool_error") {
        const index = this.#running.lastIndexOf(toolId);
        if (index !== -1) this.#running.splice(index, 1);
      }
      return undefined;
    }
    const parent = this.#parents.get(toolId) ?? this.#running.at(-1);
    if (parent != null) this.#parents.set(toolId, parent);
    return parent;
  }

  reset(): void {
    this.#hosts.clear();
    this.#parents.clear();
    this.#running.length = 0;
  }
}

/** The frames the graph places a call on — its own lifecycle and its approval gate. */
const PLACED_EVENTS = new Set<string>([
  "tool_requested",
  "tool_start",
  "tool_progress_delta",
  "tool_end",
  "tool_error",
  "tool_approval_requested",
  "tool_approval_resolved"
]);

/**
 * The host each child row ran under, by row id.
 *
 * Rows from every agent in a run arrive interleaved, so each workflow keeps its
 * own nesting. Every row a host's placement depends on comes before it, so a
 * prefix of the log nests exactly as the full log does up to that point.
 */
export function codeModeHostsByRow(rows: readonly ReplayLogRow[]): Map<string, ToolId> {
  const scopes = new Map<string, CodeModeNesting>();
  const hosts = new Map<string, ToolId>();
  for (const row of rows) {
    const scope = row.workflowId ?? "";
    let nesting = scopes.get(scope);
    if (!nesting) {
      nesting = new CodeModeNesting();
      scopes.set(scope, nesting);
    }
    if (row.event === "turn_started") {
      nesting.reset();
      continue;
    }
    if (!row.toolId || !PLACED_EVENTS.has(row.event)) continue;
    const host = nesting.place(row.event, row.toolId, codeModeScript(row.input));
    if (host != null) hosts.set(row.id, host);
  }
  return hosts;
}
