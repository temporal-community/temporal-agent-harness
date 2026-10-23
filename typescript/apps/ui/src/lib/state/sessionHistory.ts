import type { AgentDescriptor, Session } from "$lib/api/types";
import type { StatusKind } from "$lib/components/primitives/StatusChip.svelte";
import { markdownToText } from "$lib/components/chat/MarkdownMessage.svelte";
import { SvelteMap } from "svelte/reactivity";

/**
 * What a session was waiting on when the reader last left it, e.g. "1 approval needed", and
 * when, epoch ms. Only the open session's stream says what it waits on, and the session list
 * carries no pending state, so this is the last thing seen, not a live reading; it may have
 * been answered elsewhere since. Kept in memory only, so a reload forgets it.
 */
export const lastSeenPending = new SvelteMap<string, { label: string; at: number }>();

/**
 * Run as an effect of the open session's id and pending label: the returned teardown runs as the
 * reader leaves, and keeps what it was waiting on. A stream waiting on a person can still be
 * connecting, so that is kept too; only a settled view with nothing pending forgets it, not one
 * still empty while it connects.
 */
export function rememberPending(sessionId: string, label: string | null, connecting: boolean): (() => void) | undefined {
  if (!sessionId) return undefined;
  if (label) return () => lastSeenPending.set(sessionId, { label, at: Date.now() });
  if (!connecting) lastSeenPending.delete(sessionId);
  return undefined;
}

/** A session's title when it has neither an opening message nor a label. */
export const UNTITLED_SESSION = "Untitled session";

export function sessionTitle(session: Session): string {
  return markdownToText(session.initial_user_message).trim().replace(/\s+/g, " ") || session.label?.trim() || UNTITLED_SESSION;
}

/** "PydanticAIHelloAgent" → "Pydantic AI Hello Agent": words a kicker's capitals can still be read in. */
export function humanizeWorkflowType(type: string | null | undefined): string {
  return (type ?? "")
    .replace(/[_-]+/g, " ")
    .replace(/([a-z\d])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .replace(/([A-Za-z])(\d)/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim();
}

/** The registry's label for an agent, else its workflow type in words; never the raw type. */
export function agentName(agents: readonly AgentDescriptor[], workflowType: string | null | undefined): string {
  return (
    agents.find((agent) => agent.workflow_type === workflowType)?.label?.trim() ||
    humanizeWorkflowType(workflowType) ||
    "Agent"
  );
}

/** How long a connection runs before the headline asks whether the agent's worker is up. */
export const CONNECTING_HINT_AFTER_S = 10;

/**
 * The open session's status word and the kind its hue comes from, picked by one ordered rule so
 * the two can't disagree. A person waiting on an approval outranks "Connecting": the stream stays
 * open while they answer, and that wait is not a connection.
 *
 * `connectingFor` (seconds) puts the wait in the word; past CONNECTING_HINT_AFTER_S it becomes
 * "stuck" with a `hint`, worded as a question: until `/api/status` answers a missing worker with
 * its own error, nothing tells one from a slow connection.
 */
/** Why a session whose workflow Temporal no longer has can be read but not acted on. */
export const SESSION_GONE_TIP = "Workflow no longer exists in Temporal";

export function sessionHeadline(state: {
  closed: boolean;
  /** Temporal no longer has the workflow; implies closed. */
  gone?: boolean;
  /** The workflow stopped on its own: FAILED, TERMINATED or TIMED_OUT. Implies closed. */
  failed?: boolean;
  /** The registry reports no worker polling the agent's task queue. */
  noWorker?: boolean;
  error: boolean;
  pendingLabel: string | null;
  creating: boolean;
  connecting: boolean;
  sending: boolean;
  connectingFor?: number | null;
  taskQueue?: string | null;
}): { kind: StatusKind; label: string; hint?: string } {
  if (state.gone) {
    return { kind: "closed", label: "Unavailable", hint: `${SESSION_GONE_TIP}. What this browser cached is shown read-only.` };
  }
  if (state.failed) return { kind: "error", label: "Needs attention" };
  if (state.closed) return { kind: "closed", label: "Closed" };
  if (state.error) return { kind: "error", label: "Needs attention" };
  if (state.noWorker) {
    const queue = state.taskQueue?.trim();
    return { kind: "stuck", label: "No worker", hint: `No worker is polling this agent's task queue${queue ? ` ${queue}` : ""}. Start the worker to continue.` };
  }
  if (state.pendingLabel) return { kind: "approval", label: state.pendingLabel };
  if (state.creating) return { kind: "starting", label: "Starting" };
  if (state.connecting) {
    const seconds = Math.floor(state.connectingFor ?? 0);
    if (!(seconds >= 1)) return { kind: "connecting", label: "Connecting" };
    const label = `Connecting · ${seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`}`;
    if (seconds < CONNECTING_HINT_AFTER_S) return { kind: "connecting", label };
    const queue = state.taskQueue?.trim();
    const hint = `Still connecting — is this agent's worker running?${queue ? ` It polls task queue ${queue}.` : ""}`;
    return { kind: "stuck", label, hint };
  }
  if (state.sending) return { kind: "thinking", label: "Thinking" };
  return { kind: "available", label: "Available" };
}

/** Where `just temporal` (`temporal server start-dev`) serves the `default` namespace. */
export const DEFAULT_TEMPORAL_UI = "http://localhost:8233/namespaces/default/workflows";

/**
 * The Temporal Web UI page for a session's workflow, under a namespace's workflows page: the
 * `?temporal-ui=` query parameter, as the agent_dag studio takes it, else the one the API
 * reports for the session (`temporal_workflows_url`), else the local dev server's. Only http(s)
 * is honored: the value lands in an `href`.
 */
export function temporalWorkflowUrl(workflowId: string, search = "", reported?: string | null): string {
  const base = [new URLSearchParams(search).get("temporal-ui"), reported].map(httpUrl).find(Boolean) ?? DEFAULT_TEMPORAL_UI;
  return `${base.replace(/\/+$/, "")}/${encodeURIComponent(workflowId)}`;
}

function httpUrl(value: string | null | undefined): string | null {
  try {
    const url = new URL(value ?? "");
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/**
 * A start time, epoch seconds, worth showing. Before September 2001 (1e9 s) it is a zero or a
 * near-zero placeholder, not a real start: "56y" and a "Jan 1, 1970" group said nothing true.
 */
export function knownStart(timestamp: number): boolean {
  return timestamp >= 1e9 && Number.isFinite(timestamp);
}

const dayFormat = new Intl.DateTimeFormat([], { month: "short", day: "numeric", year: "numeric" });

export function sessionDay(timestamp: number, now = new Date()): string {
  if (!knownStart(timestamp)) return "Unknown date";
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return "Unknown date";
  if (date.toDateString() === now.toDateString()) return "Today";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return dayFormat.format(date);
}

const relativeFormat = new Intl.RelativeTimeFormat([], { numeric: "auto" });
const AGE_UNITS = [
  { seconds: 365 * 86_400, short: "y", unit: "year" },
  { seconds: 7 * 86_400, short: "w", unit: "week" },
  { seconds: 86_400, short: "d", unit: "day" },
  { seconds: 3_600, short: "h", unit: "hour" },
  { seconds: 60, short: "m", unit: "minute" }
] as const;

/**
 * How long ago a session started: "now", "2m", "3h", "4d", "5w", "1y", and the words a screen
 * reader says for it. Blank for a missing, unreadable or placeholder time (`knownStart`), or one
 * more than a minute ahead of the clock: the row's tip still gives the absolute start.
 */
export function sessionAge(createdAt: number, nowMs: number): { short: string; long: string } {
  const seconds = nowMs / 1000 - createdAt;
  if (!knownStart(createdAt) || !Number.isFinite(seconds) || seconds < -60) return { short: "", long: "" };
  const step = AGE_UNITS.find((entry) => seconds >= entry.seconds);
  if (!step) return { short: "now", long: relativeFormat.format(0, "second") };
  const count = Math.floor(seconds / step.seconds);
  return { short: `${count}${step.short}`, long: relativeFormat.format(-count, step.unit) };
}

/** Calls `tick` with the time at each minute boundary until the returned stop is called. */
export function everyMinute(tick: (nowMs: number) => void): () => void {
  let interval: ReturnType<typeof setInterval> | undefined;
  const timeout = setTimeout(() => {
    tick(Date.now());
    interval = setInterval(() => tick(Date.now()), 60_000);
  }, 60_000 - (Date.now() % 60_000));
  return () => {
    clearTimeout(timeout);
    clearInterval(interval);
  };
}

/** The Session Manager's fixed row heights, px: the windowed list's estimates, and its CSS. */
export const SESSION_ROW_HEIGHT = { fine: 28, coarse: 36 } as const;
export const SESSION_HEADING_HEIGHT = 22;
