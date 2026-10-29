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

  it("leaves a lone folded pane as one spine", () => {
    const stack = createPaneStack();
    stack.toggleCollapse(stack.openPane({ kind: "logs" }));
    assert.deepEqual(spines(stack), ["Replay log / Logs"]);
  });
});
