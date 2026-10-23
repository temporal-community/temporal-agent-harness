// ABOUTME: Asserts the state switch only exists when there is a choice behind it. The tabs switch
// between the documents the agents in a run declared, and most runs declare one — so a run with one
// state drew a lone "PLAN" chip that looked like a tab, a toggle and a label at once. With one state
// there are no tabs and the name is in the reading's kicker; with more, the tabs are a real tablist.
// Also pins the document as LTR and reachable by keyboard: it is a scroller with nothing to focus
// inside it, and an Arabic key renders swapped with its value unless the block is LTR.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import AgentStatePanel, { docLines, ringsOf } from "./AgentStatePanel.svelte";

const source = readFileSync(new URL("./AgentStatePanel.svelte", import.meta.url), "utf8");

const doc = (key, stateId) => ({
  key,
  stateId,
  workflowId: "wf-1",
  label: "planner",
  role: "parent",
  version: 2,
  value: { status: "idle" },
  changed: [],
  commits: 2,
  turnNumber: 1,
  problem: null
});

const html = (states) => render(AgentStatePanel, { props: { states } }).body;

describe("the registered-state tabs", () => {
  it("are not drawn when the run declared one state", () => {
    const one = html([doc("wf-1/plan", "plan")]);

    assert.doesNotMatch(one, /role="tab/, "a lone state name rendered as a tab");
    assert.doesNotMatch(one, /aria-pressed|aria-selected/, "a lone state name claimed to be a control");
    assert.doesNotMatch(one, /class="[^"]*chip/, "a lone state name rendered as a chip");
    assert.match(one, /class="kicker reading-label[^"]*" title="plan · at cursor"[^>]*>plan · at cursor</);
  });

  it("are a real tablist when there is another document to switch to", () => {
    const two = html([doc("wf-1/plan", "plan"), doc("wf-2/notes", "notes")]);

    assert.match(two, /role="tablist"/);
    assert.equal((two.match(/<button[^>]*role="tab"/g) ?? []).length, 2);
    assert.match(two, /aria-selected="true"/);
    assert.match(two, /aria-selected="false"/);
    assert.match(two, /role="tabpanel"/);
  });
});

/* A plan of 3,000 steps printed 12,000 lines, and a commit touching each step 3,000 change rows:
   69,000 nodes in one pane. */
describe("a large state document", () => {
  const steps = Array.from({ length: 3_000 }, (_, i) => ({ name: `step ${i}`, done: true }));
  const value = { goal: "ship", steps, review: { owner: "ana", checks: ["lint"] } };
  const changed = [
    { op: "add", path: "/review", value: value.review },
    ...steps.map((_, i) => ({ op: "replace", path: `/steps/${i}/done`, value: true, before: false }))
  ];
  const big = { ...doc("wf-1/plan", "plan"), value, changed };

  it("draws only the lines and change rows in view", () => {
    const body = html([big]);
    const lines = body.match(/class="line\b/g)?.length ?? 0;
    const rows = body.match(/class="change\b/g)?.length ?? 0;
    assert.ok(lines > 0 && lines <= 60, `${lines} lines drawn of ${docLines(value).length}`);
    assert.ok(rows > 0 && rows <= 60, `${rows} change rows drawn of ${changed.length}`);
    assert.match(body, /role="list" aria-label="What this commit changed"/);
    assert.doesNotMatch(body, /id="agent-state-first-change"/, "the change at the end is not drawn until revealed");
  });

  it("prints a container as its open line, its children and its close line", () => {
    const lines = docLines(value);
    /* The root's two brackets, goal, the steps' brackets and four lines a step, then review:
       its brackets, owner, and checks' brackets around one check. */
    assert.equal(lines.length, 2 + 1 + 2 + 3_000 * 4 + 2 + 1 + 3);
    const review = lines.findIndex((line) => line.pointer === "/review");
    assert.deepEqual(
      lines.slice(review, review + lines[review].lines).map((line) => `${line.kind} ${line.pointer}`),
      ["open /review", "value /review/owner", "open /review/checks", "value /review/checks/0", "close /review/checks", "close /review"]
    );
  });

  it("rings a changed container line by line: top, sides, foot", () => {
    const lines = docLines(value);
    const review = lines.findIndex((line) => line.pointer === "/review");
    const marks = new Map([["/review", "add"]]);
    const edges = lines.slice(review, review + 6).map((line) => ringsOf(line, marks).map((ring) => `${ring.edge}@${ring.depth}`).join());
    assert.deepEqual(edges, ["top@1", "side@1", "side@1", "side@1", "side@1", "foot@1"]);
    assert.deepEqual(ringsOf(lines[review - 1], marks), [], "a line outside the block carries none");
  });

  it("reveals the first change through the window before following it", () => {
    assert.match(source, /follower\.to\(FIRST_CHANGE_ID, \(\) => \{[\s\S]*?docRows\?\.scrollToIndex\(/);
  });
});

describe("the state document", () => {
  it("is LTR, keyboard-scrollable and copyable", () => {
    const body = html([doc("wf-1/plan", "plan")]);
    const docTag = body.match(/<div[^>]*class="doc[^"]*"[^>]*>/)?.[0] ?? "";

    assert.match(docTag, /dir="ltr"/);
    assert.match(docTag, /role="region"/);
    assert.match(docTag, /aria-label="State document"/);
    assert.match(docTag, /tabindex="0"/);
    assert.match(body, /aria-label="Copy state"/);
  });
});
