/**
 * Project one `AcceptedFunction.parameters` JSON Schema onto a chat-platform modal, and
 * decode the submitted form back into the payload that schema describes.
 *
 * Why this module is the load-bearing one: the harness lets an agent accept any number of
 * typed handlers, and `GET /api/agent-interface/{session_id}` hands us their schemas at
 * runtime. Everything else in this server is plumbing; this is the part that decides what a
 * given agent actually *looks like* on Slack without anyone configuring it.
 *
 * Two facts about the target shape it has to absorb:
 *
 *   1. A modal is FLAT. `ModalChild` is text/number/date/select/radio and nothing else — no
 *      checkbox, no nesting, no repetition. A JSON Schema is a tree. Anything that is not a
 *      scalar therefore degrades to a JSON text field, which is honest but plainly worse; see
 *      `jsonFallbackReason` on the emitted plan, which the caller can surface.
 *   2. Every submitted value comes back as a STRING (`ModalSubmitEvent.values` is
 *      `Record<string, string>`). So encoding a field and decoding it are two halves of one
 *      decision, and they are kept together here: `planFields` produces a `FieldPlan[]` that
 *      drives BOTH `fieldsToModalChildren` and `decodeModalValues`. Adding a type means
 *      touching one switch, not two that can drift.
 *
 * The harness workflow never sees a partial form: the platform holds the draft until submit,
 * we decode it here, and one complete structured message goes out. No state is accumulated on
 * this server or in the agent.
 */

import type { ModalChild, SelectOptionElement } from 'chat';

/** A JSON Schema node, kept loose — these come from pydantic at runtime, not from our types. */
export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  enum?: unknown[];
  format?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  default?: unknown;
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  definitions?: Record<string, JsonSchema>;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
}

/** How one form field is rendered and, symmetrically, how its string comes back. */
export type FieldKind = 'string' | 'text' | 'number' | 'integer' | 'boolean' | 'enum' | 'date' | 'json';

export interface FieldPlan {
  /** The JSON Schema property name. Also the modal input id, so submit values key by it. */
  name: string;
  kind: FieldKind;
  label: string;
  description?: string;
  required: boolean;
  /** Allowed values, for `kind: 'enum'`. */
  options?: string[];
  min?: number;
  max?: number;
  default?: unknown;
  /**
   * Set when this field could not be represented natively and fell back to a JSON text input.
   * Worth showing the user — it is the difference between "fill in the form" and "hand-write
   * JSON", and they deserve to know which one they are being asked for.
   */
  jsonFallbackReason?: string;
}

/** An enum small enough that radio buttons beat a dropdown. Purely a UX threshold. */
const RADIO_MAX_OPTIONS = 4;

/**
 * A string with no length bound is prose (an agent prompt, a description) far more often than
 * it is a short token, so it gets a textarea. A bounded one is treated as a single-line value.
 */
const MULTILINE_WHEN_MAXLENGTH_ABOVE = 200;

/** Resolve a local `$ref` (`#/$defs/Foo`). Pydantic emits these for nested models AND enums. */
function resolveRef(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (!schema.$ref?.startsWith('#/')) return schema;
  const path = schema.$ref.slice(2).split('/');
  let node: unknown = root;
  for (const segment of path) {
    if (typeof node !== 'object' || node === null) return schema;
    node = (node as Record<string, unknown>)[segment];
  }
  return typeof node === 'object' && node !== null ? (node as JsonSchema) : schema;
}

/**
 * Collapse `anyOf: [T, null]` to `T`.
 *
 * This is not an edge case: it is how pydantic spells every `Optional[T]` field, so without it
 * the most ordinary optional string in any agent would render as a raw JSON box.
 */
function unwrapNullableUnion(schema: JsonSchema): { schema: JsonSchema; nullable: boolean } {
  const branches = schema.anyOf ?? schema.oneOf;
  if (!branches) return { schema, nullable: false };
  const nonNull = branches.filter((b) => b.type !== 'null');
  if (nonNull.length === 1 && nonNull[0]) {
    return { schema: { ...nonNull[0], title: schema.title ?? nonNull[0].title, description: schema.description ?? nonNull[0].description }, nullable: true };
  }
  return { schema, nullable: false };
}

/** `new_policy` -> `New Policy`. Only used when the schema supplies no title. */
function humanize(name: string): string {
  return name
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z\d])([A-Z])/g, '$1 $2')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function classify(schema: JsonSchema): { kind: FieldKind; options?: string[]; jsonFallbackReason?: string } {
  if (Array.isArray(schema.enum) && schema.enum.length > 0) {
    // Enum values must survive a round trip through a string form field, so only stringy
    // enums render as a picker; a mixed-type enum keeps its fidelity as JSON instead.
    if (schema.enum.every((v) => typeof v === 'string')) {
      return { kind: 'enum', options: schema.enum as string[] };
    }
    return { kind: 'json', jsonFallbackReason: 'enum has non-string members' };
  }

  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  switch (type) {
    case 'string':
      if (schema.format === 'date') return { kind: 'date' };
      return {
        kind:
          schema.maxLength === undefined || schema.maxLength > MULTILINE_WHEN_MAXLENGTH_ABOVE
            ? 'text'
            : 'string',
      };
    case 'integer':
      return { kind: 'integer' };
    case 'number':
      return { kind: 'number' };
    case 'boolean':
      return { kind: 'boolean' };
    case 'object':
      return { kind: 'json', jsonFallbackReason: 'nested object — a modal cannot nest' };
    case 'array':
      return { kind: 'json', jsonFallbackReason: 'list — a modal cannot repeat a field' };
    default:
      return { kind: 'json', jsonFallbackReason: 'no single concrete type' };
  }
}

/** Turn a handler's `parameters` schema into an ordered field plan. */
export function planFields(parameters: JsonSchema): FieldPlan[] {
  const properties = parameters.properties ?? {};
  const required = new Set(parameters.required ?? []);

  return Object.entries(properties).map(([name, raw]) => {
    const { schema: unwrapped, nullable } = unwrapNullableUnion(resolveRef(raw, parameters));
    const resolved = resolveRef(unwrapped, parameters);
    const { kind, options, jsonFallbackReason } = classify(resolved);

    return {
      name,
      kind,
      label: resolved.title ?? raw.title ?? humanize(name),
      description: resolved.description ?? raw.description,
      required: required.has(name) && !nullable,
      options,
      min: resolved.minimum,
      max: resolved.maximum,
      default: resolved.default ?? raw.default,
      jsonFallbackReason,
    };
  });
}

function options(values: string[]): SelectOptionElement[] {
  return values.map((value) => ({ label: humanize(value), value }));
}

/** Render a field plan as modal inputs. */
export function fieldsToModalChildren(fields: FieldPlan[]): ModalChild[] {
  return fields.map((field): ModalChild => {
    const base = { id: field.name, label: field.label, optional: !field.required };

    switch (field.kind) {
      case 'enum': {
        const values = field.options ?? [];
        const initial = typeof field.default === 'string' ? field.default : undefined;
        return values.length <= RADIO_MAX_OPTIONS
          ? { ...base, type: 'radio_select', options: options(values), initialOption: initial }
          : { ...base, type: 'select', options: options(values), initialOption: initial, placeholder: field.description };
      }
      case 'boolean': {
        // No checkbox exists in ModalChild, so a boolean is a two-option radio. Labelled
        // Yes/No rather than true/false because this is a form for a person.
        const initial = typeof field.default === 'boolean' ? String(field.default) : undefined;
        return {
          ...base,
          type: 'radio_select',
          options: [
            { label: 'Yes', value: 'true' },
            { label: 'No', value: 'false' },
          ],
          initialOption: initial,
        };
      }
      case 'integer':
      case 'number':
        return {
          ...base,
          type: 'number_input',
          decimal: field.kind === 'number',
          min: field.min,
          max: field.max,
          initialValue: typeof field.default === 'number' ? field.default : undefined,
          placeholder: field.description,
        };
      case 'date':
        return {
          ...base,
          type: 'date_input',
          initialValue: typeof field.default === 'string' ? field.default : undefined,
          placeholder: field.description,
        };
      case 'json':
        return {
          ...base,
          type: 'text_input',
          multiline: true,
          placeholder: field.description
            ? `${field.description} (JSON)`
            : `JSON value — ${field.jsonFallbackReason ?? 'no simpler form available'}`,
          initialValue: field.default === undefined ? undefined : JSON.stringify(field.default),
        };
      case 'text':
      case 'string':
        return {
          ...base,
          type: 'text_input',
          multiline: field.kind === 'text',
          placeholder: field.description,
          initialValue: typeof field.default === 'string' ? field.default : undefined,
        };
    }
  });
}

export type DecodeResult =
  | { ok: true; payload: Record<string, unknown> }
  /** Keyed by field name, which is the modal input id — the shape `ModalErrorsResponse` wants. */
  | { ok: false; errors: Record<string, string> };

/**
 * Decode submitted form strings back into the payload the schema describes.
 *
 * Everything arrives as a string, so this is where types are restored. Errors are returned per
 * field rather than thrown: the caller answers the submission with `{action: 'errors'}` and the
 * user fixes the field in place, instead of the modal closing and the mistake surfacing as an
 * agent-side validation failure several seconds later.
 */
export function decodeModalValues(
  fields: FieldPlan[],
  values: Record<string, string>,
): DecodeResult {
  const payload: Record<string, unknown> = {};
  const errors: Record<string, string> = {};

  for (const field of fields) {
    const raw = values[field.name];
    const empty = raw === undefined || raw.trim() === '';

    if (empty) {
      // An omitted optional field is omitted from the payload entirely rather than sent as
      // null: the schema's own default should win, and only the handler knows what it is.
      if (field.required) errors[field.name] = 'Required.';
      continue;
    }

    switch (field.kind) {
      case 'integer':
      case 'number': {
        const parsed = Number(raw);
        if (!Number.isFinite(parsed)) {
          errors[field.name] = 'Must be a number.';
        } else if (field.kind === 'integer' && !Number.isInteger(parsed)) {
          errors[field.name] = 'Must be a whole number.';
        } else if (field.min !== undefined && parsed < field.min) {
          errors[field.name] = `Must be at least ${field.min}.`;
        } else if (field.max !== undefined && parsed > field.max) {
          errors[field.name] = `Must be at most ${field.max}.`;
        } else {
          payload[field.name] = parsed;
        }
        break;
      }
      case 'boolean':
        payload[field.name] = raw === 'true';
        break;
      case 'enum':
        // The platform should only ever return an offered option, but this value is on its way
        // into a workflow update, so it is checked rather than trusted.
        if (field.options && !field.options.includes(raw)) {
          errors[field.name] = `Must be one of: ${field.options.join(', ')}.`;
        } else {
          payload[field.name] = raw;
        }
        break;
      case 'json':
        try {
          payload[field.name] = JSON.parse(raw);
        } catch (err) {
          errors[field.name] = `Invalid JSON: ${(err as Error).message}`;
        }
        break;
      case 'date':
      case 'string':
      case 'text':
        payload[field.name] = raw;
        break;
    }
  }

  return Object.keys(errors).length > 0 ? { ok: false, errors } : { ok: true, payload };
}
