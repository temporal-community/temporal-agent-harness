/**
 * Applying the RFC 6902 ops that arrive on an agent's state stream.
 *
 * Deliberately not a library. The harness derives these ops itself and its own
 * contract says it emits only `add`, `replace` and `remove` — never `move`,
 * `copy` or `test` — so the whole of what can arrive is below, and a dependency
 * would be more code than this, not less.
 *
 * Two things a stock applier does not do, both of which a state view needs:
 *
 *   - It reports where each op *landed*. `/todos/-` is an append, and a panel
 *     cannot mark a row it has no index for, so every applied op carries the
 *     resolved pointer alongside the one that arrived.
 *   - It never throws. A malformed or unapplicable op is a fact about the run
 *     worth showing, not a reason for the pane that was showing the run to go
 *     blank, so a failure comes back as a sentence in `error`.
 */
import type { JsonPatchOp, JsonValue } from "./frames.ts";

export interface AppliedOp {
  op: JsonPatchOp["op"];
  /** The pointer as it arrived — `/todos/-` and all. */
  path: string;
  /** Where it landed: `/todos/-` resolved against the array it appended to. */
  resolved: string;
  /** Absent on `remove`, which carries none. */
  value?: JsonValue;
  /** What stood at `resolved` beforehand. Absent where nothing did. */
  before?: JsonValue;
}

export interface PatchResult {
  doc: JsonValue;
  applied: AppliedOp[];
  /**
   * The first op that could not be applied, named — and the rest are then not
   * attempted, because a patch is an ordered whole. Applying the tail of one
   * whose head failed builds a document the agent was never in, which is worse
   * than a document that stops being updated and says so.
   */
  error: string | null;
}

export function escapeToken(token: string): string {
  return String(token).replace(/~/g, "~0").replace(/\//g, "~1");
}

export function unescapeToken(token: string): string {
  /* `~1` first, then `~0`: the other order turns an escaped `~1` back into a
     slash. RFC 6901 §4 spells out this exact ordering. */
  return token.replace(/~1/g, "/").replace(/~0/g, "~");
}

/** `""` is the whole document, so it parses to no tokens rather than to one empty one. */
export function parsePointer(pointer: string): string[] {
  if (pointer === "") return [];
  return pointer.split("/").slice(1).map(unescapeToken);
}

export function buildPointer(tokens: Array<string | number>): string {
  return tokens.map((token) => `/${escapeToken(String(token))}`).join("");
}

function isContainer(node: unknown): node is JsonValue[] | Record<string, JsonValue> {
  return node !== null && typeof node === "object";
}

/**
 * One step down a pointer, through the document's own keys only. A plain `node[token]`
 * also finds inherited ones, and `/__proto__/x` or `/constructor/prototype/x` would then
 * walk off the document into `Object.prototype`.
 */
function child(node: JsonValue[] | Record<string, JsonValue>, token: string): JsonValue | undefined {
  if (Array.isArray(node)) return node[Number(token)];
  return Object.hasOwn(node, token) ? node[token] : undefined;
}

/** The value at `pointer`, or undefined if nothing is there. */
export function valueAt(doc: JsonValue, pointer: string): JsonValue | undefined {
  let node: JsonValue | undefined = doc;
  for (const token of parsePointer(pointer)) {
    if (!isContainer(node)) return undefined;
    node = child(node, token);
  }
  return node;
}

/**
 * The value an op inserts, copied away from the op that carried it.
 *
 * Inserting `op.value` itself would put an object the FRAME still owns into the
 * document, and a later op in the same patch — `replace /todos/0/done` after
 * `add /todos/-` — would then write through into the frame log.
 */
function inserted(value: JsonValue | undefined): JsonValue {
  if (value === undefined || value === null) return null;
  if (typeof value !== "object") return value;
  /* JSON, not `structuredClone`: a frame held in a reactive store may be a Proxy, which
     `structuredClone` refuses. Ops are JSON by construction, so the round-trip loses nothing. */
  return JSON.parse(JSON.stringify(value)) as JsonValue;
}

/**
 * Apply `ops` in order.
 *
 * MUTATES `doc` in place, so hand it one you own: a caller clones the snapshot once and
 * folds every later patch into that clone, which keeps applying O(ops) rather than
 * O(ops x document).
 */
export function applyOps(doc: JsonValue, ops: readonly JsonPatchOp[]): PatchResult {
  let next = doc;
  const applied: AppliedOp[] = [];

  for (const op of ops) {
    if (op == null || typeof op.path !== "string") {
      return { doc: next, applied, error: "a patch op arrived with no path" };
    }
    if (op.op !== "add" && op.op !== "replace" && op.op !== "remove") {
      /* Not a gap to fill: the harness promises three kinds, so a fourth means
         the two ends disagree about the contract, and guessing at `move` here
         would hide that. */
      return {
        doc: next,
        applied,
        error: `unsupported op ${JSON.stringify(op.op)} at ${op.path || "/"}`
      };
    }

    const tokens = parsePointer(op.path);

    /* The whole document, which is what `ref.set()` emits. */
    if (tokens.length === 0) {
      applied.push({ op: op.op, path: op.path, resolved: "", before: next, value: op.value });
      next = inserted(op.value);
      continue;
    }

    const last = tokens.pop() as string;
    let parent: JsonValue | undefined = next;
    for (const token of tokens) {
      if (!isContainer(parent)) break;
      parent = child(parent, token);
    }
    if (!isContainer(parent)) {
      return {
        doc: next,
        applied,
        error: `no container at ${buildPointer(tokens) || "/"} for ${op.op} ${op.path}`
      };
    }

    let before: JsonValue | undefined;
    let landed: string | number = last;

    if (Array.isArray(parent)) {
      /* `-` is RFC 6901's "one past the end", which is only ever an append. */
      const index = last === "-" ? parent.length : Number(last);
      const limit = op.op === "add" ? parent.length : parent.length - 1;
      if (!Number.isInteger(index) || index < 0 || index > limit) {
        return {
          doc: next,
          applied,
          error: `${op.op} ${op.path} is outside an array of ${parent.length}`
        };
      }
      landed = index;
      if (op.op === "add") {
        parent.splice(index, 0, inserted(op.value));
      } else if (op.op === "replace") {
        before = parent[index];
        parent[index] = inserted(op.value);
      } else {
        before = parent[index];
        parent.splice(index, 1);
      }
    } else {
      /* Assigning `__proto__` re-parents the object rather than setting a key on it. */
      if (last === "__proto__") {
        return { doc: next, applied, error: `${op.op} ${op.path} names __proto__` };
      }
      before = child(parent, last);
      if (op.op === "remove") {
        if (!Object.hasOwn(parent, last)) {
          return { doc: next, applied, error: `remove ${op.path} — no such key` };
        }
        delete parent[last];
      } else {
        parent[last] = inserted(op.value);
      }
    }

    applied.push({
      op: op.op,
      path: op.path,
      resolved: buildPointer([...tokens, landed]),
      ...(op.op === "remove" ? {} : { value: op.value }),
      ...(before === undefined ? {} : { before })
    });
  }

  return { doc: next, applied, error: null };
}
