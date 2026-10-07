// Turns a handler's input JSON Schema into a flat field description the composer can render.
//
// This is the whole reason the UI needs no per-agent knowledge: `agent_interface` gives every
// handler's `parameters` schema, and everything the composer shows is derived from it. No
// handler name, field name, or payload shape is hardcoded anywhere.
//
// The schemas come from pydantic, so the shapes worth handling are pydantic's:
//   * `$ref` into `$defs` for nested models and `Literal`s
//   * `enum` for a `Literal[...]` of one type, `anyOf` of `const`s for a mixed one
//   * `anyOf: [X, {type: "null"}]` for `X | None`
//   * `title` on every property (pydantic generates it from the field name)

import type { JsonRecord } from "$lib/api/types";

export type FieldKind =
  | "string"
  | "textarea"
  | "integer"
  | "number"
  | "boolean"
  | "enum"
  | "array"
  | "object"
  | "json";

export interface SchemaField {
  name: string;
  title: string;
  description?: string;
  kind: FieldKind;
  required: boolean;
  /** Allowed values when `kind === "enum"` — rendered as a dropdown. */
  choices?: string[];
  /** Element description when `kind === "array"`. */
  item?: SchemaField;
  /** Nested fields when `kind === "object"`. */
  fields?: SchemaField[];
  minimum?: number;
  maximum?: number;
  default?: unknown;
}

type Schema = Record<string, unknown>;

function asSchema(value: unknown): Schema | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Schema)
    : null;
}

/** Follow a `$ref` (only local `#/$defs/...`, which is all pydantic emits) against `root`. */
function deref(schema: Schema, root: Schema, seen: Set<string>): Schema {
  const ref = schema["$ref"];
  if (typeof ref !== "string" || !ref.startsWith("#/")) return schema;
  if (seen.has(ref)) return {};
  seen.add(ref);
  let node: unknown = root;
  for (const part of ref.slice(2).split("/")) {
    const step = asSchema(node);
    if (!step) return {};
    node = step[part];
  }
  const resolved = asSchema(node);
  return resolved ? deref(resolved, root, seen) : {};
}

/**
 * Collapse the wrappers pydantic puts around optional and union types.
 *
 * `X | None` arrives as `anyOf: [X, {type: "null"}]`; we unwrap to `X` and let `required`
 * carry the optionality, since that is what the form needs. A genuine multi-branch union has
 * no single form representation, so it is left alone and ends up as a raw JSON field.
 */
function unwrap(schema: Schema, root: Schema): Schema {
  let current = deref(schema, root, new Set());
  for (let depth = 0; depth < 8; depth++) {
    const branches = current["anyOf"] ?? current["oneOf"];
    if (!Array.isArray(branches)) return current;
    const nonNull = branches
      .map((b) => asSchema(b))
      .filter((b): b is Schema => b != null && b["type"] !== "null");
    if (nonNull.length !== 1) return current;
    current = deref(nonNull[0], root, new Set());
  }
  return current;
}

/** String choices for a `Literal`, whether emitted as `enum` or as `anyOf` of `const`s. */
function enumChoices(schema: Schema): string[] | null {
  const direct = schema["enum"];
  if (Array.isArray(direct) && direct.length > 0) {
    const strings = direct.filter((v) => typeof v === "string" || typeof v === "number");
    if (strings.length === direct.length) return strings.map(String);
  }
  const branches = schema["anyOf"] ?? schema["oneOf"];
  if (Array.isArray(branches) && branches.length > 0) {
    const consts = branches
      .map((b) => asSchema(b)?.["const"])
      .filter((v) => typeof v === "string" || typeof v === "number");
    if (consts.length === branches.length) return consts.map(String);
  }
  return null;
}

function describeField(
  name: string,
  raw: Schema,
  root: Schema,
  required: boolean
): SchemaField {
  const schema = unwrap(raw, root);
  // Pydantic puts an optional field's title, description and default on the outer `anyOf`,
  // not on the branch `unwrap` returns.
  const annotation = (key: string): unknown => raw[key] ?? schema[key];
  const text = (value: unknown): string | undefined =>
    typeof value === "string" && value.trim() ? value : undefined;
  const base = {
    name,
    title: text(annotation("title")) ?? name,
    description: text(annotation("description")),
    required,
    default: annotation("default"),
    minimum: typeof schema["minimum"] === "number" ? (schema["minimum"] as number) : undefined,
    maximum: typeof schema["maximum"] === "number" ? (schema["maximum"] as number) : undefined
  };

  const choices = enumChoices(schema);
  if (choices) return { ...base, kind: "enum", choices };

  switch (schema["type"]) {
    case "boolean":
      return { ...base, kind: "boolean" };
    case "integer":
      return { ...base, kind: "integer" };
    case "number":
      return { ...base, kind: "number" };
    case "string": {
      // Long-form text gets a textarea; a plain scalar gets an input.
      const long =
        schema["format"] === "textarea" ||
        (typeof schema["maxLength"] === "number" && schema["maxLength"] > 200);
      return { ...base, kind: long ? "textarea" : "string" };
    }
    case "array": {
      const items = asSchema(schema["items"]);
      return {
        ...base,
        kind: "array",
        item: items ? describeField(name, items, root, true) : undefined
      };
    }
    case "object": {
      const nested = describeSchema(schema, root);
      // An object with no declared properties (e.g. `dict[str, Any]`) has nothing to render
      // field-by-field, so fall back to raw JSON rather than an empty fieldset.
      return nested.length > 0
        ? { ...base, kind: "object", fields: nested }
        : { ...base, kind: "json" };
    }
    default:
      return { ...base, kind: "json" };
  }
}

/** The renderable fields of an object schema, in declaration order. */
export function describeSchema(schema: JsonRecord | Schema, root?: Schema): SchemaField[] {
  const self = asSchema(schema);
  if (!self) return [];
  const rootSchema = root ?? self;
  const properties = asSchema(self["properties"]);
  if (!properties) return [];
  const requiredList = Array.isArray(self["required"]) ? (self["required"] as unknown[]) : [];
  const required = new Set(requiredList.filter((v): v is string => typeof v === "string"));
  return Object.entries(properties).flatMap(([name, raw]) => {
    const propertySchema = asSchema(raw);
    return propertySchema
      ? [describeField(name, propertySchema, rootSchema, required.has(name))]
      : [];
  });
}

/**
 * The property name when a schema is "single-string-shaped" — exactly one property, a plain
 * string, no enum — else null.
 *
 * This is what lets a chat-shaped handler keep an ordinary text box. The name is read from
 * the schema rather than assumed, so a handler whose field is `script` or `prompt` works
 * exactly as well as one whose field is `text`.
 */
export function singleStringField(schema: JsonRecord | Schema): string | null {
  const fields = describeSchema(schema);
  if (fields.length !== 1) return null;
  const only = fields[0];
  if (only.kind !== "string" && only.kind !== "textarea") return null;
  return only.name;
}

/**
 * The form for a callback tool's result, from its output schema. An object result renders
 * field-by-field; anything else (`str`, `int`, a list) is wrapped as one `result` field, and
 * `wrapped` says to unwrap it from the built payload before sending.
 */
export function resultForm(schema: JsonRecord): { fields: SchemaField[]; wrapped: boolean } {
  const direct = describeSchema(schema);
  if (direct.length > 0) return { fields: direct, wrapped: false };
  const { $defs, ...rest } = schema;
  const wrapper: Schema = {
    type: "object",
    properties: { result: { title: "Response", ...rest } },
    required: ["result"],
    $defs
  };
  return { fields: describeSchema(wrapper), wrapped: true };
}

/** A blank form value for a field — its schema default when it has one. */
export function emptyValue(field: SchemaField): unknown {
  if (field.default !== undefined) return field.default;
  switch (field.kind) {
    case "boolean":
      return false;
    case "enum":
      return field.required ? (field.choices?.[0] ?? "") : "";
    case "array":
      return [];
    case "object":
      return Object.fromEntries((field.fields ?? []).map((f) => [f.name, emptyValue(f)]));
    default:
      return "";
  }
}

export function emptyValues(fields: SchemaField[]): Record<string, unknown> {
  return Object.fromEntries(fields.map((f) => [f.name, emptyValue(f)]));
}

function isBlank(value: unknown): boolean {
  return (
    value === "" ||
    value === undefined ||
    value === null ||
    (Array.isArray(value) && value.length === 0)
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return (asSchema(value) ?? {}) as Record<string, unknown>;
}

function fieldProblem(field: SchemaField, value: unknown): string | null {
  if (isBlank(value)) return field.required && field.kind !== "boolean" ? "Required." : null;
  if (field.kind === "enum" && field.choices && !field.choices.includes(String(value))) {
    return "Choose one of the listed options.";
  }
  if (field.kind === "integer" || field.kind === "number") {
    const num = Number(value);
    if (Number.isNaN(num)) return "Must be a number.";
    if (field.kind === "integer" && !Number.isInteger(num)) return "Must be a whole number.";
    if (field.minimum !== undefined && num < field.minimum) return `Must be at least ${field.minimum}.`;
    if (field.maximum !== undefined && num > field.maximum) return `Must be at most ${field.maximum}.`;
  }
  if (field.kind === "json" && typeof value === "string") {
    try {
      JSON.parse(value);
    } catch {
      return "Must be valid JSON.";
    }
  }
  return null;
}

/** A field's key in `validate`'s result: its name, under its parent's key. */
export function fieldPath(path: string, name: string | number): string {
  return path ? `${path}.${name}` : String(name);
}

/**
 * What is wrong with the current values, keyed by field path (`address.city`,
 * `travellers.0.name`) so the form can mark each field; empty when the form is submittable.
 *
 * Client-side validation is a convenience only — the workflow's update validator checks the
 * payload against the handler's real input model and is the authority. This just avoids a
 * round trip for the obvious mistakes.
 */
export function validate(
  fields: SchemaField[],
  values: Record<string, unknown>,
  path = ""
): Record<string, string> {
  const problems: Record<string, string> = {};
  for (const field of fields) {
    const key = fieldPath(path, field.name);
    const value = values[field.name];
    const problem = fieldProblem(field, value);
    if (problem) problems[key] = problem;
    if (field.kind === "object" && field.fields) {
      Object.assign(problems, validate(field.fields, asRecord(value), key));
    }
    const itemFields = field.kind === "array" ? field.item?.fields : undefined;
    if (itemFields && Array.isArray(value)) {
      value.forEach((entry, index) =>
        Object.assign(problems, validate(itemFields, asRecord(entry), fieldPath(key, index)))
      );
    }
  }
  return problems;
}

/** One line for the whole form, since each problem is shown beside its own field. */
export function problemSummary(problems: Record<string, string>): string | null {
  const count = Object.keys(problems).length;
  if (count === 0) return null;
  return count === 1 ? "Fix the marked field." : `Fix the ${count} marked fields.`;
}

/** Coerce form values into the JSON payload the handler's input model expects. */
export function buildPayload(
  fields: SchemaField[],
  values: Record<string, unknown>
): JsonRecord {
  const payload: JsonRecord = {};
  for (const field of fields) {
    const value = values[field.name];
    // Omit blank optionals entirely rather than sending "" — the model would reject an empty
    // string where it expects an absent field (or a default).
    if (!field.required && isBlank(value) && field.kind !== "boolean") continue;
    switch (field.kind) {
      case "boolean":
        payload[field.name] = value === true;
        break;
      case "integer":
      case "number":
        payload[field.name] = Number(value);
        break;
      case "json":
        payload[field.name] =
          typeof value === "string" ? JSON.parse(value) : (value as never);
        break;
      case "object":
        payload[field.name] = buildPayload(
          field.fields ?? [],
          (value ?? {}) as Record<string, unknown>
        );
        break;
      case "array": {
        const items = Array.isArray(value) ? value : [];
        const item = field.item;
        payload[field.name] = items
          .filter((entry) => !isBlank(entry))
          .map((entry) =>
            item?.kind === "object"
              ? buildPayload(item.fields ?? [], asRecord(entry))
              : item && (item.kind === "integer" || item.kind === "number")
                ? Number(entry)
                : entry
          ) as never;
        break;
      }
      default:
        payload[field.name] = value as never;
    }
  }
  return payload;
}
