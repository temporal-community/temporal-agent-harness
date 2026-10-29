import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  MAX_SEGMENT_CARDS,
  MAX_SEGMENT_CHARS,
  formatToolInput,
  keepTyping,
  openStream,
  renderedSize,
  renderToolActivity,
  rendersTaskUpdates,
  shouldRollOver,
  toActivityLine,
  toTaskUpdate,
} from '../src/activity.ts';

const start = {
  phase: 'start' as const,
  toolId: 'toolu_01A',
  toolName: 'get_weather',
  toolInput: { city: 'Lisbon' },
};

test('Slack gets a task_update chunk, keyed so the phases collapse into one card', () => {
  const a = toTaskUpdate(start);
  const b = toTaskUpdate({ ...start, phase: 'end', toolOutput: '21C' });
  assert.equal(a.type, 'task_update');
  assert.equal(a.id, b.id, 'same tool id, so Slack updates the card instead of adding one');
  assert.equal(a.status, 'in_progress');
  assert.equal(b.status, 'complete');
  assert.equal(b.output, '21C');
});

test('arguments ride along as details, so they can be inspected without clutter', () => {
  assert.match(toTaskUpdate(start).details ?? '', /Lisbon/);
  // A no-argument tool should not show an empty block.
  assert.equal(toTaskUpdate({ ...start, toolInput: {} }).details, undefined);
});

test('an error carries its reason rather than the arguments', () => {
  const chunk = toTaskUpdate({ ...start, phase: 'error', message: 'timed out' });
  assert.equal(chunk.status, 'error');
  assert.equal(chunk.details, 'timed out');
});

test('long values are clipped so one tool call cannot swamp the thread', () => {
  const huge = { blob: 'x'.repeat(5_000) };
  assert.ok((toTaskUpdate({ ...start, toolInput: huge }).details ?? '').length <= 1_500);
  assert.ok((toTaskUpdate({ ...start, phase: 'end', toolOutput: 'y'.repeat(5_000) }).output ?? '').length <= 1_500);
});

test('platforms without chunk rendering get plain text, never an object', () => {
  // The fallback renderer does `accumulated += chunk`, so a chunk object would land in the
  // message as "[object Object]". This is the whole reason for the split.
  const discord = renderToolActivity(start, { name: 'discord' } as never);
  assert.equal(typeof discord, 'string');
  assert.match(discord as string, /get_weather/);

  const slack = renderToolActivity(start, { name: 'slack' } as never);
  assert.equal(typeof slack, 'object');
});

test('an unknown platform is assumed not to render chunks', () => {
  assert.equal(rendersTaskUpdates(undefined), false);
  assert.equal(rendersTaskUpdates('telegram'), false);
  assert.equal(rendersTaskUpdates('slack'), true);
});

test('every phase produces a distinguishable line', () => {
  const lines = (['start', 'end', 'error'] as const).map((phase) =>
    toActivityLine({ ...start, phase, message: 'nope' }),
  );
  assert.equal(new Set(lines).size, 3);
  for (const line of lines) assert.match(line, /get_weather/);
});

test('typing re-asserts itself, because the indicator expires', async () => {
  // Discord clears after ~10s and startTyping is one-shot on every adapter.
  let calls = 0;
  const typing = keepTyping({ startTyping: async () => { calls += 1; } });
  assert.equal(calls, 1, 'starts immediately — the gap it covers begins at once');
  typing.stop();
  const after = calls;
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(calls, after, 'stopped means stopped');
});

test('a failing typing indicator never breaks the turn it decorates', async () => {
  const errors: unknown[] = [];
  const typing = keepTyping(
    { startTyping: async () => { throw new Error('no permission'); } },
    (err) => errors.push(err),
  );
  await new Promise((r) => setTimeout(r, 10));
  typing.stop();
  assert.equal(errors.length, 1, 'reported, not thrown');
});

test('a tool-heavy turn stays under the platform message cap', () => {
  // Slack answers `msg_too_long` and the whole reply is lost, so the budget has to bound what
  // one message accumulates. Worst case: every card carrying clipped args and output.
  const worst = toTaskUpdate({
    ...start,
    phase: 'end',
    toolInput: { blob: 'x'.repeat(5_000) },
    toolOutput: 'y'.repeat(5_000),
  });
  const perCard = renderedSize(worst);
  assert.ok(perCard < 1_600, `a single card should stay small, got ${perCard}`);
  assert.ok(
    MAX_SEGMENT_CARDS * perCard < 40_000,
    'a full message of worst-case cards must stay under Slack’s limit',
  );
});

test('the budget counts text and cards alike', () => {
  assert.equal(renderedSize('hello'), 5);
  assert.ok(renderedSize(toTaskUpdate(start)) > 64, 'a card costs more than its overhead');
});

test('a call that has not started is shown as pending, not as running', () => {
  // It is approved and on its way to a worker; claiming it is in progress would be a lie, and
  // the gap between the decision and the work is exactly what the card explains.
  assert.equal(toTaskUpdate({ ...start, phase: 'pending' }).status, 'pending');
  assert.match(toActivityLine({ ...start, phase: 'pending' }), /get_weather/);
});

test('the pending line does not repeat the arguments', () => {
  // The approval prompt carries them now. Duplicating them a line apart is noise, and on a
  // platform whose text cannot be revised the duplicate would never go away.
  assert.doesNotMatch(toActivityLine({ ...start, phase: 'pending' }), /Lisbon/);
});

test('arguments render as one fenced block, shared by the prompt and the verdict', () => {
  const block = formatToolInput({ city: 'Lisbon' }) ?? '';
  assert.match(block, /```json/);
  assert.match(block, /Lisbon/);
  // Nothing to show is `undefined`, not an empty block a card would still render.
  assert.equal(formatToolInput({}), undefined);
  assert.equal(formatToolInput(undefined), undefined);
  assert.ok((formatToolInput({ blob: 'x'.repeat(5_000) }) ?? '').length < 1_000, 'clipped');
});

test('an empty stream is reported as empty, so no message is posted for it', async () => {
  // Posting one costs a real blank message: Slack finalizes an empty native stream with
  // startStream + stopStream, and the post-and-edit fallback posts a single space. A turn that
  // pauses for an approval produces two such segments, one each side of the buttons.
  const nothing = (async function* (): AsyncGenerator<string> {})();
  assert.equal(await openStream(nothing), undefined);
});

test('a stream that does have content keeps every chunk, in order', async () => {
  const stream = (async function* () {
    yield 'one';
    yield 'two';
    yield 'three';
  })();
  const opened = await openStream(stream);
  assert.ok(opened, 'content means it must be posted');
  const seen: string[] = [];
  for await (const chunk of opened) seen.push(chunk);
  assert.deepEqual(seen, ['one', 'two', 'three'], 'the peeked chunk is not swallowed');
});

test('a message never splits while a tool card is open', () => {
  // A task card lives inside ONE message. Splitting mid-call strands the pending card and
  // opens a second one in the next message — two cards for a single tool.
  assert.equal(shouldRollOver(999_999, 999, 1), false, 'one open call vetoes the split');
  assert.equal(shouldRollOver(999_999, 999, 0), true, 'nothing open, so it may split');
});

test('rollover triggers on either budget once nothing is in flight', () => {
  assert.equal(shouldRollOver(MAX_SEGMENT_CHARS, 0, 0), true);
  assert.equal(shouldRollOver(0, MAX_SEGMENT_CARDS, 0), true);
  assert.equal(shouldRollOver(0, 0, 0), false, 'a small message keeps going');
});
