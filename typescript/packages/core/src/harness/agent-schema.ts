// `temporal-agent-harness schema`: an agent's init data, handlers and observable state as one JSON
// Schema document, in the shape @temporal-agent-harness/codegen consumes. Each model becomes a
// `$defs` entry named by its zod `.meta({ title })`, else by where it appears; a titled object
// nested inside one gets its own entry too. Init data and handler inputs are in input mode (a
// defaulted field may be omitted by a sender); handler outputs and states in output mode (every
// field arrives). A model whose two modes differ appears as `Name-Input` and `Name-Output`, as
// pydantic names them.

import type { AgentWorkflow } from "./agent.ts";
import { toJsonSchema, type Schema } from "./agent-protocol/index.ts";

type Io = "input" | "output";
type Json = Record<string, unknown>;

// Refs are written as `PENDING name \0 io` and resolved once every model has been seen, because
// whether a name splits into -Input/-Output depends on all of them.
const PENDING = "\0pending:";
const isObject = (v: unknown): v is Json =>
  v !== null && typeof v === "object" && !Array.isArray(v);
const LITERAL_KEYS = new Set(["default", "const", "enum", "examples"]);

export function agentSchema(agent: AgentWorkflow, source: string): Json {
  const def = agent.definition;
  const found = new Map<string, Partial<Record<Io, Json>>>();
  const owners = new Map<string, Set<Schema>>();
  const register = (name: string, io: Io, body: Json): Json => {
    const modes = found.get(name) ?? {};
    const prior = modes[io];
    if (prior !== undefined && JSON.stringify(prior) !== JSON.stringify(body))
      throw collision(name);
    found.set(name, { ...modes, [io]: body });
    return { $ref: `${PENDING}${name}\0${io}` };
  };
  const collision = (name: string) =>
    new Error(
      `${def.name}: two different models are both named '${name}'; give each a unique .meta({ title })`,
    );
  const hoist = (node: unknown, io: Io, top: boolean): unknown => {
    if (Array.isArray(node)) return node.map((n) => hoist(n, io, false));
    if (!isObject(node)) return node;
    const out = Object.fromEntries(
      Object.entries(node).map(([k, v]) => [k, LITERAL_KEYS.has(k) ? v : hoist(v, io, false)]),
    );
    return !top && typeof out.title === "string" && out.type === "object"
      ? register(out.title, io, out)
      : out;
  };
  // zod's own `$defs` (a recursive model's) are lifted as they are, so their refs still resolve.
  const lifted: Json = {};
  const ref = (schema: Schema, io: Io, fallback: string): Json => {
    const { $defs: inner = {}, ...generated } = toJsonSchema(schema, io);
    for (const [key, value] of Object.entries(inner as Json)) {
      if (key in lifted && JSON.stringify(lifted[key]) !== JSON.stringify(value))
        throw collision(key);
      lifted[key] = value;
    }
    const body = hoist(generated, io, true) as Json;
    const name = typeof body.title === "string" ? body.title : fallback;
    const seen = owners.get(name) ?? new Set();
    if (seen.add(schema).size > 1) throw collision(name);
    owners.set(name, seen);
    return register(name, io, body);
  };
  const pascal = (s: string) => s.replace(/(^|_)(\w)/g, (_m, _p, c: string) => c.toUpperCase());
  const doc: Json = {
    agent: def.name,
    source,
    init_data:
      def.initData === undefined
        ? null
        : {
            data: ref(def.initData.schema, "input", `${def.name}InitData`),
            required: def.initData.required,
          },
    handlers: Object.fromEntries(
      Object.entries(def.handlers)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([name, h]) => [
          name,
          {
            description: h.description,
            mid_turn: h.midTurn,
            input: ref(h.input, "input", `${pascal(name)}Input`),
            output: ref(h.output, "output", `${pascal(name)}Output`),
          },
        ]),
    ),
    states: Object.fromEntries(
      Object.entries(def.states ?? {}).map(([id, decl]) => [
        id,
        ref(decl.schema, "output", pascal(id)),
      ]),
    ),
  };
  const split = new Set(
    [...found]
      .filter(
        ([, m]) => m.input && m.output && JSON.stringify(m.input) !== JSON.stringify(m.output),
      )
      .map(([n]) => n),
  );
  const defs: Json = { ...lifted };
  for (const [name, { input, output }] of found) {
    if (split.has(name))
      Object.assign(defs, { [`${name}-Input`]: input, [`${name}-Output`]: output });
    else defs[name] = input ?? output;
  }
  doc.$defs = Object.fromEntries(
    Object.entries(defs).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
  );
  const resolve = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(resolve);
    if (!isObject(node)) return node;
    if (typeof node.$ref === "string" && node.$ref.startsWith(PENDING)) {
      const [name = "", io] = node.$ref.slice(PENDING.length).split("\0");
      return {
        ...node,
        $ref: `#/$defs/${split.has(name) ? `${name}-${io === "input" ? "Input" : "Output"}` : name}`,
      };
    }
    return Object.fromEntries(
      Object.entries(node).map(([k, v]) => [k, LITERAL_KEYS.has(k) ? v : resolve(v)]),
    );
  };
  return resolve(doc) as Json;
}
