/**
 * One entry per frame, tagged with which agent published it and which parent
 * turn encloses it. Every other projection in the console is built from this,
 * so it is the one that decides whether a subagent's events read as its own or
 * as the root's.
 */
import type { AgentSseFrame, Session } from "$lib/api/types";
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
  frames: AgentSseFrame[],
  subagents: ReplayTimelineSubagent[],
  agentLabel: string
): ReplayTimelineEntry[] {
  if (!session) return [];
  const observedBySubagentId = new Map(
    subagents.map((agent) => [agent.subagentId, agent])
  );
  const parentTurnBySubagentTurn = new Map<string, number>();
  const timeline: ReplayTimelineEntry[] = [];

  for (const frame of frames) {
    if (!("type" in frame.data)) {
      timeline.push({
        workflowId: session.workflow_id,
        role: "parent",
        label: agentLabel,
        frame
      });
      continue;
    }

    const observedSubagent = observedBySubagentId.get(frame.data.agent_id);
    const parentTurnNumber =
      observedSubagent == null
        ? undefined
        : parentTurnBySubagentTurn.get(
            `${frame.data.agent_id}:${frame.data.turn_number}`
          );
    const role: ReplayTimelineRole = observedSubagent == null ? "parent" : "subagent";
    timeline.push({
      workflowId: observedSubagent?.workflowId ?? session.workflow_id,
      role,
      label: observedSubagent?.label ?? agentLabel,
      parentTurnNumber,
      frame
    });

    if (frame.event === "subagent_message_sent") {
      const enclosingParentTurn =
        role === "subagent" && parentTurnNumber != null
          ? parentTurnNumber
          : frame.data.turn_number;
      parentTurnBySubagentTurn.set(
        `${frame.data.subagent_id}:${frame.data.subagent_turn}`,
        enclosingParentTurn
      );
    }
  }

  if (import.meta.env.DEV && timeline.length !== frames.length) {
    console.error(
      `replayTimeline emitted ${timeline.length} entries for ${frames.length} frames. ` +
        "get total() returns frames.length to avoid rebuilding this projection on every " +
        "appended frame, and that shortcut is now wrong."
    );
  }
  return timeline;
}
