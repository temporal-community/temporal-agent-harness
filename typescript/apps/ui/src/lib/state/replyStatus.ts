/**
 * What a turn is doing right now, read from its events alone.
 *
 * Only the rows decide it — never the wall clock or a component's mount — so a reload, a
 * second tab and a replay cursor parked mid-turn all read the same phase and the same start.
 */

import { formatTokens } from "$lib/cost/pricing";
import { codeModeHostsByRow, codeModeScript } from "$lib/state/codeModeNesting";
import type { ReplayLogRow } from "$lib/state/replayLog";

export type ReplyPhase =
  | { kind: "thinking" }
  | { kind: "tool"; tool: string }
  | { kind: "approval" }
  /* Blocked only on callbacks: the agent is asking the reader something, not for a decision. */
  | { kind: "response" }
  | { kind: "replying" }
  | { kind: "done" };

export interface TurnProgress {
  phase: ReplyPhase;
  /** The turn's own `turn_started`, else its earliest timed row; epoch seconds. Null when unknown. */
  startedAt: number | null;
  /** Distinct tool calls the turn has started, its subagents' included. */
  tools: number;
}

const timed = (value: number | undefined): value is number =>
  typeof value === "number" && Number.isFinite(value) && value > 0;

/**
 * One turn's rows, in frame order, as far as they go. A subagent's rows are folded under the
 * parent's turn, so its `turn_end` is not the turn's: only the turn's own rows can end it.
 */
export function turnProgress(rows: readonly ReplayLogRow[]): TurnProgress {
  let ended = false;
  let startedAt: number | null = null;
  let earliest: number | null = null;
  let lastModel = -1;
  let lastReply = -1;
  const openTools = new Map<string, string>();
  const gates = new Set<string>();
  const asks = new Set<string>();
  const tools = new Set<string>();

  rows.forEach((row, index) => {
    const own = row.parentTurnNumber == null;
    const key = `${row.workflowId ?? ""}:${row.toolId ?? ""}`;
    if (timed(row.timestamp)) earliest = Math.min(earliest ?? row.timestamp, row.timestamp);
    switch (row.event) {
      case "turn_started":
        if (own && timed(row.timestamp)) startedAt ??= row.timestamp;
        break;
      case "turn_end":
        if (own) ended = true;
        break;
      case "model_interaction_started":
        lastModel = index;
        break;
      case "reply_delta":
      case "message_handler_end":
        if (own) lastReply = index;
        break;
      case "tool_start":
        tools.add(key);
        openTools.set(key, row.toolName || "a tool");
        break;
      case "tool_end":
      case "tool_error":
        openTools.delete(key);
        break;
      case "tool_approval_requested":
        gates.add(key);
        break;
      case "callback_requested":
        asks.add(key);
        break;
      case "tool_approval_resolved":
        gates.delete(key);
        break;
      case "callback_resolved":
        asks.delete(key);
        break;
    }
  });

  const phase: ReplyPhase = ended
    ? { kind: "done" }
    : gates.size > 0
      ? { kind: "approval" }
      : asks.size > 0
        ? { kind: "response" }
        : openTools.size > 0
          ? { kind: "tool", tool: [...openTools.values()].at(-1)! }
          : lastReply > lastModel
            ? { kind: "replying" }
            : { kind: "thinking" };
  return { phase, startedAt: startedAt ?? earliest, tools: tools.size };
}

/** The status line's words. */
export function replyLabel(phase: ReplyPhase): string {
  switch (phase.kind) {
    case "thinking":
      return "Thinking";
    case "tool":
      return `Running ${phase.tool}`;
    case "approval":
      return "Waiting for approval";
    case "response":
      return "Waiting for your response";
    case "replying":
      return "Replying";
    case "done":
      return "Reply finished";
    default: {
      const unhandled: never = phase;
      return unhandled;
    }
  }
}

/** What a screen reader hears, once per change. A request waiting on the reader is the
    attention queue's to say. */
export function replyAnnouncement(phase: ReplyPhase): string {
  if (phase.kind === "approval" || phase.kind === "response") return "";
  if (phase.kind === "thinking") return "Agent is thinking";
  if (phase.kind === "replying") return "Agent is replying";
  return replyLabel(phase);
}

/** A settled turn in a few words: what it did, not how it ended. */
export function settledSummary(tools: number, thoughtSeconds: number): string {
  if (tools > 0) return `Ran ${tools} tool${tools === 1 ? "" : "s"}`;
  if (thoughtSeconds >= 1) return `Thought for ${Math.round(thoughtSeconds)}s`;
  return "Replied";
}

export type StepState = "requested" | "awaiting" | "running" | "done" | "failed" | "rejected";

/** One line of a turn's trace: a model call, a tool call, or one other event. */
export interface TraceStep {
  /** The step's first row: it keeps its id as later rows of the same call arrive. */
  id: string;
  kind: "thought" | "model" | "tool" | "subagent" | "event";
  name: string;
  state: StepState;
  /** The outcome in words, coloured by the state: "rejected by jev", "awaiting approval". */
  status: string;
  /** Plain context: who approved, the token count, the state it changed, an error's first line. */
  note: string;
  /** A call a Code Mode script made, or a state change it caused while it ran. */
  nested: boolean;
  /** First row's time, epoch seconds. */
  startedAt: number;
  durationMs: number | null;
  rows: ReplayLogRow[];
}

/** "jev_evaluator" → "jev"; a qualified callable name → its last part. */
function evaluatorName(evaluator: string | undefined): string {
  return (evaluator ?? "").split(/[.:]/).at(-1)!.replace(/_evaluator$/, "");
}

/** A summary line never carries a raw payload: JSON reads as noise cut off mid-token. */
function plainLine(value: string | undefined): string {
  const line = (value ?? "").split(/\r?\n/)[0]!.trim();
  return /^[[{"]/.test(line) ? "" : line;
}

interface ToolFold {
  evaluating: string | null;
  decidedBy: string | null;
  callback: boolean;
  approval: string;
}

/**
 * A turn's rows folded into steps: one per model call, one per tool call (its request, approval
 * gate, run and result, by `tool_id`), one per subagent, and one for each other event. A pure
 * function of the rows, so a replay cursor folds the frames up to it exactly as live did.
 */
export function traceSteps(rows: readonly ReplayLogRow[]): TraceStep[] {
  const hosts = codeModeHostsByRow(rows);
  const steps: TraceStep[] = [];
  const byKey = new Map<string, TraceStep>();
  const tools = new Map<TraceStep, ToolFold>();
  const openModels = new Map<string, TraceStep[]>();
  const runningScripts = new Map<string, Set<string>>();

  const open = (key: string | null, row: ReplayLogRow, kind: TraceStep["kind"], name: string): TraceStep => {
    const existing = key == null ? undefined : byKey.get(key);
    if (existing) {
      existing.rows.push(row);
      return existing;
    }
    const step: TraceStep = {
      id: row.id,
      kind,
      name,
      state: "done",
      status: "",
      note: "",
      nested: false,
      startedAt: row.timestamp,
      durationMs: null,
      rows: [row]
    };
    steps.push(step);
    if (key != null) byKey.set(key, step);
    return step;
  };

  for (const row of rows) {
    const scope = row.workflowId ?? "";
    if (row.actor === "reasoning") {
      open("thought", row, "thought", "Thought");
      continue;
    }
    if (row.event === "model_interaction_started") {
      const step = open(null, row, "model", row.model ?? "Model");
      step.state = "running";
      openModels.set(scope, [...(openModels.get(scope) ?? []), step]);
      continue;
    }
    if (row.event === "model_interaction_ended") {
      const running = openModels.get(scope) ?? [];
      const step = running.findLast((item) => item.name === (row.model ?? "Model")) ?? running.at(-1);
      if (step) {
        running.splice(running.indexOf(step), 1);
        step.rows.push(row);
      }
      const ended = step ?? open(null, row, "model", row.model ?? "Model");
      ended.state = "done";
      if (row.usage?.total) ended.note = `${formatTokens(row.usage.total)} tok`;
      continue;
    }
    if (row.toolId) {
      const step = open(`${scope}:tool:${row.toolId}`, row, "tool", row.toolName || "tool");
      const fold = tools.get(step) ?? { evaluating: null, decidedBy: null, callback: false, approval: "" };
      tools.set(step, fold);
      if (hosts.has(row.id)) step.nested = true;
      const script = codeModeScript(row.input) != null;
      switch (row.event) {
        case "tool_requested":
          step.state = "requested";
          break;
        case "tool_approval_requested":
          step.state = "awaiting";
          break;
        case "callback_requested":
          step.state = "awaiting";
          fold.callback = true;
          break;
        case "auto_approval_evaluation_started":
          fold.evaluating = evaluatorName(row.evaluator);
          break;
        case "auto_approval_evaluation_ended":
          fold.evaluating = null;
          if (row.status !== "awaiting") fold.decidedBy = evaluatorName(row.evaluator);
          break;
        case "auto_approval_evaluation_error":
        case "auto_approval_evaluation_superseded":
          fold.evaluating = null;
          break;
        case "tool_approval_resolved": {
          const approved = row.status === "approved";
          const verb = approved ? "approved" : "rejected";
          fold.approval =
            fold.decidedBy == null ? verb : fold.decidedBy ? `${verb} by ${fold.decidedBy}` : `auto-${verb}`;
          step.state = approved ? "running" : "rejected";
          break;
        }
        case "tool_start":
        case "tool_progress_delta":
          step.state = "running";
          if (script) runningScripts.set(scope, (runningScripts.get(scope) ?? new Set()).add(step.id));
          break;
        case "tool_end":
          step.state = "done";
          break;
        case "tool_error":
          step.state = "failed";
          step.note = plainLine(row.body);
          break;
        case "callback_resolved":
          step.state = row.status === "ok" ? "done" : "failed";
          if (step.state === "failed") step.note = plainLine(row.body);
          break;
      }
      if (row.event === "tool_end" || row.event === "tool_error") runningScripts.get(scope)?.delete(step.id);
      step.status =
        step.state === "awaiting"
          ? fold.evaluating
            ? `${fold.evaluating || "auto-approval"} checking`
            : fold.callback
              ? "waiting on client"
              : "awaiting approval"
          : step.state === "rejected"
            ? fold.approval
            : step.state === "failed"
              ? "failed"
              : "";
      if (step.state === "running" || step.state === "done") step.note = fold.approval;
      if (step.state === "requested") step.note = "requested";
      continue;
    }
    if (row.actor === "subagent") {
      /* Started and stopped name the subagent in their body; the message rows in their detail. */
      const who = (row.event === "subagent_started" || row.event === "subagent_stopped" ? row.body : row.detail) ?? "";
      const step = open(`${scope}:subagent:${who}`, row, "subagent", "Subagent");
      step.note = who.split(" · ")[0]!;
      if (row.tone === "error") step.state = "failed";
      else if (step.state !== "failed") step.state = row.event === "subagent_stopped" ? "done" : "running";
      step.status = step.state === "failed" ? "failed" : "";
      continue;
    }
    const step = open(null, row, "event", row.label);
    step.state = row.tone === "error" ? "failed" : "done";
    step.note = row.event.startsWith("state_") ? (row.body ?? "").split(" — ")[0]! : plainLine(row.body);
    step.nested = row.event.startsWith("state_") && (runningScripts.get(scope)?.size ?? 0) > 0;
  }

  for (const step of steps) {
    const times = step.rows.map((row) => row.timestamp).filter(timed);
    const span = times.length >= 2 ? Math.round((Math.max(...times) - Math.min(...times)) * 1000) : 0;
    step.durationMs = span > 0 ? span : null;
  }
  return steps;
}

/**
 * Whether opening a row would show more than its line already says (`line`): the call's
 * arguments, its result, or a payload. A model call's body is only its token count, and a tool's
 * bare body is its name, so neither counts.
 */
export function rowHasDetails(row: ReplayLogRow, line?: string): boolean {
  return (
    row.input !== undefined ||
    Boolean(row.output || row.detail) ||
    Boolean(row.body && row.actor !== "model" && row.body !== row.toolName && row.body !== line)
  );
}

export function stepHasDetails(step: TraceStep): boolean {
  return step.rows.some((row) => rowHasDetails(row, step.note));
}

/**
 * Seconds between two epoch-second readings as a ticking timer reads them: "12.3s",
 * "1m 4.0s", "2h 05m". Empty when either end is missing or not a time, so a bad frame
 * hides the timer rather than printing "NaNs". A clock behind the start reads 0.0s.
 */
export function formatReplyElapsed(startedAt: number | null, now: number | null): string {
  if (!timed(startedAt ?? undefined) || !timed(now ?? undefined)) return "";
  const seconds = Math.max(0, now! - startedAt!);
  if (seconds < 60) return `${(Math.floor(seconds * 10) / 10).toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${(Math.floor((seconds % 60) * 10) / 10).toFixed(1)}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}
