import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  decodeModalValues,
  fieldsToModalChildren,
  planFields,
  type JsonSchema,
} from '../src/schema-to-modal.ts';

/** Verbatim from a live `GET /api/agent-interface/{session_id}`. */
const SET_APPROVAL_POLICY: JsonSchema = {
  description: 'Selected tool approval policy name.',
  properties: {
    new_policy: {
      enum: ['allow_safe', 'strict', 'dangerously_skip_all'],
      title: 'New Policy',
      type: 'string',
    },
  },
  required: ['new_policy'],
  title: 'PolicyUpdate',
  type: 'object',
};

test('a real harness handler becomes a usable form with no configuration', () => {
  const fields = planFields(SET_APPROVAL_POLICY);
  assert.deepEqual(fields.map((f) => [f.name, f.kind, f.required]), [
    ['new_policy', 'enum', true],
  ]);

  const [child] = fieldsToModalChildren(fields);
  // Three options, so radio rather than a dropdown.
  assert.equal(child?.type, 'radio_select');
  assert.equal(child?.label, 'New Policy');
  assert.deepEqual(
    child?.type === 'radio_select' ? child.options.map((o) => o.value) : null,
    ['allow_safe', 'strict', 'dangerously_skip_all'],
  );

  assert.deepEqual(decodeModalValues(fields, { new_policy: 'allow_safe' }), {
    ok: true,
    payload: { new_policy: 'allow_safe' },
  });
});

test('Optional[T] is unwrapped rather than dumped into a JSON box', () => {
  // How pydantic spells `note: str | None = None` — the single most common optional field.
  const schema: JsonSchema = {
    type: 'object',
    properties: {
      note: { anyOf: [{ type: 'string', maxLength: 80 }, { type: 'null' }], title: 'Note' },
    },
  };

  const [field] = planFields(schema);
  assert.equal(field?.kind, 'string', 'nullable union should collapse to its one real branch');
  assert.equal(field?.required, false);
  assert.equal(field?.jsonFallbackReason, undefined);
});

test('an enum behind a $ref still renders as a picker', () => {
  // pydantic emits Enum-typed fields as a $ref into $defs, so failing to resolve refs would
  // silently turn every enum field into hand-written JSON.
  const schema: JsonSchema = {
    type: 'object',
    properties: { mode: { $ref: '#/$defs/Mode' } },
    required: ['mode'],
    $defs: { Mode: { enum: ['fast', 'slow'], title: 'Mode', type: 'string' } },
  };

  const [field] = planFields(schema);
  assert.equal(field?.kind, 'enum');
  assert.deepEqual(field?.options, ['fast', 'slow']);
});

test('scalars round-trip back to their schema types, not strings', () => {
  const schema: JsonSchema = {
    type: 'object',
    properties: {
      count: { type: 'integer', minimum: 1, maximum: 10 },
      ratio: { type: 'number' },
      force: { type: 'boolean' },
      when: { type: 'string', format: 'date' },
    },
    required: ['count', 'ratio', 'force', 'when'],
  };
  const fields = planFields(schema);

  assert.deepEqual(
    fieldsToModalChildren(fields).map((c) => c.type),
    ['number_input', 'number_input', 'radio_select', 'date_input'],
  );

  assert.deepEqual(
    decodeModalValues(fields, { count: '3', ratio: '0.5', force: 'false', when: '2026-01-02' }),
    { ok: true, payload: { count: 3, ratio: 0.5, force: false, when: '2026-01-02' } },
  );
});

test('bad input comes back as per-field errors, not a thrown submit', () => {
  const schema: JsonSchema = {
    type: 'object',
    properties: {
      count: { type: 'integer', minimum: 5 },
      mode: { enum: ['a', 'b'], type: 'string' },
      needed: { type: 'string', maxLength: 10 },
    },
    required: ['count', 'mode', 'needed'],
  };
  const fields = planFields(schema);

  const result = decodeModalValues(fields, { count: '2.5', mode: 'z', needed: '   ' });
  assert.equal(result.ok, false);
  assert.deepEqual(Object.keys(result.ok === false ? result.errors : {}).sort(), [
    'count',
    'mode',
    'needed',
  ]);
});

test('an omitted optional is absent from the payload, so the handler default wins', () => {
  const schema: JsonSchema = {
    type: 'object',
    properties: { text: { type: 'string' }, note: { type: 'string', maxLength: 20 } },
    required: ['text'],
  };
  const fields = planFields(schema);

  const result = decodeModalValues(fields, { text: 'hi', note: '' });
  assert.deepEqual(result, { ok: true, payload: { text: 'hi' } });
  assert.ok(!('note' in (result.ok ? result.payload : {})), 'must not send an empty string');
});

test('what a modal cannot express degrades to JSON, and says why', () => {
  const schema: JsonSchema = {
    type: 'object',
    properties: {
      tags: { type: 'array', title: 'Tags' },
      nested: { type: 'object', title: 'Nested' },
    },
    required: ['tags', 'nested'],
  };
  const fields = planFields(schema);

  assert.deepEqual(fields.map((f) => f.kind), ['json', 'json']);
  assert.match(fields[0]?.jsonFallbackReason ?? '', /cannot repeat/);
  assert.match(fields[1]?.jsonFallbackReason ?? '', /cannot nest/);

  assert.deepEqual(decodeModalValues(fields, { tags: '["a","b"]', nested: '{"k":1}' }), {
    ok: true,
    payload: { tags: ['a', 'b'], nested: { k: 1 } },
  });

  const bad = decodeModalValues(fields, { tags: 'not json', nested: '{}' });
  assert.equal(bad.ok, false);
});

test('an unbounded string is prose, a bounded one is a single line', () => {
  const schema: JsonSchema = {
    type: 'object',
    properties: { prompt: { type: 'string' }, slug: { type: 'string', maxLength: 32 } },
  };
  const [prompt, slug] = fieldsToModalChildren(planFields(schema));
  assert.equal(prompt?.type === 'text_input' && prompt.multiline, true);
  assert.equal(slug?.type === 'text_input' && slug.multiline, false);
});

test('a label is always present, even with no title in the schema', () => {
  const [field] = planFields({ type: 'object', properties: { new_policy: { type: 'string' } } });
  assert.equal(field?.label, 'New Policy');
});
