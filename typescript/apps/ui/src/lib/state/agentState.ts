/**
 * Observable agent state, folded out of the event stream.
 *
 * A workflow author declares state with one call — `runner.state("plan", ...)` —
 * and from then on the harness publishes an `AgentStateSnapshot` when it is
 * registered and an `AgentStatePatch` after every `mutate()` block that changed
 * something. Those ride `turn_events` with everything else, so this projection
 * is the console's half of the bargain: one snapshot, then ops in order, and the
 * document is exactly what the agent holds.
 *
 * Built from the frames up to the playhead, like every other projection here,
 * and that is the whole reason it is a projection rather than a subscription.
 * The reference UI for this feature applies each patch as it arrives and flashes
 * the path — which is correct while you are watching live and meaningless the
 * moment anyone scrubs, because an arrival is not a position. Here the document
 * is a function of the cursor: park on a `state_patch` and the panel shows the
 * state as of that commit, with that commit's own paths marked. Step back and
 * the marks move back with you.
 */
import type { AgentSseFrame, JsonValue } from "$lib/api/types";
import { applyOps, type AppliedOp } from "$lib/state/jsonPatch";

/** One path the commit at the cursor touched. `path` is resolved (`/todos/3`, never `/todos/-`). */
export interface StateChange {
  op: AppliedOp["op"];
  path: string;
  /** What stood there before, where anything did. */
  before?: JsonValue;
  value?: JsonValue;
}

export interface AgentStateDoc {
  /** Unique across the tree: two agents may each register a state called "plan". */
  key: string;
  stateId: string;
  workflowId: string;
  /** Which agent owns it — the same label the logs and the canvas use. */
  label: string;
  role: "parent" | "subagent";
  /** The harness's own version: 0 at registration, +1 per committed `mutate()`. */
  version: number;
  /** Null only when `problem` says why there is nothing to show. */
  value: JsonValue | null;
  /** What the commit at the cursor changed. Empty when the cursor is on the snapshot. */
  changed: StateChange[];
  /** Commits folded in so far, which is how many times the agent has touched this state. */
  commits: number;
  /** The turn the most recent change belonged to; 0 for registration and operator commands. */
  turnNumber: number;
  /** Why the document cannot be trusted, in a sentence, or null. */
  problem: string | null;
}

/**
 * Why a state can be streaming patches and still have nothing to render.
 *
 * The snapshot is published once, from `@workflow.init`, so it sits at the very
 * front of the agent's log. A stream attached from a later offset — reconnecting
 * mid-run, or opening a session that has been going for a while — never receives
 * it, and no amount of waiting produces another. The patches that follow are
 * perfectly good ops against a document this client does not have, and folding
 * them into an empty object would invent one.
 */
export const UNSYNCED_NOTE =
  "No snapshot on this stream. A state's snapshot is published once, when the agent " +
  "registers it, so a stream attached past that offset carries only the patches. " +
  "Reload the session from the start of its history to pick it up.";

/**
 * The three fields this projection reads off a timeline entry.
 *
 * Structurally what `buildReplayTimeline` produces, declared rather than imported
 * for the same reason `ReplayLogFrame` is: the dependency runs one way, and a
 * plain `AgentSseFrame[]` is still a legal argument.
 */
export interface AgentStateFrame {
  frame: AgentSseFrame;
  workflowId?: string;
  role?: "parent" | "subagent";
  label?: string;
}

function normalize(item: AgentSseFrame | AgentStateFrame): AgentStateFrame {
  return "frame" in item ? item : { frame: item, role: "parent" };
}

/**
 * A deep copy of a snapshot's value, so the fold owns the document it mutates.
 *
 * Taken once per state per rebuild, not once per patch: `applyOps` then walks
 * that one copy, which is what keeps scrubbing a long run linear in the ops
 * rather than quadratic in the document.
 *
 * A JSON round-trip and NOT `structuredClone`, which throws `DataCloneError` on a
 * Proxy — and every value reaching this function is inside `$state`, so in the
 * running app it is one. (The failure is worth remembering for how it presented:
 * the throw came out of a `$derived`, which meant it surfaced at whichever reader
 * touched it first — the pane header's description — so opening the pane did
 * nothing at all, with the stack trace blaming a line that only asked how many
 * states there were.) The round-trip is exact here rather than a compromise: this
 * is a `model_dump(mode="json")` document by construction, so there is nothing in
 * it JSON cannot carry.
 */
function ownCopy(value: unknown): JsonValue {
  return JSON.parse(JSON.stringify(value ?? null)) as JsonValue;
}

export function buildAgentStateDocs(
  input: Array<AgentSseFrame | AgentStateFrame>
): AgentStateDoc[] {
  const docs = new Map<string, AgentStateDoc>();

  for (const item of input) {
    const entry = normalize(item);
    const { frame } = entry;
    if (frame.event !== "state_snapshot" && frame.event !== "state_patch") continue;
    /* A client-side stream error has no `type` and no state on it. */
    if (!("type" in frame.data)) continue;

    const workflowId = entry.workflowId ?? frame.data.agent_id;
    const key = `${workflowId}:${frame.data.state_id}`;
    const existing = docs.get(key);

    if (frame.event === "state_snapshot") {
      /* Always a re-base, even over a document already being tracked: a second
         snapshot is the stream saying "start from here", and that is exactly how
         a reconnect resyncs. */
      docs.set(key, {
        key,
        stateId: frame.data.state_id,
        workflowId,
        label: entry.label ?? frame.data.agent_id,
        role: entry.role ?? "parent",
        version: frame.data.version,
        value: ownCopy(frame.data.value),
        changed: [],
        commits: 0,
        turnNumber: frame.data.turn_number,
        problem: null
      });
      continue;
    }

    if (!existing) {
      /* Patches with no snapshot to apply them to. Recorded rather than dropped,
         because "this agent has a state you cannot see" is the useful half of
         what is known, and silence reads as "this agent has no state". */
      docs.set(key, {
        key,
        stateId: frame.data.state_id,
        workflowId,
        label: entry.label ?? frame.data.agent_id,
        role: entry.role ?? "parent",
        version: frame.data.version,
        value: null,
        changed: [],
        commits: 1,
        turnNumber: frame.data.turn_number,
        problem: UNSYNCED_NOTE
      });
      continue;
    }

    existing.commits += 1;
    existing.version = frame.data.version;
    existing.turnNumber = frame.data.turn_number;
    /* Already unrenderable: keep counting the commits so the panel can say how
       far ahead the agent is, but there is no document to apply them to. */
    if (existing.value === null) continue;

    const result = applyOps(existing.value, frame.data.ops ?? []);
    existing.value = result.doc;
    existing.changed = result.applied.map((applied) => ({
      op: applied.op,
      path: applied.resolved,
      ...(applied.before === undefined ? {} : { before: applied.before }),
      ...(applied.value === undefined ? {} : { value: applied.value })
    }));
    if (result.error) {
      /* The document is now whatever the ops before the bad one left, which is a
         state the agent really passed through — so it is kept and labelled,
         rather than blanked. */
      existing.problem = `This patch could not be applied: ${result.error}.`;
    }
  }

  return [...docs.values()];
}
