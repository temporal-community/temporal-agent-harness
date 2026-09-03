/**
 * Which agent in a session's subagent tree published an event.
 *
 * Kept as a plain module, not a method on the controller, so it can be checked
 * without a browser or a Svelte compile step: node ui/scripts/check-turn-markers.mjs.
 */

/**
 * Whether the agent that published an event is the session's ROOT agent.
 *
 * `agent_id` is tree-unique by construction: one `AGENT_ID_LENGTH`-wide hex
 * segment for a root, and a subagent's is its parent's id plus one fresh
 * segment joined by `-`, which pydantic enforces on the way into a workflow
 * (`AgentId` in `agent_protocol/agent_interface.py`). A single segment is
 * therefore the root and a `-` is a descendant — what the event protocol means
 * by "a root-only consumer filters to the root `agent_id`".
 *
 * Worth asking this way round rather than "is this a subagent I have heard of":
 * a subagent is only KNOWN once its `subagent_started` has arrived, and that is
 * a different frame, published on the PARENT's log. A stream opened past that
 * offset never carries it, so the absence reads as proof of rootness and the
 * child's events get attributed to the root.
 */
export function isRootAgentEvent(data: { agent_id: string }): boolean {
  return !data.agent_id.includes("-");
}
