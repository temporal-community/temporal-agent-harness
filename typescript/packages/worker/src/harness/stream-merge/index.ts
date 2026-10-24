// Merge an agent's turn stream with the streams of the subagents it drives into one logical,
// causally ordered stream. A subagent's turn is released only once the parent has announced it
// (subagent_message_sent), and the parent's subagent_reply_received is held until that turn has
// ended on the child's own stream. A child that cannot be read, or stalls past a grace period,
// is given up on with a synthesized subagent_stream_unavailable; the parent never blocks on it.

import { ServiceError, isGrpcServiceError, type Client } from "@temporalio/client";
import {
  WorkflowStreamClient,
  type SubscribeOptions,
  type WorkflowStreamItem,
} from "@temporalio/workflow-streams/client";
import { TURN_EVENTS_TOPIC, type AgentEvent } from "@temporalio/agent-harness/harness/agent-protocol";

export const DEFAULT_STALL_GRACE_SECONDS = 5;

const FAILED_PRECONDITION = 9;

/** `promise`, or a rejection with the signal's reason as soon as `signal` aborts. */
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

/**
 * `WorkflowStreamClient.subscribe` that survives a failed workflow task. A poll delivered on a
 * task the server then fails (a run continuing as new or ending as a signal or cancel arrives
 * fails with UnhandledCommand) is answered with a non-retryable WorkflowNotReady error, which
 * ends the SDK's subscription; this resubscribes after the last item instead.
 *
 * Aborting `signal` cancels the poll in flight (on a connection that supports abort signals) and
 * rejects `next` with the signal's reason without waiting for that poll. `return` closes the SDK's
 * generator; while a poll is in flight the close is queued behind it, so it is not awaited.
 */
export function subscribe<T>(
  client: Client,
  workflowId: string,
  topics: string | string[] | undefined,
  fromOffset: number,
  options: SubscribeOptions & { signal?: AbortSignal },
): AsyncGenerator<WorkflowStreamItem<T>, void, unknown> {
  const { signal, ...subscribeOptions } = options;
  const open = (from: number) =>
    WorkflowStreamClient.create(client, workflowId).subscribe<T>(topics, from, {
      ...subscribeOptions,
      resultType: true,
    });
  let offset = fromOffset;
  let inner = open(offset);
  let inFlight: Promise<unknown> | null = null;
  let closed = false;
  const poll = () => {
    const polled = signal ? client.withAbortSignal(signal, () => inner.next()) : inner.next();
    inFlight = polled;
    const settled = () => {
      if (inFlight === polled) inFlight = null;
    };
    polled.then(settled, settled);
    return signal ? untilAborted(polled, signal) : polled;
  };
  const items = {
    async next(): Promise<IteratorResult<WorkflowStreamItem<T>, void>> {
      for (;;) {
        try {
          signal?.throwIfAborted();
          const next = await poll();
          if (!next.done) offset = next.value.offset + 1;
          return next;
        } catch (e) {
          // A loop over this does not call `return` when `next` rejects, so close here.
          if (signal?.aborted) {
            await items.return(undefined);
            throw e;
          }
          const cause = e instanceof ServiceError ? e.cause : e;
          if (
            !(
              isGrpcServiceError(cause) &&
              cause.code === FAILED_PRECONDITION &&
              /workflow task failure/.test(cause.details)
            )
          )
            throw e;
          inner = open(offset);
        }
      }
    },
    async return(value: undefined): Promise<IteratorReturnResult<undefined>> {
      if (closed) return { done: true, value };
      closed = true;
      const closing = inner.return(value);
      if (inFlight === null) return closing as Promise<IteratorReturnResult<undefined>>;
      closing.catch(() => undefined);
      return { done: true, value };
    },
    throw: (e: unknown) => inner.throw(e),
    [Symbol.asyncIterator]: () => items,
  };
  return items as AsyncGenerator<WorkflowStreamItem<T>, void, unknown>;
}

export class Cursor {
  head: AgentEvent | null = null;
  headOffset = -1;
  headSeq = -1;
  exhausted = false;
  error: unknown = null;
  pull: Promise<void> | null = null;
  pulling = false;
  readonly workflowId: string;
  readonly isChild: boolean;
  readonly mountIndex: number;
  readonly #events: AsyncGenerator<WorkflowStreamItem<AgentEvent>, void, unknown>;
  readonly #skipUntilTurnId: string | null;
  readonly #abort: AbortController;
  #skipping: boolean;

  constructor(
    workflowId: string,
    isChild: boolean,
    mountIndex: number,
    events: AsyncGenerator<WorkflowStreamItem<AgentEvent>, void, unknown>,
    skipUntilTurnId: string | null = null,
    abort: AbortController = new AbortController(),
  ) {
    this.workflowId = workflowId;
    this.isChild = isChild;
    this.mountIndex = mountIndex;
    this.#events = events;
    this.#skipUntilTurnId = skipUntilTurnId;
    this.#abort = abort;
    this.#skipping = skipUntilTurnId !== null;
  }

  static mount(
    client: Client,
    workflowId: string,
    isChild: boolean,
    mountIndex: number,
    fromOffset: number,
    skipUntilTurnId: string | null = null,
  ) {
    const abort = new AbortController();
    const events = subscribe<AgentEvent>(client, workflowId, TURN_EVENTS_TOPIC, fromOffset, {
      pollCooldown: "10 milliseconds",
      signal: abort.signal,
    });
    return new Cursor(workflowId, isChild, mountIndex, events, skipUntilTurnId, abort);
  }

  start(): void {
    this.pulling = true;
    this.pull = this.#pullOne().finally(() => {
      this.pulling = false;
    });
  }

  async #pullOne(): Promise<void> {
    try {
      while (true) {
        const next = await this.#events.next();
        if (next.done) {
          this.exhausted = true;
          return;
        }
        const { data: ev, offset } = next.value;
        if (this.#skipping) {
          if (ev.event.type === "turn_started" && ev.turn_id === this.#skipUntilTurnId)
            this.#skipping = false;
          else continue;
        }
        this.head = ev;
        this.headOffset = offset;
        return;
      }
    } catch (e) {
      this.error = e;
      this.exhausted = true;
    }
  }

  /** Abort the poll in flight, if any, and close the subscription without waiting for it. */
  close(): void {
    this.#abort.abort();
    void this.#events.return(undefined).catch(() => undefined);
  }
}

export class Gates {
  readonly opened = new Set<string>();
  readonly childTurnEnded = new Set<string>();
  readonly gone = new Set<string>();

  static key(workflowId: string, turn: number): string {
    return `${workflowId}\u0000${turn}`;
  }

  ready(isChild: boolean, sourceWorkflowId: string, ev: AgentEvent): boolean {
    if (
      isChild &&
      ev.turn_number !== 0 &&
      !this.opened.has(Gates.key(sourceWorkflowId, ev.turn_number))
    )
      return false;
    const e = ev.event;
    if (e.type === "subagent_reply_received") {
      if (
        !this.childTurnEnded.has(Gates.key(e.workflow_id, e.subagent_turn)) &&
        !this.gone.has(e.workflow_id)
      )
        return false;
    }
    return true;
  }

  onEmit(
    isChild: boolean,
    sourceWorkflowId: string,
    ev: AgentEvent,
  ):
    | { mount: { workflowId: string; fromOffset: number; subagentId: string } }
    | { unmount: string }
    | null {
    const e = ev.event;
    if (e.type === "subagent_message_sent") {
      this.opened.add(Gates.key(e.workflow_id, e.subagent_turn));
      return {
        mount: { workflowId: e.workflow_id, fromOffset: e.from_offset, subagentId: e.subagent_id },
      };
    }
    if (e.type === "subagent_stopped") return { unmount: e.workflow_id };
    if (isChild && e.type === "turn_end")
      this.childTurnEnded.add(Gates.key(sourceWorkflowId, ev.turn_number));
    return null;
  }
}

export type SelectPolicy = (candidates: Cursor[]) => Cursor;
export type ShouldStop = (cursor: Cursor, ev: AgentEvent) => Promise<boolean>;
export type MergedItem = readonly [event: AgentEvent, resumeOffset: number];

/** Attach/replay: earliest-mounted stream first, so history reads parent-before-child. */
export const selectReplay: SelectPolicy = (c) =>
  c.reduce((a, b) => (b.mountIndex < a.mountIndex ? b : a));
/** Live: whichever head arrived first. */
export const selectLive: SelectPolicy = (c) => c.reduce((a, b) => (b.headSeq < a.headSeq ? b : a));

export async function* mergeStream(options: {
  client: Client;
  rootWorkflowId: string;
  rootFromOffset: number;
  skipUntilTurnId: string | null;
  select: SelectPolicy;
  shouldStop: ShouldStop;
  stallGraceSeconds?: number;
  /** Ends the merge, closing its subscriptions, even while it waits on a poll. */
  signal?: AbortSignal;
}): AsyncGenerator<MergedItem, void, unknown> {
  const { client, select, shouldStop, signal } = options;
  const aborted =
    signal &&
    new Promise<void>((resolve) => {
      signal.addEventListener("abort", () => resolve(), { once: true });
    });
  const graceMs = (options.stallGraceSeconds ?? DEFAULT_STALL_GRACE_SECONDS) * 1000;
  const gates = new Gates();
  const cursors = new Map<string, Cursor>();
  const subagentIds = new Map<string, string>();
  const stallDeadlines = new Map<string, number>();
  const pendingMarkers: AgentEvent[] = [];
  let mountSeq = 0;
  let arrivalSeq = 0;
  let rootResumeOffset = options.rootFromOffset;

  const mount = (
    workflowId: string,
    fromOffset: number,
    isChild: boolean,
    skipUntil: string | null = null,
  ) => {
    if (cursors.has(workflowId)) return;
    if (isChild) gates.gone.delete(workflowId);
    cursors.set(
      workflowId,
      Cursor.mount(client, workflowId, isChild, mountSeq++, fromOffset, skipUntil),
    );
  };
  const unmount = (workflowId: string) => {
    const cur = cursors.get(workflowId);
    if (cur === undefined || !cur.isChild) return;
    cursors.delete(workflowId);
    cur.close();
  };
  const abandonedATurn = (workflowId: string) => {
    for (const key of gates.opened) {
      if (key.startsWith(`${workflowId}\u0000`) && !gates.childTurnEnded.has(key)) return true;
    }
    return false;
  };
  const giveUp = (workflowId: string) => {
    stallDeadlines.delete(workflowId);
    if (!gates.gone.has(workflowId) && abandonedATurn(workflowId)) {
      const subagentId = subagentIds.get(workflowId) ?? workflowId;
      pendingMarkers.push({
        agent_id: subagentId,
        turn_id: "",
        turn_number: 0,
        message_id: null,
        timestamp: 0,
        event: {
          type: "subagent_stream_unavailable",
          subagent_id: subagentId,
          workflow_id: workflowId,
          reason:
            "subagent stream unavailable (its workflow has completed and is not yet replayable, or it stalled without " +
            "delivering its turn) — refresh to retry",
        },
      });
    }
    gates.gone.add(workflowId);
    unmount(workflowId);
  };
  const stuckCloseGateChildren = () => {
    const blocked = new Set<string>();
    for (const c of cursors.values()) {
      const head = c.head;
      if (
        head?.event.type === "subagent_reply_received" &&
        !gates.ready(c.isChild, c.workflowId, head)
      )
        blocked.add(head.event.workflow_id);
    }
    return blocked;
  };

  mount(options.rootWorkflowId, options.rootFromOffset, false, options.skipUntilTurnId);
  try {
    while (true) {
      if (signal?.aborted) return;
      for (let m = pendingMarkers.shift(); m; m = pendingMarkers.shift())
        yield [m, rootResumeOffset];
      for (const c of cursors.values())
        if (c.head === null && !c.exhausted && c.pull === null) c.start();

      const candidates = [...cursors.values()].filter(
        (c) => c.head !== null && gates.ready(c.isChild, c.workflowId, c.head),
      );
      if (candidates.length) {
        const cur = select(candidates);
        const ev = cur.head!;
        cur.head = null;
        cur.pull = null;
        if (!cur.isChild) rootResumeOffset = cur.headOffset + 1;
        yield [ev, rootResumeOffset];
        const action = gates.onEmit(cur.isChild, cur.workflowId, ev);
        if (action && "mount" in action) {
          subagentIds.set(action.mount.workflowId, action.mount.subagentId);
          mount(action.mount.workflowId, action.mount.fromOffset, true);
        } else if (action && "unmount" in action) {
          gates.gone.add(action.unmount);
          unmount(action.unmount);
        }
        if (await shouldStop(cur, ev)) return;
        continue;
      }

      const stuck = stuckCloseGateChildren();
      for (const wf of stuck)
        if (!stallDeadlines.has(wf)) stallDeadlines.set(wf, Date.now() + graceMs);
      for (const wf of stallDeadlines.keys()) if (!stuck.has(wf)) stallDeadlines.delete(wf);
      const now = Date.now();
      const giveUpNow = [...stuck].filter(
        (wf) => !cursors.has(wf) || stallDeadlines.get(wf)! <= now,
      );
      if (giveUpNow.length) {
        giveUpNow.forEach(giveUp);
        continue;
      }

      const pending = [...cursors.values()].filter((c) => c.pulling);
      if (pending.length) {
        const deadline = stallDeadlines.size ? Math.min(...stallDeadlines.values()) - now : null;
        let timer: ReturnType<typeof setTimeout> | undefined;
        const waits: Promise<unknown>[] = pending.map((c) => c.pull!);
        if (aborted) waits.push(aborted);
        if (deadline !== null)
          waits.push(
            new Promise((resolve) => (timer = setTimeout(resolve, Math.max(0, deadline)))),
          );
        await Promise.race(waits);
        clearTimeout(timer);
        if (signal?.aborted) return;
        let rootDied = false;
        const departed: string[] = [];
        for (const c of cursors.values()) {
          if (c.pull === null || c.pulling) continue;
          c.pull = null;
          if (c.error !== null) {
            if (c.isChild) departed.push(c.workflowId);
            else rootDied = true;
          } else if (c.exhausted && c.head === null) {
            if (c.isChild) departed.push(c.workflowId);
          } else if (c.head !== null) {
            c.headSeq = arrivalSeq++;
            stallDeadlines.delete(c.workflowId);
          }
        }
        departed.forEach(giveUp);
        if (rootDied) return;
        continue;
      }
      if (stuck.size) {
        stuck.forEach(giveUp);
        continue;
      }
      return;
    }
  } finally {
    for (const c of cursors.values()) c.close();
  }
}
