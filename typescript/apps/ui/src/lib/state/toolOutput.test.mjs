// ABOUTME: Pins how a tool's result, which tool_end carries as JSON, becomes the text the log and
// the image detection read: a string as it is, a structured result as indented JSON, and a tool
// with no result as `null` rather than a blank row.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildReplayLog } from "./replayLog.ts";
import { toolImages } from "./toolImages.ts";
import { toolOutputText } from "./toolOutput.ts";

const toolEnd = (tool_output) => ({
  event: "tool_end",
  data: {
    type: "tool_end",
    agent_id: "a",
    turn_id: "t",
    turn_number: 1,
    message_id: "m",
    timestamp: 1,
    resume_offset: 0,
    tool_id: "call-1",
    tool_name: "lookup",
    tool_output
  }
});

const outputOf = (frame) => buildReplayLog([frame]).rows.find((row) => row.toolId === "call-1")?.output;

describe("toolOutputText", () => {
  it("keeps a string result as it is", () => {
    assert.equal(toolOutputText("72F sunny"), "72F sunny");
  });

  it("indents a structured result", () => {
    assert.equal(toolOutputText({ a: [1, true] }), '{\n  "a": [\n    1,\n    true\n  ]\n}');
  });

  it("shows a tool with no result as null", () => {
    assert.equal(toolOutputText(null), "null");
  });

  it("is what the replay log's tool row holds", () => {
    assert.equal(outputOf(toolEnd({ ok: 5 })), '{\n  "ok": 5\n}');
    assert.equal(outputOf(toolEnd("done")), "done");
  });

  it("finds an image in a structured result", () => {
    const src = "data:image/png;base64,iVBORw0KGgo=";
    assert.deepStrictEqual(
      toolImages(outputOf(toolEnd({ image: { url: src } })), "lookup").map((image) => image.src),
      toolImages(JSON.stringify({ image: { url: src } }), "lookup").map((image) => image.src)
    );
  });
});
