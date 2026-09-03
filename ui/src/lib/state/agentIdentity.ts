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
 * anywhere", because a `-` alone is not the thing that makes an id a child, and
 * matching the documented segment shape is what keeps the whole chain right:
 * `de539b` is a root, `de539b-093b70` its child, and `de539b-093b70-a1b2c3` no
 * more the root than its parent is.
 *
 * Which cuts both ways, and no predicate can rescue an id that ignores the
 * shape: a label-style `qa-root-search` ends in six characters that are not six
 * hex digits, so it reads as a root and its own turn 1 lands on the replay bar
 * beside the real root's — the each_key_duplicate the lane throws when two
 * chapters share a turn number. It stands to `qa-root` exactly as `qa-root`
 * stands to `qa`, so any rule calling one a root must call the other one too.
 * Fixtures conform to `AgentId` instead; check-turn-markers.mjs asserts it.
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
