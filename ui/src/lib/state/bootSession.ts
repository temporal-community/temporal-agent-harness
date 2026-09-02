import type { Session } from "$lib/api/types";

/**
 * Which session the console should open on boot, or null to start a fresh one.
 *
 * The two candidates are held to deliberately different standards. Reopening the
 * session the operator was last on is worth doing even once it has completed,
 * because its frames are still cached to scrub through; only a NOT_FOUND
 * workflow is useless, since it is gone from Temporal and /api/attach answers
 * 500. Picking a session nobody asked for is a different matter, and is only
 * useful while it is live: a closed one has no cache to fall back on and attach
 * is skipped for it, so it would open an empty console.
 */
export function chooseBootSession(
  sessions: Session[],
  storedSessionId: string | null,
  workflowType: string
): Session | null {
  const stored = storedSessionId
    ? sessions.find(
        (item) =>
          item.workflow_id === storedSessionId && item.execution_status !== "NOT_FOUND"
      )
    : undefined;
  if (stored) return stored;

  const live = [...sessions]
    .reverse()
    .find((item) => item.agent_workflow_type === workflowType && !item.closed);
  return live ?? null;
}
