import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  answerEditorTool,
  ClientToolCoordinator
} from "./clientToolProvider.ts";

const meta = {
  type: "callback_requested",
  agent_id: "builder",
  turn_id: "turn-1",
  turn_number: 1,
  message_id: "message-1",
  timestamp: 1,
  resume_offset: 1,
  output_schema: { type: "string" }
};

const requested = (toolId, toolName, toolInput) => ({
  event: "callback_requested",
  data: { ...meta, tool_id: toolId, tool_name: toolName, tool_input: toolInput }
});

const resolved = (toolId, toolName) => ({
  event: "callback_resolved",
  data: {
    ...meta,
    type: "callback_resolved",
    tool_id: toolId,
    tool_name: toolName,
    outcome: "ok",
    error: null
  }
});

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("editor client-tool provider", () => {
  it("matches the Studio results for reads, writes, edits, exports, and failed edits", () => {
    assert.deepEqual(answerEditorTool("read_code", {}, ""), {
      outcome: { result: "(empty)" },
      script: ""
    });
    const write = answerEditorTool("write_code", { code: "one\ntwo" }, "");
    assert.deepEqual(write, {
      outcome: { result: "Wrote 2 lines." },
      script: "one\ntwo",
      changedLines: { from: 1, to: 2 }
    });
    assert.deepEqual(answerEditorTool("read_code", {}, write.script).outcome, {
      result: "   1 | one\n   2 | two"
    });
    assert.deepEqual(answerEditorTool("edit_code", { old_text: "two", new_text: "three" }, write.script), {
      outcome: { result: "Replaced it; the new text is on lines 2-2." },
      script: "one\nthree",
      changedLines: { from: 2, to: 2 }
    });
    assert.deepEqual(answerEditorTool("export_code", {}, "one\nthree").outcome, {
      result: "one\nthree"
    });
    assert.deepEqual(answerEditorTool("edit_code", { old_text: "missing", new_text: "x" }, "one"), {
      outcome: {
        error:
          "old_text is not in the script. Call read_code and copy the text exactly, without the line-number prefixes."
      },
      script: "one"
    });
    assert.deepEqual(answerEditorTool("edit_code", { old_text: "x", new_text: "y" }, "x x"), {
      outcome: {
        error: "old_text appears 2 times. Include more surrounding lines so it matches exactly once."
      },
      script: "x x"
    });
  });

  it("tells the chat only when something about client tools changed", async () => {
    const changes = [];
    const coordinator = new ClientToolCoordinator({
      submit: async () => {},
      claim: async (_key, answer) => (await answer(), true),
      onChange: (statuses) => changes.push(statuses)
    });
    const frames = [requested("t1", "write_code", { code: "x" })];
    coordinator.sync(frames, "wf");
    await flush();
    const settled = changes.length;
    coordinator.sync([...frames, { event: "reply_delta", data: { ...meta, type: "reply_delta" } }], "wf");
    assert.equal(changes.length, settled, "a commit with nothing new for client tools changes nothing");
  });

  it("serializes callbacks against optimistic script state", async () => {
    const submissions = [];
    let releaseWrite;
    const coordinator = new ClientToolCoordinator({
      submit: async (request, outcome) => {
        submissions.push([request.toolName, outcome]);
        if (request.toolName === "write_code") {
          await new Promise((resolve) => (releaseWrite = resolve));
        }
      },
      claim: async (_key, answer) => {
        await answer();
        return true;
      }
    });

    coordinator.sync(
      [
        requested("write", "write_code", { code: "alpha" }),
        requested("edit", "edit_code", { old_text: "alpha", new_text: "omega" }),
        requested("read", "read_code", {})
      ],
      "workflow"
    );
    await flush();
    assert.deepEqual(submissions, [["write_code", { result: "Wrote 1 lines." }]]);
    releaseWrite();
    await flush();
    await flush();
    assert.deepEqual(submissions, [
      ["write_code", { result: "Wrote 1 lines." }],
      ["edit_code", { result: "Replaced it; the new text is on lines 1-1." }],
      ["read_code", { result: "   1 | omega" }]
    ]);
  });

  it("claims each callback once and skips resolved callbacks after reload", async () => {
    const claimed = new Set();
    const calls = [];
    const claim = async (key, answer) => {
      if (claimed.has(key)) return false;
      claimed.add(key);
      await answer();
      return true;
    };
    const options = {
      submit: async (request) => calls.push(request.toolId),
      claim
    };
    const first = new ClientToolCoordinator(options);
    const second = new ClientToolCoordinator(options);
    const frame = requested("write", "write_code", { code: "alpha" });
    first.sync([frame], "workflow");
    second.sync([frame], "workflow");
    await flush();
    assert.deepEqual(calls, ["write"]);

    const reloaded = new ClientToolCoordinator(options);
    reloaded.sync([frame, resolved("write", "write_code")], "workflow");
    await flush();
    assert.deepEqual(calls, ["write"]);
  });
});
