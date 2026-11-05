// ABOUTME: A folded column holding several panes shows one spine per pane, stacked, so each keeps
// its own title and kind rather than the front pane speaking for the whole column. Tabbed columns
// fold the same way as split ones, and a lone folded pane is unchanged.

import assert from "node:assert/strict";
import { createRawSnippet } from "svelte";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { createPaneStack } from "../state/paneStack.svelte.ts";
import PaneRail from "./PaneRail.svelte";

const TITLES = { logs: "Replay log", latency: "Latency waterfall" };

const spines = (stack) => {
  const body = render(PaneRail, {
    props: {
      stack,
      describe: (pane) => ({ title: TITLES[pane.kind] ?? pane.kind }),
      paneContent: createRawSnippet(() => ({ render: () => "<div></div>" }))
    }
  }).body;
  return [...body.matchAll(/class="spine-title[^"]*">([^<]*)<\/span>\s*<span class="spine-kind[^"]*"[^>]*>([^<]*)</g)]
    .map(([, title, kind]) => `${title} / ${kind}`);
};

const foldedPair = (split) => {
  const stack = createPaneStack();
  const logs = stack.openPane({ kind: "logs" });
  const latency = stack.openPane({ kind: "latency" });
  stack.placePane(latency, logs, "below");
  if (!split) stack.setSplit(logs, false);
  stack.toggleCollapse(logs);
  return stack;
};

describe("a folded column", () => {
  it("stacks a spine for each pane of a split column, each with its own title and kind", () => {
    assert.deepEqual(spines(foldedPair(true)).sort(), ["Latency waterfall / Latency", "Replay log / Logs"]);
  });

  it("does the same for a tabbed column, not just the tab in front", () => {
    assert.equal(spines(foldedPair(false)).length, 2);
  });

  it("leaves a lone folded pane as one spine, as the default desk's logs and latency are", () => {
    assert.deepEqual(spines(createPaneStack()), ["Replay log / Logs", "Latency waterfall / Latency"]);
  });
});

/* With every column folded nothing stretches over the rest of the rail, which was left blank
   with no way back but a spine. */
describe("a desk with every pane folded", () => {
  const body = (stack) =>
    render(PaneRail, {
      props: {
        stack,
        describe: (pane) => ({ title: TITLES[pane.kind] ?? pane.kind }),
        paneContent: createRawSnippet(() => ({ render: () => "<div></div>" }))
      }
    }).body;
  const chips = (html) =>
    [...html.slice(html.indexOf('class="rail-folded')).matchAll(/class="chip-label[^"]*">([^<]*)</g)].map(
      ([, label]) => label
    );

  /* The panes are still on the desk, so it says so beside their spines rather than
     offering the empty desk's open space, and the spines stay the way to open one. */
  it("notes beside the spines that everything is folded, with one way to unfold it all", () => {
    const stack = createPaneStack();
    stack.foldAll(true);
    const html = body(stack);
    assert.match(html, /All folded/);
    assert.doesNotMatch(html, /Open space/, "the panes are not gone");
    assert.deepEqual(chips(html), ["Unfold all"]);
    assert.match(html, /class="chip xs quiet/, "a control like the others, not a call to action");
    assert.equal(spines(stack).length, stack.panes.length, "the spines stay where they are");
  });

  it("is not shown while any pane is open", () => {
    assert.doesNotMatch(body(createPaneStack()), /rail-folded/);
  });
});
