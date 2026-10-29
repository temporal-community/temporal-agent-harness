// ABOUTME: Guards the session anchor in the app's top-left chrome — the session's name
// and its status, beside the switch that opens the Session Manager.
//
// This exists because that strip has already been lost once: moving the manager out of a
// dropdown and into a drawer replaced the whole trigger with a lone icon, and the console
// stopped saying which session you were in at all. Nothing else on the chrome row carries
// that, so its absence is silent — the desk still renders, every pane still works, and the
// one line that names the run is simply gone. These are source assertions for that reason:
// the failure mode is markup that stopped being rendered, not state that computed wrong.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { render } from "svelte/server";
import { beforeEach, describe, it } from "vitest";

import SessionControls from "../src/lib/components/chat/SessionControls.svelte";
import { agentName, humanizeWorkflowType } from "../src/lib/state/sessionHistory.ts";
import { installBrowserSurface, stripComments } from "./support/controllerHarness.mjs";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");

const [app, controls] = await Promise.all([
  read("../src/App.svelte"),
  read("../src/lib/components/chat/SessionControls.svelte")
]);

const slice = (text, open, close, label) => {
  const start = text.indexOf(open);
  assert.notEqual(start, -1, `${label}: could not find ${open}`);
  const end = text.indexOf(close, start);
  assert.notEqual(end, -1, `${label}: could not find ${close}`);
  return text.slice(start, end);
};

/* The minimap's lead zone: the app's top-left, and the only place this strip belongs. */
const lead = slice(app, "{#snippet lead()}", "{/snippet}", "App's minimap lead");
/* The launcher display, which starts sessions and names the current one. */
const launcher = slice(
  controls,
  '{#if display === "launcher"}',
  "{:else}",
  "SessionControls' launcher"
);

describe("the session anchor in the app chrome", () => {
  it("is rendered in the minimap lead, beside the drawer switch", () => {
    assert.match(
      lead,
      /<SessionControls\b[\s\S]*display="launcher"/,
      "the app's top-left must render the session launcher, not only the drawer icon"
    );
    assert.match(
      lead,
      /<PanelLeft\b/,
      "the drawer's own switch must stay in the lead beside the anchor"
    );
  });

  it("speaks the session's name and its status", () => {
    assert.match(
      launcher,
      /class="kicker head-kind session-name" dir="auto" style=\{`--head-accent: var\(--\$\{statusTone\}\)`\}>\{agentTitle\}/,
      "the anchor must name the session, in the session's status hue"
    );
    assert.match(
      launcher,
      /class="kicker session-state">\{spokenStatus\}/,
      "the states that ask something of a reader must be spoken, not left to the hue"
    );
    assert.match(
      launcher,
      /\{#if !spokenStatus && agentTitle !== statusLabel\}<span class="session-state-spoken">\{statusLabel\}<\/span>/,
      "a reader who cannot see the hue must still be told the status, in text: aria-label on a span is ignored"
    );
    const identity = slice(controls, '{#snippet identity()}', '{/snippet}', 'read-only session identity');
    assert.doesNotMatch(identity, /onclick=|aria-expanded=/, "only the drawer switch toggles session history");
    assert.match(controls, /aria-haspopup="dialog"/, "creation opens its own dialog");
    assert.match(
      launcher,
      /session-new"[\s\S]*onclick=\{openNewSessionMenu\}[\s\S]*session-anchor"/,
      "New session must precede the current session in the chrome"
    );
    assert.doesNotMatch(
      controls.slice(controls.indexOf('<section class="session-manager"')),
      /label="New session"/,
      "the session list panel must not duplicate the New session button"
    );
  });

  /* Reactivity, as far as source can check it: every input the name and status are
     computed from is bound to the controller, so selecting a session inside the drawer
     is what moves them. A literal or a one-time copy here is the regression. */
  it("takes its name and status live from the run", () => {
    for (const prop of [
      "sessionId={run.runInfo.sessionId}",
      "sessions={run.sessions}",
      "agents={run.agents}",
      "connecting={run.connecting}",
      "sending={run.sending}",
      "creatingSession={run.creatingSession}",
      "onEnsureAgents={() => run.refreshAgents()}",
      "closed={run.sessionClosed}",
      "error={run.connectionError}",
      "{pendingLabel}"
    ]) {
      assert.ok(
        lead.includes(prop),
        `the anchor must read ${prop} from the run, or it will go stale on a session switch`
      );
    }
  });

  it("names an agent in words, never by its raw workflow type", () => {
    for (const [type, words] of [
      ["PydanticAiHelloAgent", "Pydantic Ai Hello Agent"],
      ["PydanticAIHelloAgent", "Pydantic AI Hello Agent"],
      ["QaAgent", "Qa Agent"],
      ["HTTPServerAgent", "HTTP Server Agent"],
      ["Agent2", "Agent 2"],
      ["coding_agent-v2", "coding agent v 2"],
      ["", ""]
    ]) {
      assert.equal(humanizeWorkflowType(type), words, type);
    }
    const agents = [{ key: "p", workflow_type: "PydanticAIHelloAgent", label: "Pydantic AI Hello", task_queue: "q", description: "" }];
    assert.equal(agentName(agents, "PydanticAIHelloAgent"), "Pydantic AI Hello");
    assert.equal(agentName(agents, "OpenAIHelloAgent"), "Open AI Hello Agent");
    assert.equal(agentName([{ ...agents[0], label: "  " }], "PydanticAIHelloAgent"), "Pydantic AI Hello Agent");
    assert.equal(agentName([], null), "Agent");
  });

  describe("rendered", () => {
    beforeEach(() => installBrowserSurface());

    const session = {
      workflow_id: "wf-1",
      created_at: 1_790_000_000,
      agent_workflow_type: "PydanticAIHelloAgent",
      initial_user_message: "What is the **weather** in Paris?"
    };
    const anchor = (agents) => {
      const body = stripComments(
        render(SessionControls, { props: { display: "launcher", sessionId: "wf-1", sessions: [session], agents } }).body
      );
      const open = body.match(/<span class="head-name session-anchor[^"]*"[^>]*>/)?.[0] ?? "";
      assert.ok(open, "the anchor rendered");
      const attr = (name) => open.match(new RegExp(`${name}="([^"]*)"`))?.[1];
      const inner = body.slice(body.indexOf(open) + open.length, body.indexOf("session-temporal"));
      return { body, inner, attr };
    };

    it("shows the registry label alone, with the title and description in its tip and name", () => {
      const { inner, attr } = anchor([
        { key: "p", workflow_type: "PydanticAIHelloAgent", label: "Pydantic AI Hello", task_queue: "q", description: "A *hello-world*\nagent." }
      ]);
      assert.match(inner, /class="kicker head-kind session-name[^"]*"[^>]*>Pydantic AI Hello<\/span>/);
      assert.doesNotMatch(inner, /What is the|session-title/, "the title is not visible text in the bar");
      assert.equal(attr("data-tip"), "What is the weather in Paris?\nPydantic AI Hello — A hello-world agent.");
      assert.equal(attr("aria-label"), "Pydantic AI Hello — What is the weather in Paris? · Available");
      assert.equal(attr("tabindex"), "0", "a keyboard reaches the tip");
    });

    it("falls back to the workflow type in words, never the raw type", () => {
      const { body, attr } = anchor([]);
      assert.match(body, /session-name[^"]*"[^>]*>Pydantic AI Hello Agent<\/span>/);
      assert.doesNotMatch(body, />PydanticAIHelloAgent</);
      assert.equal(attr("data-tip"), "What is the weather in Paris?\nPydantic AI Hello Agent");
    });
  });

  it("still leaves exactly one session list in the app", () => {
    assert.equal(
      (app.match(/display="pane"/g) ?? []).length,
      1,
      "the Session Manager list must be rendered once, in the drawer"
    );
    assert.equal(
      (app.match(/display="launcher"/g) ?? []).length,
      1,
      "the anchor must be rendered once, in the chrome"
    );
  });
});
