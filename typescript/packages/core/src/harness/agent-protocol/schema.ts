// Schemas: any Standard Schema that can also render JSON Schema (zod v4 is one), or raw JSON
// Schema wrapped with `jsonSchema()`. The harness needs JSON Schema for agent_interface, model
// tool declarations and Code Mode stubs, and synchronous validation (update validators are sync).
// TypeScript only: Python's models are pydantic's, which carry both.

import type {
  StandardJSONSchemaV1,
  StandardSchemaV1,
  StandardTypedV1,
} from "@standard-schema/spec";


/** Standard Schema (`~standard.validate`) plus Standard JSON Schema (`~standard.jsonSchema`). */
export interface Schema<I = unknown, O = I> {
  readonly "~standard": StandardSchemaV1.Props<I, O> & StandardJSONSchemaV1.Props<I, O>;
}
export type InferInput<S extends Schema> = StandardTypedV1.InferInput<S>;
export type InferOutput<S extends Schema> = StandardTypedV1.InferOutput<S>;

/**
 * JSON Schema of a schema: what it accepts (`input`, the default) or what parsing returns (`output`).
 * zod renders each mode as it behaves: an input object is open, since parsing ignores extra keys,
 * and an output object is closed.
 */
export function toJsonSchema(
  schema: Schema,
  io: "input" | "output" = "input",
): Record<string, unknown> {
  const libraryOptions = { unrepresentable: "any" };
  const { $schema: _drop, ...rest } = schema["~standard"].jsonSchema[io]({
    target: "draft-2020-12",
    libraryOptions,
  });
  return rest;
}

/** Validate synchronously; schemas with async refinements are rejected. */
export function validate<S extends Schema>(
  schema: S,
  value: unknown,
): { ok: true; value: InferOutput<S> } | { ok: false; message: string } {
  const result = schema["~standard"].validate(value);
  if (result instanceof Promise) throw new TypeError("harness schemas must validate synchronously");
  if (result.issues === undefined) return { ok: true, value: result.value as InferOutput<S> };
  return {
    ok: false,
    message: result.issues
      .map(
        (i) =>
          (i.path?.length
            ? `${i.path.map((p) => String(typeof p === "object" ? p.key : p)).join(".")}: `
            : "") + i.message,
      )
      .join("; "),
  };
}

/**
 * Raw JSON Schema as a harness schema (the AI SDK's `jsonSchema()` counterpart). Values pass
 * unchecked unless `validate` is given, so prefer a schema library where input is untrusted.
 */
export function jsonSchema<T = unknown>(
  schema: Record<string, unknown>,
  options: {
    validate?: (value: unknown) => { ok: true; value: T } | { ok: false; message: string };
  } = {},
): Schema<T> {
  const render = () => schema;
  return {
    "~standard": {
      version: 1,
      vendor: "temporal-agent-harness",
      validate(value) {
        if (options.validate === undefined) return { value: value as T };
        const r = options.validate(value);
        return r.ok ? { value: r.value } : { issues: [{ message: r.message }] };
      },
      jsonSchema: { input: render, output: render },
    },
  };
}
