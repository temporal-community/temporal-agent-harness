/**
 * A `StateAdapter` with no external store.
 *
 * Chat SDK requires one, and its own options are Redis, Postgres or in-memory. We need none of
 * them, because the two things a shared store exists to provide here are already provided —
 * better — by the agent workflow:
 *
 *   - Per-thread serialisation. The harness workflow queues a thread's messages itself
 *     (`MessageDisposition` opened/joined/queued), so `concurrency: {strategy:'concurrent'}`
 *     takes a path that acquires no lock at all.
 *   - Idempotency. `request_id` -> `update_id` makes a redelivered webhook re-issue the SAME
 *     update and get the original acceptance back. Durable and exact, where Chat SDK's dedupe
 *     is a best-effort TTL.
 *
 * What remains is caching. Adapters memoise platform metadata here — Slack caches
 * `users.info` results per user, plus a reverse name index — and a miss costs an API call, not
 * correctness. Those get a bounded in-process cache: a cache is per-process by nature, a cold
 * start simply refetches, and nothing depends on an entry surviving a restart or being visible
 * to another instance.
 *
 * The exceptions are the keys where in-memory would be silently WRONG rather than merely cold,
 * and those still throw loudly:
 *
 *   - `chat:callback:` — button callback tokens, read up to seven days after being written,
 *     quite possibly by a different process. Serving these from memory would work in testing
 *     and fail in production after a redeploy, which is the worst possible shape for a bug.
 *     We avoid needing them by setting `Button.value` ourselves.
 *   - locks and queues — cross-process coordination. An in-memory lock held by one instance is
 *     invisible to another, so it would not be a lock.
 */

import type { Lock, QueueEntry, StateAdapter } from 'chat';

/** Keys whose whole purpose is durability or cross-process visibility. */
const DURABLE_PREFIXES = ['chat:callback:'];

/**
 * Chat SDK stashes thread/message/channel here when a modal opens and reads it back on submit.
 * Inert rather than cached: we carry `{threadId, agentKey, handler}` in the modal's
 * `privateMetadata`, which survives the platform round trip, and rebuild the thread with
 * `chat.thread(id)`. `retrieveModalContext` does not catch, so this must not throw.
 */
const MODAL_CONTEXT_PREFIX = 'modal-context:';

/**
 * Chat SDK's own message de-duplication. `deduplicate: false` is honoured by
 * `Chat.processMessage`, but the Discord Gateway path calls `Chat.handleIncomingMessage`
 * directly, which hardcodes it on — so this key is written regardless. Always answering "not
 * seen before" is correct, not a concession: a redelivery is already a durable no-op one layer
 * down, via `request_id` -> `update_id`.
 */
const DEDUPE_PREFIX = 'dedupe:';

/** Bound on cached entries. Slack user records are small; this is a leak guard, not tuning. */
const MAX_ENTRIES = 5_000;

const isDurable = (key: string): boolean => DURABLE_PREFIXES.some((p) => key.startsWith(p));

const unreachable = (method: string, key: string): never => {
  throw new Error(
    `TemporalState.${method} was called for "${key}", which needs a real store — it must ` +
      'survive a restart or be shared across instances. Serving it from memory would pass ' +
      'in testing and fail in production.',
  );
};

interface Entry {
  value: unknown;
  expiresAt: number | undefined;
}

/** Bounded TTL cache. Insertion-ordered, so the oldest entry is the one evicted. */
class MemoryCache {
  private readonly entries = new Map<string, Entry>();

  get(key: string): unknown {
    const entry = this.entries.get(key);
    if (!entry) return undefined;
    if (entry.expiresAt !== undefined && entry.expiresAt <= Date.now()) {
      this.entries.delete(key);
      return undefined;
    }
    return entry.value;
  }

  set(key: string, value: unknown, ttlMs?: number): void {
    // Re-insert so a refreshed key becomes the newest, not the next evicted.
    this.entries.delete(key);
    this.entries.set(key, {
      value,
      expiresAt: ttlMs === undefined ? undefined : Date.now() + ttlMs,
    });
    while (this.entries.size > MAX_ENTRIES) {
      const oldest = this.entries.keys().next();
      if (oldest.done) break;
      this.entries.delete(oldest.value);
    }
  }

  delete(key: string): void {
    this.entries.delete(key);
  }
}

export class TemporalState implements StateAdapter {
  private readonly cache = new MemoryCache();

  async connect(): Promise<void> {}
  async disconnect(): Promise<void> {}

  /**
   * Always false. This is a ROUTING SWITCH, not a liveness query.
   *
   * Chat SDK dispatch reads it as "deliver this thread's messages to `onSubscribedMessage` and
   * stop":
   *
   *     if (isSubscribed) { runHandlers(subscribedMessageHandlers); return; }
   *     if (message.isMention) { runHandlers(mentionHandlers); return; }
   *
   * Answering `true` without registering `onSubscribedMessage` therefore swallows every
   * message before the mention handler sees it. It is tempting to wire this to "does a live
   * agent session exist" — they sound like the same question and are not. It would also be
   * wrong on its merits: a Discord thread id for an ordinary channel message IS the channel,
   * so subscribing would route all channel chatter to the agent.
   */
  async isSubscribed(_threadId: string): Promise<boolean> {
    return false;
  }

  async subscribe(_threadId: string): Promise<void> {}
  async unsubscribe(_threadId: string): Promise<void> {}

  async get<T = unknown>(key: string): Promise<T | null> {
    if (key.startsWith(MODAL_CONTEXT_PREFIX)) return null;
    if (isDurable(key)) return unreachable('get', key);
    return (this.cache.get(key) as T | undefined) ?? null;
  }

  async set<T = unknown>(key: string, value: T, ttlMs?: number): Promise<void> {
    if (key.startsWith(MODAL_CONTEXT_PREFIX)) return;
    if (isDurable(key)) unreachable('set', key);
    this.cache.set(key, value, ttlMs);
  }

  async delete(key: string): Promise<void> {
    if (key.startsWith(MODAL_CONTEXT_PREFIX)) return;
    if (isDurable(key)) unreachable('delete', key);
    this.cache.delete(key);
  }

  async setIfNotExists(key: string, value: unknown, ttlMs?: number): Promise<boolean> {
    // `true` means "not seen before, carry on" — see DEDUPE_PREFIX.
    if (key.startsWith(DEDUPE_PREFIX)) return true;
    if (isDurable(key)) return unreachable('setIfNotExists', key);
    if (this.cache.get(key) !== undefined) return false;
    this.cache.set(key, value, ttlMs);
    return true;
  }

  async getList<T = unknown>(key: string): Promise<T[]> {
    if (isDurable(key)) return unreachable('getList', key);
    const existing = this.cache.get(key);
    return Array.isArray(existing) ? (existing as T[]) : [];
  }

  async appendToList(
    key: string,
    value: unknown,
    options?: { maxLength?: number; ttlMs?: number },
  ): Promise<void> {
    if (isDurable(key)) unreachable('appendToList', key);
    const existing = this.cache.get(key);
    const list = Array.isArray(existing) ? [...existing, value] : [value];
    // Trim from the front: the contract keeps the NEWEST entries.
    const trimmed =
      options?.maxLength !== undefined && list.length > options.maxLength
        ? list.slice(list.length - options.maxLength)
        : list;
    this.cache.set(key, trimmed, options?.ttlMs);
  }

  // --- Cross-process coordination: cannot be faked in memory -------------------------------

  async acquireLock(threadId: string, _ttlMs: number): Promise<Lock | null> {
    return unreachable('acquireLock', threadId);
  }
  async extendLock(lock: Lock, _ttlMs: number): Promise<boolean> {
    return unreachable('extendLock', lock.threadId);
  }
  async releaseLock(lock: Lock): Promise<void> {
    unreachable('releaseLock', lock.threadId);
  }
  async forceReleaseLock(threadId: string): Promise<void> {
    unreachable('forceReleaseLock', threadId);
  }
  async enqueue(threadId: string, _entry: QueueEntry, _maxSize: number): Promise<number> {
    return unreachable('enqueue', threadId);
  }
  async dequeue(threadId: string): Promise<QueueEntry | null> {
    return unreachable('dequeue', threadId);
  }
  async queueDepth(threadId: string): Promise<number> {
    return unreachable('queueDepth', threadId);
  }
}
