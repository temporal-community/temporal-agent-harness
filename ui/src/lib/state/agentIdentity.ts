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
 * (`AgentId` in `agent_protocol/agent_interface.py`, `AGENT_ID_LENGTH = 6`).
 * A descendant is therefore exactly an id carrying that trailing fresh segment
 * — what the event protocol means by "a root-only consumer filters to the root
 * `agent_id`".
 *
 * Asked as "does it END in a fresh segment" rather than "does it contain a `-`
 * anywhere", because a `-` alone is not the thing that makes an id a child: a
 * label-style root id like `qa-root` carries one and is still the root of its
 * tree, and reading it as a child drops every one of its frames — which
 * collapses `turnMarkers` to nothing and leaves the replay bar with no chapters
 * at all. Matching the documented segment shape keeps the whole chain right:
 * `de539b` is a root, `de539b-093b70` its child, and `de539b-093b70-a1b2c3` no
 * more the root than its parent is.
 *
 * Worth asking this way round rather than "is this a subagent I have heard of":
 * a subagent is only KNOWN once its `subagent_started` has arrived, and that is
 * a different frame, published on the PARENT's log. A stream opened past that
 * offset never carries it, so the absence reads as proof of rootness and the
 * child's events get attributed to the root.
 */
/** The one fresh `AGENT_ID_LENGTH`-wide hex segment a subagent's id ends in. */
const SUBAGENT_SEGMENT = /-[0-9a-f]{6}$/;

export function isRootAgentEvent(data: { agent_id: string }): boolean {
  return !SUBAGENT_SEGMENT.test(data.agent_id);
}
