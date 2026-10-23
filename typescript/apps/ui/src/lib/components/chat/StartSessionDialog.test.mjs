// ABOUTME: The start dialog browses agents as a windowed grid of cards with capability tags and
// filters read only from registry fields, says a worker's state only when it is not ready, shows
// the selected agent's handlers, state and init form beside the grid, and moves through the grid
// in two dimensions. Rendered on the server (no DOM here), so this checks markup and the module's
// pure helpers; opening via showModal() and submitting are the controller's and the browser's.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import StartSessionDialog, { agentEntries, columnsFor, filterAgents, filterChips, gridMove } from "./StartSessionDialog.svelte";
import { stripComments } from "../../../../tests/support/controllerHarness.mjs";

const source = readFileSync(new URL("./StartSessionDialog.svelte", import.meta.url), "utf8");

const schema = {
  title: "MatchSettings",
  description: "Who the agent is playing.",
  type: "object",
  properties: { player_name: { type: "string", title: "Player Name" } },
  required: ["player_name"]
};

const agent = (required) => ({
  key: "tictactoe",
  label: "Tic-Tac-Toe",
  workflow_type: "TicTacToeAgent",
  task_queue: "q",
  description: "",
  init_data: { required, schema }
});

const worker = (status) => ({ status, task_queue: "q", poller_count: status === "ready" ? 1 : 0, last_seen: null, error: null });

const fleet = (count, extra = () => ({})) =>
  Array.from({ length: count }, (_, i) => ({
    key: `agent-${i}`,
    label: `Agent ${i}`,
    workflow_type: `Agent${i}Workflow`,
    task_queue: "q",
    description: i === 1 ? "Books **flights** and hotels." : `Does task ${i}.`,
    worker: worker("ready"),
    ...extra(i)
  }));

const html = (props) =>
  stripComments(render(StartSessionDialog, { props: { agents: [], onStart() {}, onClose() {}, ...props } }).body);

/* Each card, from its tag through its description (and tags, when it has them). */
const cards = (body) =>
  [...body.matchAll(/<div[^>]*role="option"[\s\S]*?class="agent-description[^>]*>[^<]*<\/span>\s*(?:<span class="agent-tags[^>]*>(?:<span class="tag[^>]*>[^<]*<\/span>)*<\/span>)?/g)].map(([text]) => text);

describe("StartSessionDialog", () => {
  it("renders the data model's fields for the agent being started", () => {
    const body = html({ agents: [agent(true)] });
    assert.match(body, /aria-label="Start session with Tic-Tac-Toe"/);
    assert.match(body, /MatchSettings/);
    assert.match(body, /Player Name/);
  });

  it("offers to start without the data only when it is optional", () => {
    assert.doesNotMatch(html({ agents: [agent(true)] }), /Start without/);
    assert.match(html({ agents: [agent(false)] }), /Start without/);
  });

  it("disables starting and explains when the registry is empty", () => {
    const body = html({ agents: [] });
    assert.match(body, /No agents are registered yet/);
    assert.match(body, /<button[^>]*type="submit"[^>]*disabled/);
  });

  it("tags a card only from fields the registry serves", () => {
    const bare = { key: "bare", label: "Bare", workflow_type: "Bare", task_queue: "q", description: "x" };
    assert.deepEqual(agentEntries([bare])[0].tags, [], "an agent the registry says nothing about has no tags");
    const rich = {
      ...bare,
      init_data: { required: true, schema },
      states: ["board"],
      handlers: [
        { name: "new_game", description: "Reset." },
        { name: "play", description: "Play a cell." }
      ]
    };
    assert.deepEqual(agentEntries([rich])[0].tags, ["Setup required", "Live state", "2 handlers"]);
    assert.deepEqual(agentEntries([{ ...bare, init_data: { required: false, schema }, handlers: [{ name: "ask", description: "" }] }])[0].tags, ["Optional setup"]);
    const body = html({ agents: [rich, bare] });
    assert.deepEqual([...cards(body)[0].matchAll(/<span class="tag[^"]*">([^<]*)<\/span>/g)].map((m) => m[1]), ["Setup required", "Live state", "2 handlers"]);
    assert.doesNotMatch(cards(body)[1], /agent-tags/);
    assert.match(source, /\.agent-tags \{[^}]*text-transform: uppercase;/);
    assert.doesNotMatch(source.match(/\n {2}\.agent-tags \{[^}]*\}/)?.[0] ?? "", /border/, "tags are labels, not boxes");
  });

  it("offers a filter for each capability some agents have and some lack, and combines it with search", () => {
    const agents = fleet(6, (i) => ({
      worker: worker(i < 4 ? "ready" : "no_worker"),
      init_data: i % 2 ? { required: true, schema } : null,
      states: i === 0 ? ["board"] : []
    }));
    const entries = agentEntries(agents);
    const chips = filterChips(entries);
    assert.deepEqual(chips.map((chip) => [chip.id, chip.count]), [["ready", 4], ["no-setup", 3], ["setup-required", 3], ["live-state", 1]]);
    const names = (query, active) => filterAgents(entries, query, active).map((entry) => entry.name);
    assert.deepEqual(names("", ["ready"]), ["Agent 0", "Agent 1", "Agent 2", "Agent 3"]);
    assert.deepEqual(names("", ["ready", "setup-required"]), ["Agent 1", "Agent 3"], "groups narrow");
    assert.deepEqual(names("", ["no-setup", "setup-required"]).length, 6, "alternatives in one group widen");
    assert.deepEqual(names("task 3", ["ready", "setup-required"]), ["Agent 3"], "search and filters combine");
    assert.deepEqual(names("flights", ["no-setup"]), [], "Agent 1 needs setup");

    const body = html({ agents });
    const group = body.match(/<div class="filters[^"]*" role="group" aria-label="Filter agents">[\s\S]*?<\/div>/)?.[0] ?? "";
    assert.match(group, /<button[^>]*aria-pressed="false"[^>]*class="chip xs quiet[^"]*"[^>]*>\s*Ready now<span class="chip-count[^"]*">4<\/span>/);
    assert.doesNotMatch(html({ agents: fleet(3) }), /Filter agents/, "nothing to tell apart, nothing to filter");
  });

  it("filters by name, plain-text description and workflow type", () => {
    const entries = agentEntries(fleet(40));
    const names = (query) => filterAgents(entries, query).map((entry) => entry.name);
    assert.deepEqual(names("  FLIGHTS and "), ["Agent 1"]);
    assert.deepEqual(names("**flights**"), []);
    assert.deepEqual(names("agent39workflow"), ["Agent 39"]);
    assert.equal(names("").length, 40);
    assert.match(source, /filtered\.length < agents\.length \? `\$\{filtered\.length\} of \$\{agents\.length\}`/);
    assert.match(source, /<EmptyState title="No agents match">/);
  });

  it("says a worker's state only when it is not ready, and never READY", () => {
    const status = ["ready", "no_worker", "unknown"];
    const agents = fleet(3, (i) => ({ worker: worker(status[i]) }));
    const shown = cards(html({ agents }));
    assert.doesNotMatch(shown[0], /Ready|No worker|Unknown/i);
    assert.match(shown[1], /No worker/);
    assert.match(shown[2], /Unknown/);
    assert.doesNotMatch(html({ agents: fleet(3) }), />\s*Ready\s*</i);
    const checking = cards(html({ agents, refreshing: true }));
    assert.doesNotMatch(checking[0], /Checking/);
    assert.match(checking[1], /Checking…/);
  });

  it("selects with the shared tint and keeps the card's own hairline, with no radio or check", () => {
    const body = html({ agents: fleet(3), preferredAgentType: "Agent1Workflow" });
    assert.doesNotMatch(body, /type="radio"/);
    assert.deepEqual(cards(body).map((card) => /aria-selected="true"/.test(card)), [false, true, false]);
    assert.match(source, /\.agent-card\.selected \{\s*background: var\(--selected-bg\);\s*\}/);
    assert.match(source, /\.agent-card \{[^}]*border: 1px solid var\(--border\);[^}]*border-radius: 0;/);
  });

  it("shows the selected agent's handlers, state and description beside the grid", () => {
    const agents = fleet(2, (i) =>
      i === 1 ? { handlers: [{ name: "play", description: "Play your mark on an **empty** cell." }, { name: "new_game", description: "" }], states: ["board"] } : {}
    );
    const body = html({ agents, preferredAgentType: "Agent1Workflow" });
    const detail = body.slice(body.indexOf('<section class="detail'));
    assert.match(detail, /Accepts/);
    assert.match(detail, /<code class="handler-name[^"]*"[^>]*>play<\/code>\s*<span class="handler-description[^"]*"[^>]*>Play your mark on an empty cell\.<\/span>/);
    assert.match(detail, /<code class="handler-name[^"]*"[^>]*>new_game<\/code>/);
    assert.match(detail, /Publishes state[\s\S]*<code[^>]*>board<\/code>/);
    assert.match(detail, /class="detail-description[^"]*"[^>]*><div class="markdown-message compact[\s\S]*<strong>flights<\/strong>/);
    const without = html({ agents, preferredAgentType: "Agent0Workflow" });
    assert.doesNotMatch(without, /Accepts|Publishes state/, "nothing invented when the registry is silent");
  });

  it("moves through the grid in two dimensions", () => {
    /* 3 columns, 8 cards: rows [0 1 2] [3 4 5] [6 7]. */
    assert.equal(gridMove("ArrowRight", 0, 8, 3), 1);
    assert.equal(gridMove("ArrowRight", 7, 8, 3), 7);
    assert.equal(gridMove("ArrowLeft", 3, 8, 3), 2);
    assert.equal(gridMove("ArrowDown", 1, 8, 3), 4);
    assert.equal(gridMove("ArrowDown", 5, 8, 3), 7, "onto the short last row's last card");
    assert.equal(gridMove("ArrowDown", 6, 8, 3), 6);
    assert.equal(gridMove("ArrowUp", 4, 8, 3), 1);
    assert.equal(gridMove("ArrowUp", 1, 8, 3), 1);
    assert.equal(gridMove("Home", 5, 8, 3), 0);
    assert.equal(gridMove("End", 0, 8, 3), 7);
    assert.equal(gridMove("ArrowLeft", 0, 8, 3, true), 1, "Left reads forward on an RTL page");
    assert.equal(gridMove("ArrowDown", -1, 8, 3), 0);
    assert.equal(gridMove("a", 0, 8, 3), null);
    assert.equal(gridMove("ArrowDown", 0, 0, 3), null);
    assert.deepEqual([150, 200, 420, 700, 2000].map(columnsFor), [1, 1, 2, 3, 3]);
  });

  it("wires the grid's and the search's keys, and starts on Enter from either", () => {
    const handler = source.slice(source.indexOf("function handleKey"), source.indexOf("function close"));
    assert.match(handler, /event\.key === "Enter"\) \{\s*event\.preventDefault\(\);\s*void start\(\);/);
    assert.match(handler, /inSearch && \["ArrowLeft", "ArrowRight", "Home", "End"\]\.includes\(event\.key\)/);
    assert.match(handler, /gridMove\(event\.key, selectedIndex, filtered\.length, columns, rtl\)/);
    assert.match(handler, /searchInput\?\.focus\(\)/);
    assert.match(source, /role="listbox"[\s\S]*onkeydown=\{\(event\) => handleKey\(event, false\)\}/);
    assert.match(source, /aria-label="Search agents"[\s\S]*onkeydown=\{\(event\) => handleKey\(event, true\)\}/);
    assert.match(source, /event\.key === "Escape" && query[\s\S]*query = ""/);
    const body = html({ agents: fleet(3), preferredAgentType: "Agent1Workflow" });
    const shown = cards(body);
    assert.match(shown[1], /tabindex="0"/);
    assert.match(shown[0], /tabindex="-1"/);
    assert.match(shown[1], /aria-setsize="3"[^>]*aria-posinset="2"/);
  });

  it("windows the grid by rows once it holds more than fit", () => {
    assert.equal(cards(html({ agents: fleet(11) })).length, 11);
    const body = html({ agents: fleet(300) });
    const drawn = cards(body).length;
    assert.ok(drawn > 0 && drawn < 30, `drew ${drawn} of 300 cards`);
    assert.match(body, /aria-setsize="300"/);
    assert.match(body, /300 agents/);
  });

  it("ends on Cancel and Start session chips, the consequential one last", () => {
    const body = html({ agents: fleet(2) });
    const footer = body.slice(body.indexOf('<footer class="detail-actions'));
    const cancel = footer.search(/>\s*Cancel</);
    const start = footer.search(/>\s*Start session</);
    assert.ok(cancel > -1 && start > cancel);
    assert.match(footer, /<button[^>]*class="chip sm quiet[^"]*"[^>]*>\s*Cancel<\/button>/);
    assert.match(footer, /<button[^>]*class="chip sm filled accent[^"]*"[^>]*type="submit"/);
    assert.match(body, /<button[^>]*class="head-button"[^>]*aria-label="Close"/);
  });
});
