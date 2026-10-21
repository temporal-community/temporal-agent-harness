import assert from "node:assert/strict";
import { applyOps, type JsonPatchOp } from "@temporalio/agent-harness-client";
import { StateRef, state } from "@temporalio/agent-harness";
import type { z } from "zod";
import { PlanState } from "../../fixtures/workflows.ts";

export type Plan = z.output<typeof PlanState>;
/** What a StateRef publishes: its snapshot, or a patch per commit. */
export type StateEvent = Parameters<ConstructorParameters<typeof StateRef<Plan>>[2]>[0];
export type StateSnapshotEvent = Extract<StateEvent, { type: "state_snapshot" }>;

export function harness(initial?: () => z.input<typeof PlanState>): {
  ref: StateRef<Plan>;
  published: StateEvent[];
} {
  const published: StateEvent[] = [];
  const ref = new StateRef<Plan>("plan", state(PlanState, initial), (e) => published.push(e));
  return { ref, published };
}

/** Fold a snapshot and its patches the way a consumer does. */
export function replay(
  snapshot: Record<string, unknown>,
  patches: { ops: Record<string, unknown>[] }[],
) {
  let doc = structuredClone(snapshot) as never;
  for (const p of patches) {
    const result = applyOps(doc, p.ops as unknown as JsonPatchOp[]);
    assert.equal(result.error, null);
    doc = result.doc as never;
  }
  return doc;
}
