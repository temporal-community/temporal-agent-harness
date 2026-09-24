// ABOUTME: Pins which IDs a Logs row's details offer to copy. The session's workflow ID used to sit on
// every row, identical down the whole pane and already copyable from the session anchor; what a
// reader needs there is what tells THIS row apart — its tool call, the turn a bracket closes, and
// the workflow of a subagent, which is the one workflow ID that does differ from row to row.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse } from "svelte/compiler";
import { describe, it } from "vitest";

import { buildReplayLog, rowIdentifiers } from "$lib/state/replayLog.ts";

const meta = (type, extra = {}) => ({
  type,
  agent_id: "agent",
  turn_id: "turn-abc",
  turn_number: 1,
  message_id: null,
  timestamp: 1,
  resume_offset: 0,
  ...extra
});
const frame = (type, extra) => ({ event: type, data: meta(type, extra) });
const parent = (item) => ({ frame: item, workflowId: "wf-session", role: "parent" });

const log = buildReplayLog([
  parent(frame("turn_end")),
  parent(frame("tool_start", { tool_id: "call_0123456789abcdef", tool_name: "search", tool_input: {} })),
  parent(frame("subagent_started", { subagent_id: "r-1", agent_key: "researcher", workflow_id: "wf-child-a" })),
  { frame: frame("reply_delta", { text: "hi" }), workflowId: "wf-child-b", role: "subagent", parentTurnNumber: 1 }
]);
const idsOf = (event) => rowIdentifiers(log.rows.find((row) => row.event === event));

describe("a Logs row's copyable IDs", () => {
  it("never repeats the session's workflow ID", () => {
    for (const row of log.rows) {
      assert.ok(
        rowIdentifiers(row).every((id) => id.value !== "wf-session"),
        `${row.event} should not show the session workflow`
      );
    }
    assert.deepEqual(idsOf("turn_end"), [{ label: "turn", value: "turn-abc" }]);
  });

  it("leads a tool row with its full tool call ID", () => {
    assert.deepEqual(idsOf("tool_start")[0], { label: "tool call", value: "call_0123456789abcdef" });
  });

  it("shows a child workflow, whether the child published the row or the row is about it", () => {
    assert.deepEqual(idsOf("subagent_started"), [{ label: "child workflow", value: "wf-child-a" }]);
    assert.deepEqual(idsOf("reply_delta"), [{ label: "child workflow", value: "wf-child-b" }]);
  });

  /* Copyable's value is not in its markup, so the wiring is checked where it is written:
     the copy button and the tooltip get the full value, and only the text on screen is cut. */
  it("copies and titles the full value", () => {
    const source = readFileSync(new URL("./TranscriptPanel.svelte", import.meta.url), "utf8");
    const block = source.slice(source.indexOf('<dl class="row-ids">'), source.indexOf("</dl>"));
    assert.ok(parse(source, { modern: true }), "the component should still parse");
    assert.match(block, /\{#each ids as id/);
    assert.match(block, /<Copyable value=\{id\.value\}/);
    assert.match(block, /<code title=\{id\.value\}>\{id\.value\}<\/code>/);
  });
});
