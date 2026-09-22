import assert from 'node:assert/strict';
import { test } from 'node:test';

import { planFields, decodeModalValues, type JsonSchema } from '../src/schema-to-modal.ts';
import { fieldsToTemplate, parseFieldText } from '../src/text-form.ts';

const ASK: JsonSchema = {
  type: 'object',
  properties: { text: { type: 'string' } },
  required: ['text'],
};

const POLICY: JsonSchema = {
  type: 'object',
  properties: {
    new_policy: { enum: ['allow_safe', 'strict'], type: 'string' },
    note: { anyOf: [{ type: 'string', maxLength: 50 }, { type: 'null' }] },
  },
  required: ['new_policy'],
};

test('a single prose field takes the whole line, unquoted', () => {
  const fields = planFields(ASK);
  assert.deepEqual(parseFieldText(fields, 'what is the status of the deploy?'), {
    text: 'what is the status of the deploy?',
  });
});

test('multi-field handlers take key=value in any order', () => {
  const fields = planFields(POLICY);
  const values = parseFieldText(fields, 'note="needs review" new_policy=allow_safe');
  assert.deepEqual(values, { note: 'needs review', new_policy: 'allow_safe' });

  // Same decoder as the modal path — one place restores types.
  assert.deepEqual(decodeModalValues(fields, values), {
    ok: true,
    payload: { new_policy: 'allow_safe', note: 'needs review' },
  });
});

test('quoted values keep their spaces and their = signs', () => {
  const fields = planFields(POLICY);
  assert.deepEqual(parseFieldText(fields, `new_policy=strict note='a=b c'`), {
    new_policy: 'strict',
    note: 'a=b c',
  });
});

test('bad text fails the same way bad form input does', () => {
  const fields = planFields(POLICY);
  const result = decodeModalValues(fields, parseFieldText(fields, 'new_policy=nonsense'));
  assert.equal(result.ok, false);
  assert.match(result.ok === false ? result.errors.new_policy ?? '' : '', /must be one of/i);
});

test('a missing required field is reported, not silently dropped', () => {
  const fields = planFields(POLICY);
  const result = decodeModalValues(fields, parseFieldText(fields, 'note=hi'));
  assert.equal(result.ok, false);
  assert.ok(result.ok === false && 'new_policy' in result.errors);
});

test('the template is copy-pasteable and round-trips', () => {
  const fields = planFields(POLICY);
  const template = fieldsToTemplate('set_approval_policy', fields);
  assert.match(template, /new_policy=<allow_safe\|strict>/);
  assert.match(template, /\[note=<text>\]/, 'optional fields are bracketed');

  // What the template tells the user to type must actually parse.
  const typed = 'set_approval_policy new_policy=allow_safe'.replace('set_approval_policy ', '');
  assert.deepEqual(decodeModalValues(fields, parseFieldText(fields, typed)), {
    ok: true,
    payload: { new_policy: 'allow_safe' },
  });
});

test('the explicit form still works for a single prose field', () => {
  // The template prints `<text>`, but a user who types `text=hi` must not get `text=hi` as
  // the literal value.
  const fields = planFields(ASK);
  assert.deepEqual(parseFieldText(fields, 'text=hello'), { text: 'hello' });
});

test('usage addresses the handler directly, and names it exactly once', () => {
  const fields = planFields(POLICY);
  const template = fieldsToTemplate('set_approval_policy', fields);
  // Regression: this once rendered a wrapper command that does not exist, and from the other
  // call site duplicated the handler name.
  assert.match(template, /^`\/set_approval_policy new_policy=/);
  assert.doesNotMatch(template, /set_approval_policy set_approval_policy/);
});

test('a handler with no fields still shows how to call it', () => {
  assert.equal(fieldsToTemplate('status', []), '`/status`');
});
