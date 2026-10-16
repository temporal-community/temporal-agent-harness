import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseIntent, stripLeadingMentions } from '../src/command.ts';

test('talking to the bot is just talking — no slash command needed', () => {
  assert.deepEqual(parseIntent('<@1475474784136593500> hey'), { kind: 'ask', text: 'hey' });
  assert.deepEqual(parseIntent('how is the deploy going?'), {
    kind: 'ask',
    text: 'how is the deploy going?',
  });
});

test('a leading slash addresses a specific handler', () => {
  assert.deepEqual(parseIntent('<@123> /set_approval_policy new_policy=allow_safe'), {
    kind: 'handler',
    name: 'set_approval_policy',
    args: 'new_policy=allow_safe',
  });
  assert.deepEqual(parseIntent('/run_script'), { kind: 'handler', name: 'run_script', args: '' });
});

test('/help and friends ask for the guide', () => {
  for (const text of ['/help', '/ ', '/?', '<@1> /handlers', '/COMMANDS']) {
    assert.equal(parseIntent(text).kind, 'guide', text);
  }
});

test('mention markup never reaches the agent', () => {
  assert.equal(stripLeadingMentions('<@!123> <@&456> hello'), 'hello');
  // Only LEADING mentions: naming someone mid-sentence is content, not addressing.
  assert.equal(stripLeadingMentions('tell <@99> hello'), 'tell <@99> hello');
});

test('a slash inside the message body is not a command', () => {
  assert.deepEqual(parseIntent('<@1> what is 3/4 of 12?'), {
    kind: 'ask',
    text: 'what is 3/4 of 12?',
  });
});

test('an empty mention still asks, so the agent can greet', () => {
  assert.deepEqual(parseIntent('<@1>'), { kind: 'ask', text: '' });
});
