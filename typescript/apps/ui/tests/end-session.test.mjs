// ABOUTME: Pins where ending a session lives now that the composer no longer offers it: an X on
// each open row of the Session Manager, and nowhere in the header or the chat. It asks first,
// with the safe answer as the default. Ending a row that is not on screen closes that session,
// not the one being read.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import EndSessionDialog from "../src/lib/components/chat/EndSessionDialog.svelte";
import SessionControls from "../src/lib/components/chat/SessionControls.svelte";
import { AgentRunController } from "../src/lib/state/agentRun.svelte.ts";
import { stripComments } from "./support/controllerHarness.mjs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const controls = read("../src/lib/components/chat/SessionControls.svelte");
const app = read("../src/App.svelte");

const session = (id, extra = {}) => ({
  workflow_id: id,
  agent_workflow_type: "OpenAIHello",
  created_at: 1_790_000_000,
  label: `Session ${id}`,
  ...extra
});
const markup = (display, props) =>
  stripComments(render(SessionControls, { props: { display, onEndSession: async () => {}, ...props } }).body);

describe("ending a session", () => {
  it("is not offered in the header", () => {
    const body = markup("launcher", { sessions: [session("wf-a")], sessionId: "wf-a" });
    assert.doesNotMatch(body, /End session|session-actions-trigger|role="menu"/);
    assert.doesNotMatch(controls, /session-menu|Ellipsis/);
  });

  it("is an X on every open row, named for the session, and on no ended one", () => {
    const body = markup("pane", {
      sessions: [session("wf-a"), session("wf-b", { closed: true })],
      sessionId: "wf-c",
      closedWorkflowIds: []
    });
    const label = "End session — Open AI Hello — Session wf-a";
    assert.match(body, new RegExp(`<button[^>]*aria-label="${label}"[^>]*data-tip="${label}"[^>]*>\\s*<svg[^>]*lucide-x`));
    assert.doesNotMatch(body, /aria-label="End session[^"]*wf-b"/, "an ended session has nothing to end");
  });

  it("is revealed on row hover and focus, and always shown where there is no hover", () => {
    assert.match(controls, /\.session-item:is\(:hover, :focus-within\) \.session-actions :global\(\.icon-button\),[^{]*\{\s*opacity: 1;/);
    assert.match(controls, /@media \(hover: none\) \{[\s*\S]*?\.session-actions :global\(\.icon-button\) \{\s*opacity: 1;/);
  });

  it("confirms with Cancel first and End session in the error tone", () => {
    const body = stripComments(
      render(EndSessionDialog, {
        props: { target: { workflowId: "wf-a", name: "Hello", trigger: null }, onConfirm: async () => {}, onClose: () => {} }
      }).body
    );
    assert.match(body, /End this session\?/);
    assert.match(body, /The agent stops and you can't send to it again\. Its history stays viewable\./);
    assert.match(body, /end-cancel[^"]*"[^>]*>\s*(<[^>]*>\s*)*Cancel[\s\S]*class="chip sm filled error toned[^"]*end-confirm[\s\S]*End session/);
  });

  it("is wired to the controller from the manager only, and ends a session that is not on screen", async () => {
    assert.equal(app.match(/onEndSession=\{\(sessionId\) => run\.endSession\(sessionId\)\}/g)?.length, 1);
    const pane = app.slice(app.indexOf('display="pane"'), app.indexOf("</DockedDrawer>"));
    assert.match(pane, /onEndSession=/);
    const closed = [];
    const controller = new AgentRunController(
      new Proxy({}, { get: (_, key) => (key === "closeSession" ? async (id) => void closed.push(id) : async () => []) })
    );
    controller.sessions = [session("wf-on-screen"), session("wf-other")];
    controller.session = controller.sessions[0];
    controller.sending = true;
    await controller.endSession("wf-other");
    assert.deepEqual(closed, ["wf-other"]);
    assert.ok(controller.closedWorkflowIds.includes("wf-other"));
    assert.equal(controller.sessionClosed, false, "the session being read is untouched");
    assert.equal(controller.sending, true);
  });
});
