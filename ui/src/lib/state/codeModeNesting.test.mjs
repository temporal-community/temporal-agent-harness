// ABOUTME: Pins the rule that puts a Code Mode script's calls under their host, which the graph and
// the chat's activity list both read. A child names no host, so the relation comes from order: the
// innermost host that has started and not yet ended. The cases that break it are a child whose
// tool_requested was never published, another agent's frames interleaved with the host's, and a
// scrub — a prefix of the log has to nest exactly as the full log does up to that row.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { codeModeHostsByRow } from "./codeModeNesting.ts";

let next = 0;
const row = (event, toolId, over = {}) => ({ id: `r${next++}`, event, toolId, ...over });
const SCRIPT = { script: "await search('x')" };

function hostWithChildren() {
  next = 0;
  return [
    row("turn_started", undefined),
    row("tool_requested", "host", { input: SCRIPT }),
    row("tool_start", "host", { input: SCRIPT }),
    row("tool_requested", "child-a", { input: { q: "x" } }),
    row("tool_start", "child-a"),
    row("tool_end", "child-a"),
    /* Published straight at tool_start: no tool_requested to hang the host on. */
    row("tool_start", "child-b"),
    /* Another agent's call, interleaved while the host is running. */
    row("tool_start", "other", { workflowId: "wf-sub" }),
    row("tool_end", "child-b"),
    row("tool_end", "host"),
    row("tool_requested", "after", { input: {} })
  ];
}

const hostsOf = (rows) =>
  Object.fromEntries([...codeModeHostsByRow(rows)].map(([id, host]) => [id, host]));

describe("codeModeHostsByRow", () => {
  it("nests a script's calls under the host, and nothing else", () => {
    assert.deepEqual(hostsOf(hostWithChildren()), {
      r3: "host",
      r4: "host",
      r5: "host",
      r6: "host",
      r8: "host"
    });
  });

  it("nests a prefix exactly as the full log does up to that row", () => {
    const rows = hostWithChildren();
    const full = hostsOf(rows);
    for (let end = 0; end <= rows.length; end += 1) {
      const expected = Object.fromEntries(
        Object.entries(full).filter(([id]) => Number(id.slice(1)) < end)
      );
      assert.deepEqual(hostsOf(rows.slice(0, end)), expected, `prefix of ${end} rows`);
    }
  });

  it("starts each turn with no host running", () => {
    next = 0;
    const rows = [
      row("turn_started", undefined),
      row("tool_start", "host", { input: SCRIPT }),
      row("turn_started", undefined),
      row("tool_start", "fresh")
    ];
    assert.deepEqual(hostsOf(rows), {});
  });
});
