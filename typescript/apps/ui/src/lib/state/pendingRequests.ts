import type { ReplayLogRow } from "./replayLog";

export interface PendingRequestCounts {
  approvals: number;
  responses: number;
}

/** Tool approvals and callbacks the agent is blocked on: requested and not yet resolved. */
export function countPendingRequests(rows: ReplayLogRow[]): PendingRequestCounts {
  const resolved = new Set<string>();
  for (const row of rows) {
    if (row.toolId && (row.event === "tool_approval_resolved" || row.event === "callback_resolved")) {
      resolved.add(`${row.event}:${row.toolId}`);
    }
  }
  const open = (requested: string, resolution: string) =>
    rows.filter(
      (row) =>
        row.event === requested && row.toolId != null && !resolved.has(`${resolution}:${row.toolId}`)
    ).length;
  return {
    approvals: open("tool_approval_requested", "tool_approval_resolved"),
    responses: open("callback_requested", "callback_resolved")
  };
}

/** What the reader owes the agent, e.g. "2 approvals, 1 response needed"; null when nothing. */
export function pendingRequestLabel({ approvals, responses }: PendingRequestCounts): string | null {
  const parts = [
    approvals > 0 ? `${approvals} approval${approvals === 1 ? "" : "s"}` : null,
    responses > 0 ? `${responses} response${responses === 1 ? "" : "s"}` : null
  ].filter(Boolean);
  return parts.length > 0 ? `${parts.join(", ")} needed` : null;
}
