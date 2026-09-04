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
 *
 * The one thing a remembered session loses to is being both contentless AND
 * superseded. A probe run that leaves eight untouched sessions behind parks a
 * browser on one of them forever, and it opens on nothing every time. Emptiness
 * alone is not enough to leave: a session nobody has spoken to but that nothing
 * has been created since is the one the operator just made on purpose.
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
  if (stored && !(hasNothingToShow(stored) && isSuperseded(sessions, stored))) return stored;

  const live = [...sessions]
    .reverse()
    .find((item) => item.agent_workflow_type === workflowType && !item.closed);
  return live ?? null;
}

/**
 * Whether nobody has ever spoken to this session, which is as near to "it has no
 * frames" as the session list can answer for free: the preview is the memo the
 * agent upserts on its first message, and it rides on the describe the list
 * already makes. An exact event count is a workflow query per session
 * (`_published_event_count`, web/app.py) and must not be spent on the existence
 * poll — that path is a manager query only.
 */
function hasNothingToShow(session: Session): boolean {
  return session.initial_user_message == null;
}

/**
 * Stale, in one place and one sense: something newer exists. Read off
 * `created_at`, which is the field the session sidebar already sorts by, rather
 * than list position — discovered sessions are appended to the list whatever
 * their age, so position is not age.
 */
function isSuperseded(sessions: Session[], session: Session): boolean {
  return sessions.some((item) => item.created_at > session.created_at);
}
