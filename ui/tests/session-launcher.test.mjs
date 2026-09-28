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
import { describe, it } from "vitest";

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
/* The launcher display, which is the anchor and nothing else. */
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
      /class="session-name">\{agentTitle\}/,
      "the anchor must name the session"
    );
    assert.match(
      launcher,
      /tone=\{statusTone\}/,
      "the anchor's pip must take the session's status tone"
    );
    assert.match(
      launcher,
      /class="session-state">\{spokenStatus\}/,
      "the states that ask something of a reader must be spoken, not left to the pip"
    );
    assert.match(
      launcher,
      /aria-label=\{`\$\{agentTitle\} — \$\{statusLabel\}/,
      "a reader who cannot see the pip must still be told the name and the status"
    );
    assert.doesNotMatch(
      launcher,
      /session-new/,
      "the chrome strip must not carry a New chip; new sessions start from the drawer"
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
