import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { reconstructEditorWorkspace } from "./editorWorkspace.ts";

const meta = {
  agent_id: "builder",
  turn_id: "turn-1",
  turn_number: 1,
  message_id: "message-1",
  timestamp: 1,
  resume_offset: 1
};
const requested = (toolId, toolName, toolInput) => ({
  event: "callback_requested",
  data: {
    type: "callback_requested",
    ...meta,
    tool_id: toolId,
    tool_name: toolName,
    tool_input: toolInput,
    output_schema: { type: "string" }
  }
});
const resolved = (toolId, toolName, outcome = "ok") => ({
  event: "callback_resolved",
  data: {
    type: "callback_resolved",
    ...meta,
    tool_id: toolId,
    tool_name: toolName,
    outcome,
    error: outcome === "ok" ? null : "rejected"
  }
});
const ended = (toolId, toolName, toolOutput) => ({
  event: "tool_end",
  data: {
    type: "tool_end",
    ...meta,
    tool_id: toolId,
    tool_name: toolName,
    tool_output: toolOutput
  }
});

describe("editor workspace reconstruction", () => {
  it("replays a write and successive edits with version history", () => {
    const frames = [
      requested("write-1", "write_code", { code: 'async def main():\n    return "draft"\n' }),
      resolved("write-1", "write_code"),
      ended("write-1", "write_code", "Wrote 2 lines."),
      requested("edit-1", "edit_code", { old_text: '"draft"', new_text: '"researched"' }),
      resolved("edit-1", "edit_code"),
      ended("edit-1", "edit_code", "Replaced it; the new text is on lines 2-2."),
      requested("edit-2", "edit_code", { old_text: "main", new_text: "run_flow" }),
      resolved("edit-2", "edit_code"),
      ended("edit-2", "edit_code", "Replaced it; the new text is on lines 1-1.")
    ];

    const workspace = reconstructEditorWorkspace(frames);
    assert.equal(workspace.script, 'async def run_flow():\n    return "researched"\n');
    assert.equal(workspace.version, 3);
    assert.deepEqual(workspace.edits.map((edit) => edit.version), [1, 2, 3]);
    assert.equal(workspace.edits[1].result, "Replaced it; the new text is on lines 2-2.");
  });

  it("accepts an early result but ignores duplicate, failed, and invalid edits", () => {
    const earlyWrite = requested("write-1", "write_code", { code: "alpha" });
    const frames = [
      ended("edit-too-early", "edit_code", "Replaced it."),
      requested("edit-too-early", "edit_code", { old_text: "alpha", new_text: "wrong" }),
      ended("write-1", "write_code", "Wrote 1 lines."),
      earlyWrite,
      earlyWrite,
      requested("edit-duplicate", "edit_code", { old_text: "alpha", new_text: "omega" }),
      resolved("edit-duplicate", "edit_code"),
      requested("edit-duplicate", "edit_code", { old_text: "omega", new_text: "again" }),
      requested("failed-write", "write_code", { code: "lost" }),
      resolved("failed-write", "write_code", "error")
    ];

    const workspace = reconstructEditorWorkspace(frames);
    assert.equal(workspace.script, "omega");
    assert.equal(workspace.version, 2);
    assert.deepEqual(workspace.edits.map((edit) => edit.toolId), ["write-1", "edit-duplicate"]);
  });

  it("returns an empty version-zero workspace without edits", () => {
    assert.deepEqual(reconstructEditorWorkspace([]), {
      script: "",
      version: 0,
      edits: []
    });
  });
});
