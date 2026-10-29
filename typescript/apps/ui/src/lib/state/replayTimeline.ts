/**
 * One entry per frame, tagged with which agent published it and which parent
 * turn encloses it. Every other projection in the console is built from this,
 * so it is the one that decides whether a subagent's events read as its own or
 * as the root's.
 */
import type { AgentSseFrame, Session } from "$lib/api/types";
import { isRootAgentEvent } from "$lib/state/agentIdentity";
import type { StepTimelineFrame } from "./stepTimeline";

export type ReplayTimelineRole = "parent" | "subagent";

export interface ReplayTimelineEntry extends StepTimelineFrame {
  workflowId: string;
  role: ReplayTimelineRole;
  frame: AgentSseFrame;
}

/**
 * The three fields this projection reads off an observed subagent.
 *
 * Declared here rather than importing ObservedSubagent, so the dependency runs
 * one way — the same shape flowProjection.ts uses for AgentGraphSource.
 */
export interface ReplayTimelineSubagent {
  subagentId: string;
  workflowId: string;
  label: string;
}

/**
 * `agentLabel` is passed already resolved, and that is load-bearing rather than
 * cosmetic: as a method this read `this.runInfo.agentLabel` once per frame, and
 * that getter summarizes cost across every frame to name the models, so the
 * projection was quadratic in the session length (8x the frames measured 25x
 * the cost). The label cannot vary between frames of one rebuild, so hoisting
 * it to the call is both what a parameter forces and what it always should
 * have been.
 */
export function buildReplayTimeline(
  session: Session | null,
  frames: readonly AgentSseFrame[],
  subagents: readonly ReplayTimelineSubagent[],
  agentLabel: string
): ReplayTimelineEntry[] {
  return replayTimelineBuilder()(session, frames, subagents, agentLabel);
}

/**
 * The replay timeline, folded forward and kept between calls.
 *
 * Frames only ever grow within a session, so while the last frame folded is still
 * at its index, only the new ones are added. A different session, agent label or
 * set of observed subagents (which relabels earlier entries) starts over, and so
 * does anything that is not an extension. Entries already built keep their identity,
 * which is what lets the projections downstream fold forward too.
 */
export function replayTimelineBuilder(): (
  session: Session | null,
  frames: readonly AgentSseFrame[],
  subagents: readonly ReplayTimelineSubagent[],
  agentLabel: string
) => ReplayTimelineEntry[] {
  let scope = "";
  let observedBySubagentId = new Map<string, ReplayTimelineSubagent>();
  let parentTurnBySubagentTurn = new Map<string, number>();
  let timeline: ReplayTimelineEntry[] = [];

  return (session, frames, subagents, agentLabel) => {
    if (!session) return [];
    const nextScope = [session.workflow_id, agentLabel, ...subagents.map((a) => `${a.subagentId}\0${a.workflowId}\0${a.label}`).sort()].join("\n");
    const seen = timeline.length;
    if (nextScope !== scope || frames.length < seen || (seen > 0 && frames[seen - 1] !== timeline[seen - 1]!.frame)) {
      scope = nextScope;
      observedBySubagentId = new Map(subagents.map((agent) => [agent.subagentId, agent]));
      parentTurnBySubagentTurn = new Map();
      timeline = [];
    }
    for (let i = timeline.length; i < frames.length; i += 1) {
      timeline.push(entryFor(session, frames[i]!, agentLabel, observedBySubagentId, parentTurnBySubagentTurn));
    }

    /* `?.` because `import.meta.env` is Vite's, and this function is also called
       from a plain-node self-check. A dev-only assertion must not be the reason a
       consumer outside Vite throws; absent an env, it simply stays off. */
    if (import.meta.env?.DEV && timeline.length !== frames.length) {
      console.error(
        `replayTimeline emitted ${timeline.length} entries for ${frames.length} frames. ` +
          "get total() returns frames.length to avoid rebuilding this projection on every " +
          "appended frame, and that shortcut is now wrong."
      );
    }
    return timeline.slice();
  };
}

function entryFor(
  session: Session,
  frame: AgentSseFrame,
  agentLabel: string,
  observedBySubagentId: Map<string, ReplayTimelineSubagent>,
  parentTurnBySubagentTurn: Map<string, number>
): ReplayTimelineEntry {
  if (!("type" in frame.data)) {
    return { workflowId: session.workflow_id, role: "parent", label: agentLabel, frame };
  }

  const observedSubagent = observedBySubagentId.get(frame.data.agent_id);
  const parentTurnNumber =
    observedSubagent == null
      ? undefined
      : parentTurnBySubagentTurn.get(`${frame.data.agent_id}:${frame.data.turn_number}`);
  /* Asked of the `agent_id`, not of `observedSubagent`: a subagent is only
     observed once its `subagent_started` has arrived, and that frame is
     published on the PARENT's log, so a stream opened past its offset never
     carries it. Inferring rootness from that absence attributed every
     unannounced child's frames to the root. */
  const role: ReplayTimelineRole = isRootAgentEvent(frame.data) ? "parent" : "subagent";

  if (frame.event === "subagent_message_sent") {
    const enclosingParentTurn =
      role === "subagent" && parentTurnNumber != null ? parentTurnNumber : frame.data.turn_number;
    parentTurnBySubagentTurn.set(`${frame.data.subagent_id}:${frame.data.subagent_turn}`, enclosingParentTurn);
  }
  return {
    workflowId: observedSubagent?.workflowId ?? session.workflow_id,
    role,
    label: observedSubagent?.label ?? agentLabel,
    parentTurnNumber,
    frame
  };
}
