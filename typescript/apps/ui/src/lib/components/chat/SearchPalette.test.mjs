// ABOUTME: Pins the console's one search: everything at once, a few of each, or one list in
// full, scoped by shared Chips, and opened from Cmd/Ctrl+K and every list's search icon.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import SearchPalette from "./SearchPalette.svelte";

const app = readFileSync(new URL("../../../App.svelte", import.meta.url), "utf8");
const palette = readFileSync(new URL("./SearchPalette.svelte", import.meta.url), "utf8");
const controls = readFileSync(new URL("./SessionControls.svelte", import.meta.url), "utf8");

const sessions = Array.from({ length: 8 }, (_, i) => ({
  workflow_id: `wf-${i}`,
  agent_workflow_type: "QaAgent",
  initial_user_message: `Question ${i}`,
  created_at: 1_700_000_000 + i,
  closed: false
}));
const agents = [
  { key: "qa", label: "Q&A Agent", workflow_type: "QaAgent", task_queue: "qa-agent", description: "Answers." },
  { key: "dag", label: "Agent DAG Studio", workflow_type: "DagAgent", task_queue: "agent-dag", description: "Builds flows.", worker: { status: "no_worker" } }
];
const draw = (props = {}) =>
  stripComments(
    render(SearchPalette, {
      props: { open: false, scope: "all", sessions, agents, paneKinds: ["bay", "setup"], onSession() {}, onAgent() {}, onPane() {}, ...props }
    }).body
  );
const groups = (body) => [...body.matchAll(/<h3 class="kicker group-label[^"]*">([^<]+)<\/h3>/g)].map((match) => match[1]);
const hits = (body) => [...body.matchAll(/<span class="hit-title[^"]*"[^>]*>([^<]+)<\/span>/g)].map((match) => match[1]);

describe("the search palette", () => {
  it("shows a few of everything, grouped, newest sessions first", () => {
    const body = draw();
    assert.deepEqual(groups(body), ["Sessions", "Agents", "Panes"]);
    const titles = hits(body);
    assert.deepEqual(titles.slice(0, 5), ["Question 7", "Question 6", "Question 5", "Question 4", "Question 3"], "five sessions of eight");
    assert.deepEqual(titles.slice(5), ["Q&amp;A Agent", "Agent DAG Studio", "Agent Bay", "Setup"]);
    assert.match(body, /<span class="kicker hit-meta[^"]*">No worker<\/span>/, "an agent nobody can run says so");
  });

  it("shows one list in full when scoped to it", () => {
    const body = draw({ scope: "sessions" });
    assert.deepEqual(groups(body), ["Sessions"]);
    assert.equal(hits(body).length, 8);
    assert.match(body, /placeholder="Search sessions by message, agent, or ID…"/);
  });

  it("switches scope with the shared Chip, the chosen one marked", () => {
    const body = draw({ scope: "agents" });
    const tabs = [...body.matchAll(/<button role="tab" aria-selected="(true|false)" class="chip ([^"]*)"[^>]*>\s*([^<]+?)\s*<\/button>/g)].map((match) => [match[3], match[1], match[2].split(" ").includes("active")]);
    assert.deepEqual(tabs, [["All", "false", false], ["Sessions", "false", false], ["Agents", "true", true], ["Panes", "false", false]]);
  });

  it("marks each session with the Sessions list's own status glyph and says it", () => {
    const row = (workflow_id, extra) => ({ ...sessions[0], workflow_id, initial_user_message: workflow_id, ...extra });
    const body = draw({
      scope: "sessions",
      sessions: [row("open"), row("ended", { closed: true }), row("failed", { execution_status: "FAILED", closed: true }), row("gone", { execution_status: "NOT_FOUND" }), row("stalled", { agent_workflow_type: "DagAgent" }), row("ended-here")],
      closedWorkflowIds: ["ended-here"]
    });
    const status = (title) => {
      const hit = body.match(new RegExp(`<div[^>]*class="hit[^"]*"[^>]*>((?:(?!<div)[\\s\\S])*?)<span class="hit-title[^"]*"[^>]*>${title}</span>\\s*<span class="visually-hidden">, ([^<]+),</span>`));
      assert.ok(hit, `${title} has a glyph and a spoken status`);
      return [hit[1].match(/session-pip hollow|session-pip|lucide-circle-x|state-glyph/)?.[0], hit[2]];
    };
    assert.deepEqual(status("open"), ["session-pip", "Open"]);
    assert.deepEqual(status("ended"), ["session-pip hollow", "Closed"]);
    assert.deepEqual(status("ended-here"), ["session-pip hollow", "Closed"], "ended from this console before the list caught up");
    assert.equal(status("failed")[1], "Needs attention");
    assert.deepEqual(status("gone"), ["lucide-circle-x", "Unavailable"]);
    assert.equal(status("stalled")[1], "No worker");
    assert.doesNotMatch(body, /lucide-messages-square/, "no generic bubble once a row has its status");
  });

  it("wraps a long query it can't match rather than scrolling sideways", () => {
    assert.match(palette, /\.no-hits \{[^}]*overflow-wrap: anywhere;/);
  });

  it("drops the keyboard hint on a phone so the scopes keep their room", () => {
    assert.match(palette, /@media \(max-width: 520px\) \{\s*\.scope-hint \{\s*display: none;/);
  });

  it("closes from the same X every dialog has, its tip naming the key", () => {
    const body = draw();
    assert.match(body, /<button type="button" class="head-button[^"]*" aria-label="Close" data-tip="Close\nEsc"[^>]*>[\s\S]*?lucide-x/);
    assert.doesNotMatch(body, /<kbd/);
  });

  it("draws the scope keys' arrows as icons, and says them", () => {
    const hint = draw().match(/<span class="kicker scope-hint[^"]*">([\s\S]*?)<\/span><\/div>/)?.[1] ?? "";
    assert.match(hint, /lucide-arrow-left[\s\S]*lucide-arrow-right/);
    assert.match(hint, /<span class="visually-hidden">the arrow keys<\/span>/);
    assert.doesNotMatch(hint, /[←→]/);
  });

  it("opens on everything from Cmd/Ctrl+K, and scoped from each list's search icon", () => {
    assert.match(app, /\(event\.metaKey \|\| event\.ctrlKey\) && !event\.altKey && !event\.shiftKey && event\.key\.toLowerCase\(\) === "k"\) \{\s*event\.preventDefault\(\);\s*openPalette\("all"\);/);
    assert.match(app, /onOpenSearch=\{\(\) => openPalette\("sessions"\)\}/, "the Sessions sidebar's icon");
    assert.match(app, /aria-label="Search agents"[\s\S]{0,200}?onclick=\{\(\) => openPalette\("agents"\)\}/, "the Agent Bay's icon");
    assert.match(controls, /\{#if onOpenSearch\}\s*<button[\s\S]*?aria-label="Search sessions"[\s\S]*?\{#if !onOpenSearch\}\s*<div class="session-filters">/, "the sidebar's own search and filter give way to the icon");
  });

  it("does what clicking the row in place would", () => {
    assert.match(app, /onSession=\{\(workflowId\) => run\.selectSession\(workflowId\)\}/);
    assert.match(app, /onAgent=\{\(workflowType\) => \{\s*palettePreferred = workflowType;\s*newSessionOpen = true;\s*\}\}/, "an agent opens New session on it");
    assert.match(
      app,
      /onPane=\{\(kind\) => \{[^}]*deskStack\.exitBleed\(\);\s*deskDrawer\.exitBleed\(\);\s*deskStack\.openPane\(\{ kind \}\);/,
      "a pane picked while another is full-screen comes into view rather than opening behind it"
    );
  });
});
