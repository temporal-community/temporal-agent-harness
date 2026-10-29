// The host functions as the model reads them, and as Python stubs Monty type-checks scripts
// against.

import type { AnyHarnessTool } from "../agent-workflow/types.ts";
import { pyRepr } from "../py-repr.ts";

export { pyRepr };
import { toJsonSchema } from "../agent-protocol/index.ts";

export interface TypeCheckStubs {
  source: string;
  type_names: string[];
}

// ---------------------------------------------------------------------------------------------
// Python stubs from JSON Schema
// ---------------------------------------------------------------------------------------------

export class CodeModeStubError extends Error {
  override name = "CodeModeStubError";
}

export type Schema = Record<string, unknown>;
const SEPARATOR = /[_\-\s(). ]/;
const WORD = /\w/;

/**
 * `s` in PascalCase without non-word characters: what
 * `s.replace(/(^|[_\-\s(). ]+)(\w)/g, upper).replace(/\W/g, "")` makes, in one pass (that regex
 * retries inside every run of separators, which is quadratic).
 */
function pascal(s: string): string {
  let out = "";
  let i = 0;
  if (s.length > 0 && WORD.test(s[0]!)) out += s[i++]!.toUpperCase();
  while (i < s.length) {
    let run = i;
    while (run < s.length && SEPARATOR.test(s[run]!)) run++;
    if (run === i) {
      out += s[i++];
    } else if (run < s.length && WORD.test(s[run]!)) {
      out += s[run]!.toUpperCase();
      i = run + 1;
    } else {
      // No word follows the run: the regex backs off to the run's last `_` after its first
      // character, which is a word character too, and replaces the span with it.
      let underscore = run - 1;
      while (underscore > i && s[underscore] !== "_") underscore--;
      if (underscore > i) {
        out += "_";
        i = underscore + 1;
      } else {
        out += s.slice(i, run);
        i = run;
      }
    }
  }
  return out.replace(/\W/g, "");
}
const pyLiteral = (v: unknown) =>
  typeof v === "string"
    ? JSON.stringify(v)
    : v === true
      ? "True"
      : v === false
        ? "False"
        : v === null
          ? "None"
          : String(v);

class StubBuilder {
  readonly #classes: { name: string; fields: string[] }[] = [];
  readonly #byKey = new Map<string, string>();
  readonly #used = new Set<string>();
  readonly #imports = new Set<string>();

  build(tools: readonly AnyHarnessTool[], withDoc: boolean): string {
    const funcs = tools.map((t) => this.#renderTool(t, withDoc));
    if (this.#classes.length) this.#imports.add("TypedDict");
    const header = ["from __future__ import annotations"];
    if (this.#imports.size)
      header.push(`from typing import ${[...this.#imports].sort().join(", ")}`);
    const sections = [header.join("\n")];
    if (this.#classes.length) {
      sections.push(
        this.#classes
          .map(
            (c) =>
              `class ${c.name}(TypedDict):\n${c.fields.length ? c.fields.join("\n") : "    pass"}`,
          )
          .join("\n\n"),
      );
    }
    if (funcs.length) sections.push(funcs.join("\n\n"));
    return `${sections.join("\n\n\n")}\n`;
  }

  typeNames(): string[] {
    return this.#classes.map((c) => c.name);
  }

  #renderTool(tool: AnyHarnessTool, withDoc: boolean): string {
    if (tool.outputSchema === undefined) {
      throw new CodeModeStubError(
        `tool '${tool.name}' has no output schema; Code Mode requires a typed result so scripts can be checked against it.`,
      );
    }
    const input = tool.parameters;
    const required = new Set((input.required as string[] | undefined) ?? []);
    const props = Object.entries((input.properties as Record<string, Schema> | undefined) ?? {});
    const params = [
      ...props
        .filter(([n]) => required.has(n))
        .map(([n, s]) => `${n}: ${this.#type(s, input, `${tool.name}(${n})`)}`),
      ...props
        .filter(([n]) => !required.has(n))
        .map(([n, s]) => `${n}: ${this.#type(s, input, `${tool.name}(${n})`)} = ...`),
    ];
    const outSchema = toJsonSchema(tool.outputSchema, "output");
    const signature = `async def ${tool.name}(${params.join(", ")}) -> ${this.#type(outSchema, outSchema, `${tool.name} return`)}`;
    const text = tool.description.trim().replaceAll('"""', '\\"\\"\\"');
    if (!withDoc || !text) return `${signature}: ...`;
    const doc = text
      .split("\n")
      .map((line) => `    ${line}`.trimEnd())
      .join("\n");
    return `${signature}:\n    """\n${doc}\n    """`;
  }

  #type(schema: Schema, root: Schema, ctx: string): string {
    if (typeof schema.$ref === "string") {
      const key = schema.$ref.replace(/^#\/(\$defs|definitions)\//, "");
      const target = ((root.$defs ?? root.definitions) as Record<string, Schema> | undefined)?.[
        key
      ];
      if (target === undefined)
        throw new CodeModeStubError(`${ctx}: unresolvable $ref ${schema.$ref}`);
      return this.#type({ title: key, ...target }, root, ctx);
    }
    const union = (schema.anyOf ?? schema.oneOf) as Schema[] | undefined;
    if (union) return [...new Set(union.map((s) => this.#type(s, root, ctx)))].join(" | ");
    if (schema.const !== undefined) {
      this.#imports.add("Literal");
      return `Literal[${pyLiteral(schema.const)}]`;
    }
    if (Array.isArray(schema.enum)) {
      this.#imports.add("Literal");
      return `Literal[${schema.enum.map(pyLiteral).join(", ")}]`;
    }
    const type = schema.type;
    if (Array.isArray(type))
      return type.map((t) => this.#type({ ...schema, type: t }, root, ctx)).join(" | ");
    switch (type) {
      case "string":
        return "str";
      case "integer":
        return "int";
      case "number":
        return "float";
      case "boolean":
        return "bool";
      case "null":
        return "None";
      case "array": {
        if (typeof schema.items !== "object" || schema.items === null) {
          throw new CodeModeStubError(
            `${ctx}: array without an element type; give its items a schema for static validation.`,
          );
        }
        return `list[${this.#type(schema.items as Schema, root, ctx)}]`;
      }
      case "object": {
        if (schema.properties !== undefined) return this.#typedDict(schema, root, ctx);
        const extra = schema.additionalProperties;
        if (typeof extra === "object" && extra !== null)
          return `dict[str, ${this.#type(extra as Schema, root, ctx)}]`;
        this.#imports.add("Any");
        return "dict[str, Any]";
      }
      case undefined:
        this.#imports.add("Any");
        return "Any";
      default:
        throw new CodeModeStubError(
          `${ctx}: cannot represent JSON Schema type ${JSON.stringify(type)} in Code Mode stubs.`,
        );
    }
  }

  #typedDict(schema: Schema, root: Schema, ctx: string): string {
    // A model's input and output schemas differ only in these, and render to the same TypedDict.
    const { $schema: _s, additionalProperties: _a, ...shape } = schema;
    const key = JSON.stringify(shape);
    const known = this.#byKey.get(key);
    if (known !== undefined) return known;
    let name =
      typeof schema.title === "string"
        ? pascal(schema.title)
        : pascal(ctx.replace(/ return$/, "_result"));
    for (let i = 2; this.#used.has(name); i++) name = `${name.replace(/_\d+$/, "")}_${i}`;
    this.#used.add(name);
    this.#byKey.set(key, name);
    const entry = { name, fields: [] as string[] };
    this.#classes.push(entry);
    const required = new Set((schema.required as string[] | undefined) ?? []);
    for (const [field, fieldSchema] of Object.entries(
      schema.properties as Record<string, Schema>,
    )) {
      let rendered = this.#type(fieldSchema, root, `${name}.${field}`);
      if (!required.has(field)) {
        this.#imports.add("NotRequired");
        rendered = `NotRequired[${rendered}]`;
      }
      entry.fields.push(`    ${field}: ${rendered}`);
    }
    return name;
  }
}

export function renderTypeCheckStubs(tools: readonly AnyHarnessTool[]): TypeCheckStubs {
  const builder = new StubBuilder();
  const source = builder.build(tools, false);
  return { source, type_names: builder.typeNames() };
}

export function renderHostInterface(tools: readonly AnyHarnessTool[]): string {
  return new StubBuilder().build(tools, true);
}
