// ABOUTME: Pins the Logs pane's tree and its signal rule: an error is never routine, a
// routine event always is, a fold hides everything under it at every depth, and a subagent's
// events nest under the step that started it, through a grandchild.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { frame, meta } from "$lib/mock/scenarios.ts";
import { buildReplayLog } from "$lib/state/replayLog.ts";
import { logLines, logSignal } from "$lib/state/logTree.ts";

const tool = (tool_id, tool_name, tool_input = {}) => ({ tool_id, tool_name, tool_input });
const call = (t, dt, end = "tool_end") => [
  frame("tool_requested", { type: "tool_requested", ...meta(1, dt), ...t }),
  frame("tool_start", { type: "tool_start", ...meta(1, dt + 0.1), ...t }),
  end === "tool_end"
    ? frame("tool_end", { type: "tool_end", ...meta(1, dt + 0.2), ...t, tool_output: "ok" })
    : frame("tool_error", { type: "tool_error", ...meta(1, dt + 0.2), ...t, message: "Upstream 503" })
];

describe("logSignal", () => {
  const rows = buildReplayLog([
    frame("turn_started", { type: "turn_started", ...meta(1, 0) }),
    ...call(tool("ok", "read_file"), 1),
    ...call(tool("bad", "read_file"), 2, "tool_error"),
    frame("tool_approval_requested", { type: "tool_approval_requested", ...meta(1, 3), ...tool("gate", "write_note") })
  ]).rows;
  const by = (label) => rows.find((row) => row.label === label);

  it("never calls an error routine, and always calls routine events routine", () => {
    assert.equal(logSignal(by("Tool failed")), "error");
    assert.equal(logSignal(by("Approval requested")), "approval");
    for (const label of ["Turn started", "Tool requested", "Tool started", "Tool completed"]) {
      assert.equal(logSignal(by(label)), null, `${label} is routine`);
    }
  });

  it("folds a routine step by default and keeps a step with an error open", () => {
    const heads = logLines(rows).filter((line) => line.kind === "step");
    const ok = heads.find((line) => line.step.id === by("Tool requested").id);
    const bad = heads.find((line) => line.signal === "error");
    assert.equal(ok?.collapsed, true);
    assert.equal(bad?.collapsed, false);
    assert.equal(logLines(rows, {}, true).filter((line) => line.kind === "step" && line.collapsed).length, 0, "a search opens everything");
  });
});

describe("logLines", () => {
  /* A Code Mode host whose script makes two calls: host head, call heads, call events. */
  const host = tool("host", "run_temporal_code", { script: "search()" });
  const rows = buildReplayLog([
    frame("tool_requested", { type: "tool_requested", ...meta(1, 0), ...host }),
    frame("tool_start", { type: "tool_start", ...meta(1, 0.1), ...host }),
    ...call(tool("c1", "search_flights"), 1),
    ...call(tool("c2", "search_hotels"), 2),
    frame("tool_end", { type: "tool_end", ...meta(1, 3), ...host, tool_output: "done" })
  ]).rows;
  const hostId = rows[0].id;
  const open = Object.fromEntries(rows.map((row) => [row.id, false]));

  it("nests a script's calls under its host, three levels deep", () => {
    const lines = logLines(rows, open);
    const depthOf = (label, name) =>
      lines.find((line) => line.kind === "row" && line.row.label === label && line.row.toolName === name)?.depth;
    assert.equal(lines[0].kind, "step");
    assert.equal(lines[0].depth, 0);
    assert.equal(depthOf("Tool started", "run_temporal_code"), 1);
    assert.equal(lines.find((line) => line.kind === "step" && line.step.name === "search_flights")?.depth, 1);
    assert.equal(depthOf("Tool completed", "search_flights"), 2);
    /* In event order under the host: its start, then the calls, then its end. */
    assert.equal(lines.at(-1).kind === "row" && lines.at(-1).row.label, "Tool completed");
    assert.equal(lines.at(-1).row.toolName, "run_temporal_code");
  });

  it("hides every descendant of a folded head and still knows which rows it holds", () => {
    const lines = logLines(rows, { ...open, [hostId]: true });
    assert.equal(lines.length, 1, "the host's events and its calls' heads and events are all hidden");
    assert.equal(lines[0].collapsed, true);
    assert.deepEqual([...lines[0].rowIds].sort(), rows.map((row) => row.id).sort());
  });

  /* A subagent's workflow, and one it starts, as the controller feeds them in. */
  it("nests a subagent's events under the step that started it, through a grandchild", () => {
    const child = (data, workflowId) => ({ frame: data, workflowId, role: "subagent", parentTurnNumber: 1, label: workflowId });
    const rows = buildReplayLog([
      { frame: frame("subagent_started", { type: "subagent_started", ...meta(1, 0), subagent_id: "s1", agent_key: "research", workflow_id: "wf-child" }), workflowId: "wf-root", role: "parent" },
      child(frame("tool_start", { type: "tool_start", ...meta(1, 1), ...tool("k1", "search") }), "wf-child"),
      child(frame("tool_end", { type: "tool_end", ...meta(1, 2), ...tool("k1", "search"), tool_output: "ok" }), "wf-child"),
      child(frame("subagent_started", { type: "subagent_started", ...meta(1, 3), subagent_id: "s2", agent_key: "fares", workflow_id: "wf-grand" }), "wf-child"),
      child(frame("turn_started", { type: "turn_started", ...meta(1, 4) }), "wf-grand"),
      { frame: frame("subagent_stopped", { type: "subagent_stopped", ...meta(1, 5), subagent_id: "s1", agent_key: "research", workflow_id: "wf-child" }), workflowId: "wf-root", role: "parent" }
    ]).rows;
    const lines = logLines(rows, Object.fromEntries(rows.map((row) => [row.id, false])));
    const depth = (predicate) => lines.find(predicate)?.depth;
    assert.equal(depth((line) => line.kind === "step" && line.step.kind === "subagent"), 0);
    assert.equal(depth((line) => line.kind === "step" && line.step.name === "search"), 1);
    assert.equal(depth((line) => line.kind === "row" && line.row.label === "Tool completed"), 2);
    assert.equal(depth((line) => line.kind === "row" && line.row.label === "Turn started"), 2);
  });
});
