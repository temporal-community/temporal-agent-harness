// Observable agent state. Declare it on an agent with `states: { board: state(BoardSchema) }`;
// the key is its `state_id`. Every committed `ref.mutate((d) => { ... })` is published on the
// event stream as RFC 6902 ops, so a consumer that applies one snapshot and then every patch in
// version order holds the exact value without polling. Immer records the ops (it emits only
// add/replace/remove, which is all the protocol allows); zod re-validates every commit.

import {
  applyPatches,
  castDraft,
  enablePatches,
  freeze,
  produceWithPatches,
  type Draft,
  type Patch,
} from "immer";
import type { z } from "zod";

enablePatches();

export class HarnessStateError extends Error {
  override name = "HarnessStateError";
}

export interface StateSnapshotEvent {
  type: "state_snapshot";
  state_id: string;
  version: number;
  value: Record<string, unknown>;
}
export interface StatePatchEvent {
  type: "state_patch";
  state_id: string;
  version: number;
  ops: Record<string, unknown>[];
}
export type StateEvent = StateSnapshotEvent | StatePatchEvent;

export interface StateDecl<S extends z.ZodType<Record<string, unknown>>> {
  readonly schema: S;
  /** Receives the agent's validated init data (null/undefined when it has none). */
  readonly initial: (data: unknown) => z.input<S>;
}

/** Declare a piece of observable state. Without `initial`, the schema's defaults must cover it. */
export function state<S extends z.ZodType<Record<string, unknown>>>(
  schema: S,
  initial: (data: unknown) => z.input<S> = () => ({}) as z.input<S>,
): StateDecl<S> {
  return { schema, initial };
}

// Python's JSON parser rejects lone surrogates and nesting past about 200 levels, and a JSON number
// past 2^53 has already been rounded by the time JavaScript sees it.
const MAX_JSON_DEPTH = 200;

/** Why a Python peer can't read `value` back as sent, if it can't. */
export function portabilityProblem(value: unknown, depth = 0): string | undefined {
  if (typeof value === "string")
    return value.isWellFormed() ? undefined : "has a string with a lone surrogate";
  if (typeof value === "number") {
    // From 1e21 up JSON writes numbers in exponent form, which every peer reads as a float.
    if (Number.isInteger(value) && !Number.isSafeInteger(value) && Math.abs(value) < 1e21) {
      return `has the integer ${value}, past 2^53, which a JSON number can't carry exactly; send it as a string`;
    }
    return undefined;
  }
  if (typeof value !== "object" || value === null) return undefined;
  if (depth >= MAX_JSON_DEPTH) return `nests deeper than ${MAX_JSON_DEPTH} levels`;
  for (const [key, item] of Object.entries(value)) {
    if (!key.isWellFormed()) return "has a key with a lone surrogate";
    const problem = portabilityProblem(item, depth + 1);
    if (problem !== undefined) return problem;
  }
  return undefined;
}

// structuredClone is not available in the workflow isolate; state values are JSON by contract.
const jsonClone = <V>(value: V): V => JSON.parse(JSON.stringify(value)) as V;

const escape = (segment: string | number): string =>
  String(segment).replaceAll("~", "~0").replaceAll("/", "~1");

function toOp(patch: Patch): Record<string, unknown> {
  const op: Record<string, unknown> = {
    op: patch.op,
    path: patch.path.map((s) => `/${escape(s)}`).join(""),
  };
  if (patch.op !== "remove") op.value = jsonClone(patch.value);
  return op;
}

// Python writes an append as "/-"; an add at an array's current length means the same thing.
function toOps(base: unknown, patches: Patch[]): Record<string, unknown>[] {
  let value = base;
  return patches.flatMap((patch) => {
    const op = toOp(patch);
    const index = patch.path.at(-1);
    const parent = patch.path
      .slice(0, -1)
      .reduce<unknown>((v, key) => (v as Record<string | number, unknown>)[key], value);
    // immer checks `key in base`, so deleting an absent key named like an inherited member
    // (`toString`) comes out as a remove of a path that does not exist.
    if (patch.op === "remove" && !Array.isArray(parent) && !Object.hasOwn(parent as object, index!))
      return [];
    if (patch.op === "add" && Array.isArray(parent) && index === parent.length)
      op.path = `${(op.path as string).slice(0, -String(index).length)}-`;
    value = applyPatches(value as object, [patch]);
    return [op];
  });
}

export class StateRef<T extends Record<string, unknown>> {
  #current: T;
  #version = 0;
  #mutating = false;
  readonly #schema: z.ZodType<T>;
  readonly #publish: (event: StateEvent) => void;
  readonly stateId: string;

  constructor(
    stateId: string,
    decl: StateDecl<z.ZodType<T>>,
    publish: (event: StateEvent) => void,
    data?: unknown,
  ) {
    this.stateId = stateId;
    this.#schema = decl.schema;
    this.#current = freeze(this.#schema.parse(decl.initial(data)), true);
    this.#publish = publish;
  }

  /** The committed value. Deeply frozen: change it only through `mutate` or `set`. */
  get current(): T {
    return this.#current;
  }

  get version(): number {
    return this.#version;
  }

  snapshot(): StateSnapshotEvent {
    return {
      type: "state_snapshot",
      state_id: this.stateId,
      version: this.#version,
      value: jsonClone(this.#current),
    };
  }

  /** Apply `recipe` to a draft and publish what it changed. Synchronous by contract. */
  mutate(recipe: (draft: Draft<T>) => void): void {
    if (this.#mutating)
      throw new HarnessStateError(`state ${this.stateId} already has an open mutate() block`);
    this.#mutating = true;
    try {
      const [next, patches] = produceWithPatches(this.#current, (draft) => {
        const returned: unknown = recipe(draft);
        if (returned instanceof Promise) {
          throw new HarnessStateError(
            `mutate() on state ${this.stateId} must be synchronous; it must not span an await`,
          );
        }
      });
      const ops = toOps(this.#current, patches);
      if (ops.length === 0) return;
      this.#commit(next, ops);
    } finally {
      this.#mutating = false;
    }
  }

  /** Replace the whole value (one `replace` op at the root). */
  set(value: z.input<z.ZodType<T>>): void {
    if (this.#mutating)
      throw new HarnessStateError(
        `state ${this.stateId} cannot be set() inside its open mutate() block`,
      );
    const next = this.#schema.parse(value);
    this.#commit(next, [{ op: "replace", path: "", value: jsonClone(next) }]);
  }

  // Validates but commits `next` itself, so the published ops describe exactly the committed value.
  #commit(next: T, ops: Record<string, unknown>[]): void {
    const validated = this.#schema.safeParse(next);
    if (!validated.success) {
      throw new HarnessStateError(
        `state ${this.stateId}: the change does not fit its schema: ${validated.error.message}`,
      );
    }
    const problem = portabilityProblem(next);
    if (problem !== undefined)
      throw new HarnessStateError(`state ${this.stateId}: the change ${problem}`);
    this.#current = freeze(castDraft(next) as T, true);
    this.#version += 1;
    this.#publish({ type: "state_patch", state_id: this.stateId, version: this.#version, ops });
  }
}
