import assert from 'node:assert/strict';
import { test } from 'node:test';
import { toCardElement } from 'chat';

import {
  needsDisabledButtons,
  pendingApprovalCard,
  resolvedApprovalCard,
  settleApprovalPrompt,
  type LivePrompt,
} from '../src/app.tsx';

const prompt = (toolInput?: Record<string, unknown>) =>
  toCardElement(
    pendingApprovalCard({
      toolName: 'delete_file',
      ...(toolInput ? { toolInput } : {}),
      shortId: 'a1b2',
    }),
  )!;

const card = (adapterName: string | undefined, approved = true) =>
  toCardElement(
    resolvedApprovalCard({
      toolName: 'delete_everything',
      approved,
      decidedBy: 'alice',
      adapterName,
    }),
  )!;

const actions = (c: ReturnType<typeof card>) =>
  c.children.find((child) => child.type === 'actions');

test('the prompt shows WHAT is about to run, not just its name', () => {
  // "Run `delete_file`?" is unanswerable on its own, and the tool card that would otherwise
  // carry the arguments is a Slack-native stream chunk that silently does not render unless
  // the app has the agent feature, the scope, and a live native stream.
  const rendered = JSON.stringify(prompt({ path: '/etc/passwd', recursive: true }));
  assert.match(rendered, /delete_file/);
  assert.match(rendered, /etc\/passwd/, 'the arguments are the question');
  assert.match(rendered, /recursive/);
});

test('a no-argument call says so rather than showing an empty block', () => {
  const rendered = JSON.stringify(prompt());
  assert.match(rendered, /no arguments/i);
  assert.doesNotMatch(rendered, /```/);
});

test('the prompt offers exactly the three decisions the harness accepts', () => {
  const block = actions(prompt({ path: '/tmp/x' }));
  assert.ok(block && block.type === 'actions');
  const ids = block.type === 'actions' ? block.children.map((b) => b.type === 'button' && b.id) : [];
  assert.deepEqual(ids, ['approve', 'approve-remember', 'deny']);
});

test('every button carries the short id, because the full tool id does not fit', () => {
  const block = actions(prompt());
  const values =
    block?.type === 'actions' ? block.children.map((b) => (b.type === 'button' ? b.value : '')) : [];
  assert.deepEqual(values, ['a1b2', 'a1b2', 'a1b2']);
});

test('Discord gets a DISABLED replacement button, or the old ones stay live', () => {
  // Discord PATCHes `components` only when the new payload has some. A card with no actions
  // updates the embed and leaves the original Approve/Deny buttons clickable.
  const block = actions(card('discord'));
  assert.ok(block, 'discord must receive replacement components');
  assert.equal(block.type === 'actions' && block.children.length, 1);
  const button = block.type === 'actions' ? block.children[0] : undefined;
  assert.equal(button?.type === 'button' && button.disabled, true);
});

test('Slack gets NO actions, because Block Kit has no disabled state', () => {
  // chat.update replaces blocks wholesale, so omitting actions removes the buttons. Sending a
  // "disabled" button instead would render as a live one.
  assert.equal(actions(card('slack')), undefined);
});

test('an unknown platform defaults to omitting actions', () => {
  assert.equal(actions(card(undefined)), undefined);
  assert.equal(needsDisabledButtons(undefined), false);
});

test('the verdict records the decision and who made it', () => {
  const approvedText = JSON.stringify(card('slack', true));
  assert.match(approvedText, /Approved/);
  assert.match(approvedText, /alice/);
  assert.match(approvedText, /delete_everything/);

  const deniedText = JSON.stringify(card('slack', false));
  assert.match(deniedText, /Denied/);
  assert.doesNotMatch(deniedText, /"Approved"/);
});

test('a denial keeps the arguments, because nothing else will ever show them', () => {
  // An approved prompt is deleted and the call's own card takes its place. A denied call never
  // runs and emits no further event, so this card is the only record it ever existed.
  const rendered = JSON.stringify(
    toCardElement(
      resolvedApprovalCard({
        toolName: 'delete_file',
        toolInput: { path: '/etc/passwd' },
        approved: false,
        decidedBy: 'alice',
        adapterName: 'slack',
      }),
    )!,
  );
  assert.match(rendered, /etc\/passwd/);
});

test('a decision made elsewhere still settles, without inventing a decider', () => {
  // The turn settles a resolution it did not see clicked — one made in the harness UI, swept
  // up by an "Always allow", or auto-denied when the session closed. Naming a user there would
  // be a fabrication.
  const rendered = JSON.stringify(
    toCardElement(
      resolvedApprovalCard({
        toolName: 'delete_file',
        approved: false,
        reason: 'the session closed with the approval still pending',
        adapterName: 'slack',
      }),
    )!,
  );
  assert.match(rendered, /Denied/);
  assert.match(rendered, /session closed/);
  assert.doesNotMatch(rendered, /\bby\b/, 'no decider is claimed');
});

// -- what a decision does to the message that asked ----------------------------------------

interface Recorder {
  prompt: LivePrompt;
  deleted: number;
  edits: string[];
}

const recorder = (options: { refuseDelete?: boolean } = {}): Recorder => {
  const state: Recorder = {
    deleted: 0,
    edits: [],
    prompt: {
      gate: { toolId: 'toolu_01A', toolName: 'delete_file', toolInput: { path: '/etc/passwd' } },
      adapterName: 'slack',
      message: {
        delete: async () => {
          if (options.refuseDelete) throw new Error('cant_delete_message');
          state.deleted += 1;
        },
        edit: async (card) => {
          state.edits.push(JSON.stringify(toCardElement(card)));
        },
      },
    },
  };
  return state;
};

test('an approved prompt is DELETED, so the tool card takes its place in the timeline', async () => {
  // The point of the whole arrangement: the transcript should show when the agent did the
  // work, not a settled question sitting above it.
  const state = recorder();
  await settleApprovalPrompt(state.prompt, { approved: true, decidedBy: 'alice' });
  assert.equal(state.deleted, 1);
  assert.deepEqual(state.edits, [], 'nothing is left behind to rewrite');
});

test('a denied prompt is kept as the verdict, because nothing follows a denial', async () => {
  const state = recorder();
  await settleApprovalPrompt(state.prompt, { approved: false, decidedBy: 'alice' });
  assert.equal(state.deleted, 0);
  assert.equal(state.edits.length, 1);
  assert.match(state.edits[0]!, /Denied/);
  assert.match(state.edits[0]!, /etc\/passwd/, 'the request itself survives');
});

test('a platform that refuses to delete gets a verdict, never live buttons', async () => {
  const state = recorder({ refuseDelete: true });
  const warnings: string[] = [];
  await settleApprovalPrompt(
    state.prompt,
    { approved: true, decidedBy: 'alice' },
    { warn: (message: string) => warnings.push(message) },
  );
  assert.equal(state.edits.length, 1, 'falls back to rewriting the prompt');
  assert.match(state.edits[0]!, /Approved/);
  assert.equal(warnings.length, 1, 'reported, not thrown');
});

test('a failed rewrite is swallowed: the decision is already recorded', async () => {
  const warnings: string[] = [];
  await settleApprovalPrompt(
    {
      gate: { toolId: 'toolu_01A', toolName: 'delete_file' },
      message: {
        delete: async () => { throw new Error('nope'); },
        edit: async () => { throw new Error('also nope'); },
      },
    },
    { approved: true },
    { warn: (message: string) => warnings.push(message) },
  );
  assert.equal(warnings.length, 2, 'both failures are visible in the log');
});
