/**
 * Showing the agent working: typing indicators, and tool calls as they run.
 *
 * Without these a turn is a silence. The agent may be several tool calls deep for tens of
 * seconds with nothing on screen — and since the reply message is only created once there is
 * real text to put in it, there is not even a placeholder to look at.
 */

import type { Adapter, TaskUpdateChunk, Thread } from 'chat';

/**
 * Platforms that render `StreamChunk` objects natively.
 *
 * Everywhere else the fallback renderer does `accumulated += chunk`, so a chunk OBJECT lands
 * in the message as `[object Object]`. That is why tool activity is emitted as chunks for some
 * platforms and as plain markdown for the rest — not a style preference, a hard constraint.
 */
export const rendersTaskUpdates = (adapterName: string | undefined): boolean =>
  adapterName === 'slack' || adapterName === 'linear';

/**
 * Roughly how much rendered content one message may carry before the turn rolls over into a
 * new one.
 *
 * Platforms cap message size — Slack answers `msg_too_long` — and a tool-heavy turn will
 * otherwise exceed it, losing the whole reply rather than degrading. Measured in characters
 * of rendered output, with a card counted at its serialized size, and set well under the real
 * limit because the platform also counts formatting we do not see.
 */
export const MAX_SEGMENT_CHARS = 6_000;

/** A second bound: Slack allows ~50 blocks per message, and each task card costs several. */
export const MAX_SEGMENT_CARDS = 8;

/**
 * Whether a turn should continue in a new message.
 *
 * `openCards` is the number of tool calls whose card is still open in the current message.
 * It is a hard veto, not a tiebreak: a task card exists only within ONE message, so splitting
 * while a call is mid-flight strands its pending or in-progress card and opens a second one
 * next door — two cards for a single tool, which is exactly the bug this guards.
 */
export function shouldRollOver(spent: number, cards: number, openCards: number): boolean {
  if (openCards > 0) return false;
  return spent >= MAX_SEGMENT_CHARS || cards >= MAX_SEGMENT_CARDS;
}

/** Approximate rendered cost of one item, for the budget above. */
export function renderedSize(item: string | { details?: string; output?: string; title?: string }): number {
  if (typeof item === 'string') return item.length;
  return (item.title?.length ?? 0) + (item.details?.length ?? 0) + (item.output?.length ?? 0) + 64;
}

/**
 * Start a stream, or report that it had nothing to say.
 *
 * An EMPTY stream is not free. Slack's adapter finalizes one by calling `chat.startStream`
 * followed by `chat.stopStream` with no content, which posts a real but contentless message —
 * a blank loading bubble — and Chat SDK's post-and-edit fallback posts a single space for the
 * same case. A turn that pauses for an approval produces two such segments (the one that ends
 * the instant the gate opens, and the one after the call finishes with nothing left to say),
 * so the transcript grows a blank bubble above the buttons and another below them.
 *
 * Pulling the first chunk before posting anything is the only way to know: a generator reading
 * a live event stream cannot say whether it will produce output without reading it.
 */
export async function openStream<T>(
  stream: AsyncGenerator<T>,
): Promise<AsyncGenerator<T> | undefined> {
  const first = await stream.next();
  if (first.done) return undefined;
  return (async function* () {
    yield first.value;
    yield* stream;
  })();
}

/** Discord clears its indicator after ~10s, so it has to be re-asserted while work continues. */
const TYPING_REFRESH_MS = 8_000;

/**
 * Keep a typing indicator alive until `stop()`.
 *
 * `startTyping` is one-shot on every adapter, so this re-asserts it on a timer. Failures are
 * swallowed: an indicator is decoration, and losing it must never take down the turn it was
 * decorating.
 */
export function keepTyping(thread: Pick<Thread, 'startTyping'>, onError?: (err: unknown) => void) {
  let stopped = false;
  const tick = () => {
    if (stopped) return;
    void thread.startTyping().catch((err: unknown) => onError?.(err));
  };
  tick();
  const timer = setInterval(tick, TYPING_REFRESH_MS);
  // Do not hold the process open just to say someone is typing.
  timer.unref?.();
  return {
    stop() {
      stopped = true;
      clearInterval(timer);
    },
  };
}

/** One tool call's lifecycle, as the harness reports it. */
export interface ToolActivity {
  /** `pending` is a call that is not running yet — approved, and waiting to reach a worker. */
  phase: 'pending' | 'start' | 'end' | 'error';
  toolId: string;
  toolName: string;
  toolInput?: Record<string, unknown>;
  toolOutput?: string;
  message?: string;
}

/** Trim a value to something a chat surface can show without dominating the thread. */
function clip(value: string, max: number): string {
  return value.length <= max ? value : `${value.slice(0, max - 1)}…`;
}

/**
 * A tool call's arguments as a fenced block, for anywhere a card or a message shows them.
 *
 * Shared so the approval prompt and the verdict that can replace it cannot drift: the question
 * "run this?" and the record "this was refused" have to describe the same call.
 */
export function formatToolInput(
  input: Record<string, unknown> | undefined,
  max = 800,
): string | undefined {
  if (!input || Object.keys(input).length === 0) return undefined;
  return '```json\n' + clip(JSON.stringify(input, null, 2), max) + '\n```';
}

const STATUS: Record<ToolActivity['phase'], TaskUpdateChunk['status']> = {
  pending: 'pending',
  start: 'in_progress',
  end: 'complete',
  error: 'error',
};

/**
 * A `task_update` chunk, for platforms that render them.
 *
 * Slack shows these as task cards keyed by `id`, so the three phases of one call collapse into
 * a single card that updates in place rather than three messages. `details` carries the
 * arguments — visible without cluttering the transcript.
 */
export function toTaskUpdate(activity: ToolActivity): TaskUpdateChunk {
  const details =
    activity.phase === 'error'
      ? activity.message
      : activity.toolInput && Object.keys(activity.toolInput).length > 0
        ? clip(JSON.stringify(activity.toolInput, null, 2), 800)
        : undefined;

  return {
    type: 'task_update',
    id: activity.toolId,
    title: activity.toolName,
    status: STATUS[activity.phase],
    ...(details === undefined ? {} : { details }),
    ...(activity.toolOutput === undefined ? {} : { output: clip(activity.toolOutput, 500) }),
  };
}

/**
 * A markdown line, for platforms that cannot render chunks.
 *
 * Deliberately one short line per transition rather than the arguments: streamed text cannot
 * be revised once written, so a running tool would otherwise be stuck mid-sentence forever,
 * and dumping arguments inline buries the reply. The name answers "what is it doing".
 */
export function toActivityLine(activity: ToolActivity): string {
  switch (activity.phase) {
    case 'pending':
      // Emitted only where a card can go on to become the running and finished states of the
      // SAME call (see `rendersTaskUpdates`). Appended text cannot be revised, so a platform
      // without cards says nothing until the call actually starts rather than stranding a
      // "starting" line above "started".
      return `\n-# ⏳ ${activity.toolName} — starting\n`;
    case 'start':
      return `\n-# 🔧 ${activity.toolName}…\n`;
    case 'end':
      return `\n-# ✅ ${activity.toolName}\n`;
    case 'error':
      return `\n-# ⚠️ ${activity.toolName} failed: ${clip(activity.message ?? 'unknown error', 200)}\n`;
  }
}

/** Render one tool transition for whichever platform is listening. */
export function renderToolActivity(
  activity: ToolActivity,
  adapter: Adapter | undefined,
): TaskUpdateChunk | string {
  return rendersTaskUpdates(adapter?.name) ? toTaskUpdate(activity) : toActivityLine(activity);
}
