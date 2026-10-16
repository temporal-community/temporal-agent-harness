import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseRoutes, resolveAgentKey } from '../src/routing.ts';
import { TemporalState } from '../src/state.ts';

const state = () => new TemporalState();

test('isSubscribed is always false, or Chat SDK swallows every message', async () => {
  // Regression. Chat SDK dispatch reads this as a routing switch:
  //   if (isSubscribed) { runHandlers(subscribedMessageHandlers); return; }
  //   if (message.isMention) { runHandlers(mentionHandlers); return; }
  // With no onSubscribedMessage registered, answering true drops the message before the
  // mention handler ever sees it. It is NOT "is the session live".
  assert.equal(await state().isSubscribed('any-live-session'), false);
});

test('adapter metadata caches work, so a platform lookup is not repeated per message', async () => {
  // Slack memoises users.info here; a miss costs an API call, not correctness.
  const s = state();
  assert.equal(await s.get('slack:user:U123'), null);
  await s.set('slack:user:U123', { displayName: 'alice' }, 60_000);
  assert.deepEqual(await s.get('slack:user:U123'), { displayName: 'alice' });
  await s.delete('slack:user:U123');
  assert.equal(await s.get('slack:user:U123'), null);
});

test('a cached entry expires rather than going stale forever', async () => {
  const s = state();
  await s.set('slack:user:U1', { displayName: 'old' }, -1); // already past its TTL
  assert.equal(await s.get('slack:user:U1'), null);
});

test('list keys append and trim to the newest entries', async () => {
  const s = state();
  assert.deepEqual(await s.getList('slack:user-by-name:alice'), []);
  await s.appendToList('slack:user-by-name:alice', 'U1', { maxLength: 2 });
  await s.appendToList('slack:user-by-name:alice', 'U2', { maxLength: 2 });
  await s.appendToList('slack:user-by-name:alice', 'U3', { maxLength: 2 });
  assert.deepEqual(await s.getList('slack:user-by-name:alice'), ['U2', 'U3']);
});

test('dedupe never drops a message, because update_id already makes redelivery a no-op', async () => {
  // The Discord Gateway path calls Chat.handleIncomingMessage directly, which hardcodes
  // deduplicate=true, so this IS reached in normal operation and must not throw.
  const s = state();
  assert.equal(await s.setIfNotExists('dedupe:discord:123', true), true);
  assert.equal(await s.setIfNotExists('dedupe:discord:123', true), true, 'still delivered');
});

test('modal context is inert, because privateMetadata carries what submit needs', async () => {
  // retrieveModalContext does NOT catch, so a throw here breaks every modal submission.
  const s = state();
  await s.set('modal-context:slack:abc', { thread: null });
  assert.equal(await s.get('modal-context:slack:abc'), null);
  await s.delete('modal-context:slack:abc');
});

test('keys that genuinely need durability still fail loudly', async () => {
  // A button token is read up to seven days later, possibly by another process. Serving it
  // from memory would pass in testing and break after a redeploy — the worst shape of bug.
  const s = state();
  await assert.rejects(() => s.get('chat:callback:deadbeef'), /needs a real store/);
  await assert.rejects(() => s.set('chat:callback:deadbeef', {}), /needs a real store/);
});

test('cross-process coordination is not faked in memory', async () => {
  // An in-memory lock held by one instance is invisible to another, so it is not a lock.
  const s = state();
  await assert.rejects(() => s.acquireLock('t', 1000), /needs a real store/);
  await assert.rejects(() => s.enqueue('t', {} as never, 10), /needs a real store/);
});

test('the cache is bounded, so a long-running process cannot leak', async () => {
  const s = state();
  for (let i = 0; i < 5_200; i += 1) await s.set(`slack:user:U${i}`, i);
  assert.equal(await s.get('slack:user:U0'), null, 'oldest evicted');
  assert.equal(await s.get('slack:user:U5199'), 5_199, 'newest retained');
});

test('one bot is one agent, and a bare entry is the single-bot default', () => {
  const table = parseRoutes('T1/B1=support, T1/B2=research');
  assert.equal(resolveAgentKey(table, 'T1/B1'), 'support');
  assert.equal(resolveAgentKey(table, 'T1/B3'), undefined, 'no route and no default');
  assert.equal(resolveAgentKey(parseRoutes('support'), 'any-bot'), 'support');
});
