// ABOUTME: Pins what the header chip and a folded chat say the agent is waiting on. It counted
// only tool approvals, so an agent blocked on an ask_user callback looked idle whenever the chat
// was folded, closed or scrolled.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { countPendingRequests, pendingRequestLabel } from "./pendingRequests.ts";

const row = (event, toolId) => ({ event, toolId });

describe("pending requests", () => {
  it("counts approvals and callbacks that are still unresolved", () => {
    const rows = [
      row("tool_approval_requested", "a1"),
      row("tool_approval_requested", "a2"),
      row("tool_approval_resolved", "a2"),
      row("callback_requested", "c1"),
      row("callback_requested", "c2"),
      row("callback_resolved", "c2"),
      row("callback_requested", undefined)
    ];
    assert.deepEqual(countPendingRequests(rows), { approvals: 1, responses: 1 });
  });

  it("does not let one kind's resolution clear the other kind with the same tool id", () => {
    const rows = [row("callback_requested", "t1"), row("tool_approval_resolved", "t1")];
    assert.deepEqual(countPendingRequests(rows), { approvals: 0, responses: 1 });
  });

  it("names what is needed, and nothing when nothing is", () => {
    assert.equal(pendingRequestLabel({ approvals: 0, responses: 1 }), "1 response needed");
    assert.equal(pendingRequestLabel({ approvals: 1, responses: 0 }), "1 approval needed");
    assert.equal(
      pendingRequestLabel({ approvals: 12, responses: 12 }),
      "12 approvals, 12 responses needed"
    );
    assert.equal(pendingRequestLabel({ approvals: 0, responses: 0 }), null);
  });
});
