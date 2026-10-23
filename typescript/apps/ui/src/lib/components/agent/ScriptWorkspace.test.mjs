import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { reconstructEditorWorkspace } from "$lib/state/editorWorkspace.ts";
import { PANE_KINDS } from "$lib/panes/registry.ts";
import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import ScriptWorkspace from "./ScriptWorkspace.svelte";

const source = readFileSync(new URL("./ScriptWorkspace.svelte", import.meta.url), "utf8");
const chatSource = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../../../../src/App.svelte", import.meta.url), "utf8");

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

const resolved = (toolId, toolName) => ({
  event: "callback_resolved",
  data: {
    type: "callback_resolved",
    ...meta,
    tool_id: toolId,
    tool_name: toolName,
    outcome: "ok",
    error: null
  }
});

describe("script workspace", () => {
  it("renders the full reconstructed source with line numbers and the latest edit highlighted", () => {
    const frames = [
      requested("write", "write_code", { code: "async def flow():\n    return 'draft'" }),
      resolved("write", "write_code"),
      requested("edit", "edit_code", { old_text: "'draft'", new_text: "'done'" }),
      resolved("edit", "edit_code")
    ];
    const workspace = reconstructEditorWorkspace(frames);
    const body = stripComments(render(ScriptWorkspace, {
      props: { script: workspace.script, changedLines: { from: 2, to: 2 } }
    }).body);

    assert.match(body, /aria-label="Current script"/);
    assert.match(body, /<i[^>]*>1<\/i><code[^>]*>async def flow\(\):<\/code>/);
    assert.match(body, /<span class="[^"]*changed[^"]*"><i[^>]*>2<\/i><code[^>]*>    return 'done'<\/code>/);
    assert.match(body, /aria-label="Copy script"/);
    assert.doesNotMatch(source, /max-height/, "the pane owns the available height; code must not be capped");
  });

  it("uses the shared pane empty state before a script exists", () => {
    const body = stripComments(render(ScriptWorkspace, {
      props: { script: "" }
    }).body);
    assert.match(body, /class="empty-state/);
    assert.match(body, /No script yet/);
  });

  it("is registered as a pane and opened by the chat update row", () => {
    assert.ok(PANE_KINDS.includes("script"));
    assert.match(chatSource, /name="Script updated"/);
    assert.match(chatSource, /onActivate=\{onOpenScript\}/);
    assert.match(
      chatSource,
      /name="Script updated"[\s\S]{0,120}tip=\{onOpenScript \? "Open the script" : undefined\}/,
      "the row says what pressing it does; the console has no Run, so it opens the pane"
    );
    assert.doesNotMatch(chatSource, /<ScriptWorkspace/, "the workspace must not be pinned above the composer");
    assert.match(
      appSource,
      /onOpenScript=\{\(\) => stack\.openPane\(\{ kind: "script" \}, "chat"\)\}/
    );
  });
});
