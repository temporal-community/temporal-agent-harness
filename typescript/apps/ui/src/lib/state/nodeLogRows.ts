/**
 * Which Logs row a State flow card stands for — what "view in logs" jumps to.
 *
 * A card is a projection of frames (flowProjection), and the Logs pane lists the
 * same frames as rows (replayLog), so the link between them is the frame, not
 * anything either surface invents. Cards are scoped `workflowId::localId` by the
 * tree graph and rows carry the same `workflowId`, so a card only ever matches rows
 * from its own runtime: a subagent's Input is its own message, not the parent's.
 *
 * Several rows usually stand for one card (a tool is requested, starts, ends), and
 * the card shows the latest of them, so the latest is what this returns. Rows are
 * already scoped to the replay cursor, the same moment the graph is drawn at, so
 * "latest" is never a row from the future of what the card is showing.
 */
import type { AgentEventType } from "$lib/api/types";
import type { ReplayLogRow } from "./replayLog";

type RowEvent = AgentEventType | "stream_error";

const INPUT_EVENTS = new Set<RowEvent>(["message_accepted", "turn_started"]);
const MODEL_EVENTS = new Set<RowEvent>(["model_interaction_started", "model_interaction_ended"]);
const REASONING_EVENTS = new Set<RowEvent>(["thought_summary"]);
const OUTPUT_EVENTS = new Set<RowEvent>([
  "reply_delta",
  "text_annotation",
  "message_handler_end",
  "message_handler_error"
]);
const SUBAGENT_EVENTS = new Set<RowEvent>([
  "subagent_started",
  "subagent_message_sent",
  "subagent_reply_received",
  "subagent_stopped",
  "subagent_stream_unavailable"
]);

/** Splits a tree-graph node id into the runtime it belongs to and its local id. */
function splitNodeId(nodeId: string): { workflowId: string | null; localId: string } {
  const at = nodeId.lastIndexOf("::");
  return at === -1
    ? { workflowId: null, localId: nodeId }
    : { workflowId: nodeId.slice(0, at), localId: nodeId.slice(at + 2) };
}

function matcherFor(localId: string): ((row: ReplayLogRow) => boolean) | null {
  if (localId === "input") return (row) => INPUT_EVENTS.has(row.event);
  if (localId === "model") return (row) => MODEL_EVENTS.has(row.event);
  if (localId === "reasoning") return (row) => REASONING_EVENTS.has(row.event);
  if (localId === "output") return (row) => OUTPUT_EVENTS.has(row.event);
  if (localId === "tool-container" || localId === "subagent") {
    return (row) => SUBAGENT_EVENTS.has(row.event);
  }
  if (localId.startsWith("tool:")) {
    const toolId = localId.slice("tool:".length);
    return (row) => row.toolId === toolId;
  }
  if (localId.startsWith("approval:")) {
    const evaluationId = localId.slice("approval:".length);
    return (row) => row.evaluationId === evaluationId;
  }
  /* A joined message handler's own cards (`input:<message_id>` and so on): the same events
     as the opener's card of that kind, held to that message. */
  const laneScoped = /^(input|output|model|reasoning):(.+)$/.exec(localId);
  if (laneScoped) {
    const events = { input: INPUT_EVENTS, output: OUTPUT_EVENTS, model: MODEL_EVENTS, reasoning: REASONING_EVENTS }[
      laneScoped[1] as "input" | "output" | "model" | "reasoning"
    ];
    const messageId = laneScoped[2];
    return (row) => row.messageId === messageId && events.has(row.event);
  }
  return null;
}

/**
 * The latest Logs row a card stands for, or null when the card stands for none —
 * a runtime frame, or a card whose frames have not reached the log yet.
 */
export function latestLogRowForNode(
  nodeId: string,
  rows: readonly ReplayLogRow[]
): ReplayLogRow | null {
  const { workflowId, localId } = splitNodeId(nodeId);
  const matches = matcherFor(localId);
  if (!matches) return null;
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index];
    /* An unscoped id (a single-agent graph) has no runtime to hold the row to. */
    if (workflowId != null && row.workflowId !== workflowId) continue;
    if (matches(row)) return row;
  }
  return null;
}
