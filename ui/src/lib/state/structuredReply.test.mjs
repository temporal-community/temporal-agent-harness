// ABOUTME: A handler's reply is prose or data, decided once (handlerReply.ts) for the chat bubble,
// the transcript, the log, and the graph. Prose is exactly the free-text output shape — only
// `text` (TextReply), or the legacy only-`message` — read from the handler's declared output
// schema, or from the payload's own keys when no schema is known. Everything else is data and is
// shown whole: a sentence beside records used to hide the records, and `{text: ""}` beside them
// rendered a blank bubble. While streaming, a declared handler decides from its first delta, and
// only an unknown one is sniffed — strictly, so prose that opens with `[1]` or `{name}` stays prose.
// A handler error mid-stream ends the bubble's stream and shows the error in it.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { jsonReplyScenarios } from "../mock/jsonReplyScenarios.ts";
import { buildAgentGraph } from "./flowProjection.ts";
import { handlerReply, proseSchema, streamingIsData } from "./handlerReply.ts";
import { buildReplayLog } from "./replayLog.ts";
import { buildTranscript } from "./transcript.ts";

let offset = 0;
const meta = () => ({
  agent_id: "agent",
  turn_id: "turn-1",
  turn_number: 1,
  message_id: "msg-1",
  timestamp: 1_700_000_000 + offset,
  resume_offset: `${offset++}`
});
const frame = (event, data) => ({ event, data: { ...meta(), type: event, ...data } });
const accepted = (handler = "clean") =>
  frame("message_accepted", { handler, payload: { text: "clean these rows" }, disposition: "opened" });

const TEXT_REPLY = { title: "TextReply", type: "object", properties: { text: { type: "string" } }, required: ["text"] };
const CLEANED = {
  title: "Cleaned",
  type: "object",
  properties: { text: { type: "string" }, records: { type: "array" }, skipped: { type: "array" } }
};
const iface = (output) => [{ name: "clean", description: "", parameters: {}, output, mid_turn: "queue", model_callable: false }];

const cleaned = {
  records: [{ name: "Dana Scully", email: "dana@acme.test", company: "Acme", status: "active" }],
  skipped: ["no email here"]
};

/** Every surface that reads a finished reply, for one run. */
function surfaces(output, agentInterface) {
  const frames = [accepted(), frame("turn_started", {}), frame("message_handler_end", { output }), frame("turn_end", {})];
  return {
    reply: buildTranscript(frames, agentInterface).find((item) => item.kind === "agent"),
    logged: buildReplayLog(frames, agentInterface ? { "": agentInterface } : {}).rows.find((row) => row.label === "Handler reply"),
    output: buildAgentGraph(frames, { agentInterface }).nodes.find((node) => node.id === "output")
  };
}

describe("prose or data", () => {
  it("reads the declared output schema", () => {
    assert.equal(proseSchema(TEXT_REPLY), true);
    assert.equal(proseSchema({ type: "object", properties: { message: { type: "string" } } }), true, "legacy");
    assert.equal(proseSchema(CLEANED), false, "text beside other fields is data");
    assert.equal(proseSchema({ type: "array", items: {} }), false, "a declared non-object is data");
    assert.equal(proseSchema(undefined), undefined, "no schema, no answer");
  });

  it("falls back to the payload's own keys by the same rule", () => {
    assert.deepEqual(handlerReply({ output: { text: "hello" } }), { text: "hello" });
    assert.deepEqual(handlerReply({ output: { message: "hello" } }), { text: "hello" });
    const both = { text: "Cleaned 3 rows; 1 skipped.", score: 0.9 };
    assert.deepEqual(handlerReply({ output: both }).json, both);
    const blank = { text: "", records: [1] };
    assert.deepEqual(handlerReply({ output: blank }), { text: JSON.stringify(blank, null, 2), json: blank });
  });

  it("lets a data schema overrule a payload that happens to look like prose", () => {
    assert.deepEqual(handlerReply({ output: { text: "only" } }, CLEANED).json, { text: "only" });
    assert.equal(handlerReply({ output: { text: "hi" } }, TEXT_REPLY).json, undefined);
  });

  it("shows data whole on every surface, text and all", () => {
    for (const output of [cleaned, { text: "Cleaned 1 row.", ...cleaned }, { text: "", ...cleaned }]) {
      const json = JSON.stringify(output, null, 2);
      const { reply, logged, output: node } = surfaces(output, iface(CLEANED));
      assert.equal(reply?.data, true);
      assert.deepEqual(reply?.json, output, "the chat pane renders the whole payload");
      assert.equal(reply?.text, json);
      assert.equal(logged?.body, json, "the log shows the same JSON");
      assert.equal(node?.data.detail, json, "the output node shows the same JSON");
      assert.deepEqual(surfaces(output).reply?.json, output, "and without a schema");
    }
  });

  it("keeps a free-text reply as prose", () => {
    const { reply, logged, output } = surfaces({ text: "hello" }, iface(TEXT_REPLY));
    assert.equal(reply?.text, "hello");
    assert.equal(reply?.data, false);
    assert.equal(reply?.json, undefined, "a free-text reply stays a prose bubble");
    assert.equal(logged?.body, "hello");
    assert.equal(output?.data.detail, "hello");
  });
});

describe("a reply still streaming", () => {
  it("follows the declared schema from the first delta", () => {
    assert.equal(streamingIsData("Sure", CLEANED), true);
    assert.equal(streamingIsData('{"records"', TEXT_REPLY), false);
  });

  it("sniffs strictly when no schema is known", () => {
    for (const prose of ["[1] See the footnote", "{name} is a placeholder", "[x] done", "[", "{", "Sure — here", "[1]"]) {
      assert.equal(streamingIsData(prose), false, prose);
    }
    for (const json of ['{"records', "{}", '[{"a"', '["a"', "[[1", "[]", "[1, 2", "[-0.5,", "[true,", '\n  {\n  "records"', '```json\n{"records', "```\n[{"]) {
      assert.equal(streamingIsData(json), true, json);
    }
  });

  it("marks the bubble as data only once the opening says so", () => {
    const delta = (text) => frame("reply_delta", { text });
    const reply = (frames, agentInterface) => buildTranscript(frames, agentInterface).find((item) => item.kind === "agent");
    assert.equal(reply([accepted(), delta("[1] See the footnote")]).data, false);
    assert.equal(reply([accepted(), delta('```json\n{"rec')]).data, true);
    assert.equal(reply([accepted(), delta("Sure")], iface(CLEANED)).data, true, "a declared data handler");
  });

  it("ends the stream on a handler error and shows the error in the bubble", () => {
    const frames = [
      accepted(),
      frame("turn_started", {}),
      frame("reply_delta", { text: '{"records": [{"name": "Da' }),
      frame("message_handler_error", { message: "Upstream timed out" }),
      frame("turn_end", {})
    ];
    const reply = buildTranscript(frames).find((item) => item.kind === "agent");
    assert.equal(reply?.streaming, false, "no bubble streams forever");
    assert.equal(reply?.error, "Upstream timed out");
    assert.equal(reply?.text, '{"records": [{"name": "Da', "the partial reply stays");
  });

  it("leaves no finished reply in the mock scenarios streaming", () => {
    for (const [name, { frames, agentInterface }] of Object.entries(jsonReplyScenarios())) {
      for (const item of buildTranscript(frames, agentInterface)) {
        if (item.kind === "agent") assert.equal(item.streaming, false, `${name} ${item.id}`);
      }
    }
    const { frames, agentInterface } = jsonReplyScenarios().worst;
    const ledWithNewline = buildTranscript(frames, agentInterface).find((item) => item.id === "reply-msg-7-0");
    assert.deepEqual(ledWithNewline?.json, { records: [{ name: "Ana Souza", email: "ana@example.com", company: "Northwind", status: "active" }], skipped: [] });
  });
});
