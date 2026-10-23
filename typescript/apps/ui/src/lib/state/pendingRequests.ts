import type { ReplayLogRow } from "./replayLog";

export interface PendingRequestCounts {
  approvals: number;
  responses: number;
}

/** A request's identity: the agent that asked plus its tool id, since tool ids are only unique
 * within one agent. Rows without a workflow id are the session parent's. */
export function requestKey(row: ReplayLogRow, parentWorkflowId: string): string | null {
  return row.toolId ? `${row.workflowId ?? parentWorkflowId}:${row.toolId}` : null;
}

/** Tool approvals and callbacks the agent is blocked on: requested and not yet resolved. */
export function countPendingRequests(
  rows: ReplayLogRow[],
  parentWorkflowId: string
): PendingRequestCounts {
  const open = (requested: string, resolution: string) => {
    const resolved = new Set<string | null>();
    for (const row of rows) if (row.event === resolution) resolved.add(requestKey(row, parentWorkflowId));
    return rows.filter((row) => {
      if (row.event !== requested) return false;
      const key = requestKey(row, parentWorkflowId);
      return key != null && !resolved.has(key);
    }).length;
  };
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
