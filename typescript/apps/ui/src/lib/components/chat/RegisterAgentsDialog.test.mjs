// ABOUTME: The add-agents form renders one labelled fieldset per agent with every registry field and
// an optional schema, offers importing files, a folder or a zip, and ends on Cancel and Register.
// Rendered on the server (no DOM here), so this checks markup; reading files, the checks and the
// round trip are agentRegistration.test.mjs's.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import RegisterAgentsDialog from "./RegisterAgentsDialog.svelte";
import { stripComments } from "../../../../tests/support/controllerHarness.mjs";

const actions = {
  async register() {},
  async registration() {
    throw new Error("unused");
  },
  async retire() {},
  async remove() {}
};

const html = (props = {}) => stripComments(render(RegisterAgentsDialog, { props: { actions, onClose() {}, ...props } }).body);

const row = (key) => ({
  key,
  workflow_type: `${key}Agent`,
  task_queue: `${key}-queue`,
  label: key,
  description: `The ${key} agent.`,
  schema: ""
});

describe("RegisterAgentsDialog", () => {
  it("starts with one empty agent, every field labelled", () => {
    const body = html();

    assert.equal([...body.matchAll(/<fieldset/g)].length, 1);
    for (const label of ["Key", "Label", "Workflow type", "Task queue", "Description", "Schema document"]) {
      assert.match(body, new RegExp(`<label[^>]*for="[^"]+"[^>]*>${label}`));
    }
    assert.match(body, />\s*Register agent\s*<\/button>/);
    assert.doesNotMatch(body, /aria-label="Remove agent 1"/, "the last agent cannot be removed");
  });

  it("fills one fieldset per given row and counts them on Register", () => {
    const body = html({ rows: [row("alpha"), row("beta")] });

    assert.equal([...body.matchAll(/<fieldset/g)].length, 2);
    assert.match(body, /value="alpha"/);
    assert.match(body, /value="beta-queue"/);
    assert.match(body, /aria-label="Remove agent 2"/);
    assert.match(body, />\s*Register 2 agents\s*<\/button>/);
  });

  it("imports files, a folder or a zip", () => {
    const body = html();

    assert.match(body, /<input[^>]*type="file"[^>]*multiple[^>]*accept=".toml,.json,.zip"/);
    assert.match(body, /<input[^>]*type="file"[^>]*webkitdirectory/);
  });

  it("ends on Cancel and Register, the consequential one last", () => {
    const footer = html().slice(html().indexOf('<footer class="register-actions'));
    const cancel = footer.search(/>\s*Cancel</);
    const register = footer.search(/>\s*Register agent</);

    assert.ok(cancel > -1 && register > cancel);
    assert.match(footer, /<button[^>]*class="chip sm solid accent[^"]*"[^>]*type="submit"/);
  });
});
