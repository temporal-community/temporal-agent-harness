// ABOUTME: Pins the pending-callback card: a `callback_requested` event (e.g. ReactAgent's
// ask_user) shows its question and a response form in the chat pane, and the card goes away on
// `callback_resolved` — whichever client answered it, this pane or the `just react-client` CLI.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import AgentChatPanel from "./AgentChatPanel.svelte";
import { resultForm } from "$lib/components/chat/schemaForm.ts";
import { buildReplayLog } from "$lib/state/replayLog.ts";

const meta = (seconds) => ({
  agent_id: "root",
  turn_id: "turn-1",
  turn_number: 1,
  message_id: "m1",
  timestamp: 1_700_000_000 + seconds,
  resume_offset: seconds
});

const requested = {
  event: "callback_requested",
  data: {
    type: "callback_requested",
    ...meta(1),
    tool_id: "call_ask1",
    tool_name: "ask_user",
    tool_input: { question: "Which Springfield do you mean?" },
    output_schema: { type: "string" }
  }
};

const resolved = {
  event: "callback_resolved",
  data: {
    type: "callback_resolved",
    ...meta(2),
    tool_id: "call_ask1",
    tool_name: "ask_user",
    outcome: "ok",
    error: null
  }
};

const chatHtml = (frames) =>
  render(AgentChatPanel, {
    props: {
      items: [],
      logs: buildReplayLog(frames).rows,
      agentLabel: "ReAct",
      sessionId: "wf-root",
      onCallbackResult: async () => {}
    }
  }).body;

describe("pending callback card", () => {
  it("shows the question and a labelled response box while the callback is open", () => {
    const body = chatHtml([requested]);
    assert.match(body, /aria-label="Pending callback requests"/);
    assert.match(body, /ask_user/);
    assert.match(body, /Which Springfield do you mean\?/);
    assert.match(body, /<label[^>]*for="callback-call_ask1-result"[^>]*>\s*Response/);
    assert.match(body, /<input[^>]*id="callback-call_ask1-result"/);
    assert.match(body, /Send response/);
  });

  it("labels a lone non-question argument by name instead of showing a bare value", () => {
    const tree = {
      ...requested,
      data: { ...requested.data, tool_id: "call_tree1", tool_name: "tree", tool_input: { path: "." } }
    };
    const body = chatHtml([tree]);
    assert.match(body, /<dt[^>]*>path<\/dt>\s*<dd[^>]*>\.<\/dd>/);
    assert.doesNotMatch(body, /<p class="prose[^"]*">\.<\/p>/);
    assert.match(chatHtml([requested]), /<p class="prose[^"]*">Which Springfield do you mean\?<\/p>/);
  });

  it("goes away once the callback resolves, whoever answered it", () => {
    const body = chatHtml([requested, resolved]);
    assert.doesNotMatch(body, /Pending callback requests/);
    assert.doesNotMatch(body, /Send response/);
  });

  it("carries the output schema onto the replay row", () => {
    const [row] = buildReplayLog([requested]).rows;
    assert.equal(row.event, "callback_requested");
    assert.equal(row.toolId, "call_ask1");
    assert.deepEqual(row.outputSchema, { type: "string" });
  });
});

describe("resultForm", () => {
  it("wraps a scalar output as one required field, to be unwrapped on send", () => {
    const { fields, wrapped } = resultForm({ type: "string" });
    assert.equal(wrapped, true);
    assert.deepEqual(
      fields.map((f) => [f.name, f.kind, f.required, f.title]),
      [["result", "string", true, "Response"]]
    );
  });

  it("renders an object output field by field, resolving $defs", () => {
    const { fields, wrapped } = resultForm({
      type: "object",
      properties: { city: { type: "string", title: "City" }, unit: { $ref: "#/$defs/Unit" } },
      required: ["city"],
      $defs: { Unit: { enum: ["C", "F"] } }
    });
    assert.equal(wrapped, false);
    assert.deepEqual(fields.map((f) => [f.name, f.kind]), [["city", "string"], ["unit", "enum"]]);
  });

  it("keeps $defs reachable when a non-object output is wrapped", () => {
    const { fields } = resultForm({
      $ref: "#/$defs/Unit",
      $defs: { Unit: { enum: ["C", "F"] } }
    });
    assert.equal(fields[0].kind, "enum");
    assert.deepEqual(fields[0].choices, ["C", "F"]);
  });
});
