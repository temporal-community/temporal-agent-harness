// ABOUTME: Pins focus and keyboard reach in the windowed lists — chat, Logs, and the session
// picker draw only the rows in view, so a focused row scrolled away left the DOM and focus fell
// to <body>, and the keyboard could only reach the rows already drawn. Also pins that chat and
// Logs say what they are: a feed of articles, each with its place in the whole list.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import AgentChatPanel from "../agent/AgentChatPanel.svelte";
import TranscriptPanel from "../agent/TranscriptPanel.svelte";
import { scaleScenario } from "$lib/mock/scaleScenarios.ts";
import { buildReplayLog } from "$lib/state/replayLog.ts";
import { buildTranscript } from "$lib/state/transcript.ts";
import { windowedFocus } from "./windowedFocus.ts";

let mutated = () => {};
globalThis.MutationObserver = class {
  constructor(callback) {
    mutated = callback;
  }
  observe() {}
  disconnect() {}
};
globalThis.requestAnimationFrame = (fn) => fn();
const body = { name: "body" };
globalThis.document = { body, activeElement: body };

/** A scroller drawing rows `[data-index]`; `draw` replaces the window, as a scroll does. */
function fakeList() {
  const handlers = {};
  let rows = new Map();
  const list = {
    name: "list",
    isConnected: true,
    closest: () => null,
    addEventListener: (type, fn) => (handlers[type] = fn),
    removeEventListener: (type) => delete handlers[type],
    querySelector: (selector) => rows.get(Number(/\d+/.exec(selector)[0])) ?? null,
    querySelectorAll: () => [...rows.values()],
    contains: (node) => node === list || [...rows.values()].includes(node),
    getBoundingClientRect: () => ({ top: 0 }),
    focus() {
      document.activeElement = list;
      handlers.focusin?.({ target: list });
    },
    draw(indexes) {
      for (const node of rows.values()) node.isConnected = false;
      rows = new Map(indexes.map((index) => [index, row(index)]));
      if (!list.contains(document.activeElement) && document.activeElement !== list && document.activeElement.isConnected === false) {
        document.activeElement = body;
      }
      mutated();
    },
    handlers
  };
  const row = (index) => {
    const node = {
      name: `row ${index}`,
      isConnected: true,
      closest: () => node,
      getAttribute: () => String(index),
      getBoundingClientRect: () => ({ bottom: index + 1 }),
      focus() {
        document.activeElement = node;
        handlers.focusin?.({ target: node });
      }
    };
    return node;
  };
  list.row = (index) => rows.get(index);
  return list;
}

describe("a windowed list", () => {
  it("hands focus to the list when the focused row scrolls out of the window", () => {
    const list = fakeList();
    windowedFocus()(list);
    list.draw([3, 4, 5, 6, 7]);
    list.row(5).focus();
    list.draw([40, 41, 42]);
    assert.equal(document.activeElement, list);
  });

  it("gives focus back to the row redrawn at the same index", () => {
    const list = fakeList();
    windowedFocus()(list);
    list.draw([3, 4, 5]);
    list.row(4).focus();
    list.draw([3, 4, 5]);
    assert.equal(document.activeElement, list.row(4));
  });

  it("leaves focus alone when the reader moved it out themselves", () => {
    const list = fakeList();
    windowedFocus()(list);
    list.draw([3, 4, 5]);
    list.row(4).focus();
    document.activeElement = body;
    mutated();
    assert.equal(document.activeElement, body);
  });

  it("reaches an undrawn row with PageDown, and stops at the ends", () => {
    const list = fakeList();
    const scrolled = [];
    windowedFocus((index) => {
      scrolled.push(index);
      list.draw([index - 1, index]);
    }, () => 10)(list);
    list.draw([7, 8]);
    list.row(8).focus();
    const key = (k) => list.handlers.keydown({ key: k, preventDefault() {} });

    key("PageDown");
    assert.equal(document.activeElement, list.row(9));
    key("PageDown");
    assert.deepEqual(scrolled, [9, 9]);
    key("PageUp");
    assert.equal(document.activeElement, list.row(8));
  });

  it("leaves PageDown to a field or a scroller inside a row", () => {
    const list = fakeList();
    const scrolled = [];
    windowedFocus((index) => scrolled.push(index), () => 10)(list);
    list.draw([3, 4]);
    list.row(3).focus();
    const key = (target) => list.handlers.keydown({ key: "PageDown", target, preventDefault() {} });
    globalThis.getComputedStyle = () => ({ overflowY: "auto" });

    key({ tagName: "TEXTAREA", parentElement: list.row(3) });
    key({ tagName: "DIV", scrollHeight: 900, clientHeight: 200, parentElement: list.row(3) });
    assert.deepEqual(scrolled, [], "the field and the inner scroller page themselves");
    key(list.row(3));
    assert.deepEqual(scrolled, [4], "a row pages the list");
  });

  /* The hand-back above, wired into the two side-pane lists that draw only the rows in view. */
  it("hands focus back in the decisions list and the file tree", () => {
    const decisions = readFileSync(new URL("../agent/ApprovalDecisionPanel.svelte", import.meta.url), "utf8");
    assert.match(decisions, /const decisionFocus = windowedFocus\(\s*\(index\) => rowList\?\.scrollToIndex\(index\),\s*\(\) => \w+\.length\s*\)/);
    assert.match(decisions, /<div class="evaluation-items" tabindex="-1" bind:this=\{\w+\} \{@attach decisionFocus\}>/);
    assert.match(decisions, /data-index=\{index\}/, "decision rows carry their index");
    const tree = readFileSync(new URL("../agent/MountTrees.svelte", import.meta.url), "utf8");
    assert.match(tree, /const treeFocus = windowedFocus\(\s*\(index\) => rowList\?\.scrollToIndex\(index\),\s*\(\) => rows\.length,\s*\(key, from\) => treeKey\(rows, from, key\)\s*\)/);
    assert.match(tree, /<div\s+class="mounts"\s+role="tree"[\s\S]*?tabindex=\{drawn\.has\(activeIndex\) \? -1 : 0\}[\s\S]*?\{@attach treeFocus\}/);
    assert.match(tree, /"data-index": index/, "tree rows carry their index");
    /* A delegated onkeydown ran after the list had already moved focus, so Left left instead of closing. */
    assert.match(tree, /onkeydowncapture: \(event: KeyboardEvent\) => rowKey\(event, row\)/, "a row opens and closes before the list moves");
  });

  /* A tree moves with the arrows too, through the same scroll-then-focus path as PageDown. */
  it("moves with a list's own keys, and leaves the rest alone", () => {
    const list = fakeList();
    const scrolled = [];
    const prevented = [];
    windowedFocus(
      (index) => {
        scrolled.push(index);
        list.draw([index]);
      },
      () => 10,
      (key, from) => (key === "ArrowDown" ? from + 1 : key === "End" ? 9 : key === "ArrowLeft" ? null : undefined)
    )(list);
    list.draw([2]);
    list.row(2).focus();
    const key = (k) => list.handlers.keydown({ key: k, preventDefault: () => prevented.push(k) });
    key("ArrowDown");
    key("End");
    key("ArrowLeft");
    key("PageDown");
    assert.deepEqual(scrolled, [3, 9]);
    assert.equal(document.activeElement, list.row(9));
    assert.deepEqual(prevented, ["ArrowDown", "End"], "a key that goes nowhere, or isn't the list's, is left alone");
    list.handlers.keydown({ key: "ArrowDown", shiftKey: true, preventDefault: () => prevented.push("Shift+ArrowDown") });
    assert.deepEqual(scrolled, [3, 9], "a chord is left to whoever owns it");
  });

  /* The run-wide timeline is a treegrid of windowed rows, so it needs the same hand-back and
     reach as the file tree, and its span buttons stay out of the Tab order. */
  it("hands focus back and reaches every row in the timeline", () => {
    const timeline = readFileSync(new URL("../flow/WorkflowTimeline.svelte", import.meta.url), "utf8");
    assert.match(timeline, /const rowFocus = windowedFocus\(\s*\(position\) => windowedRows\?\.scrollToIndex\(position\),\s*\(\) => tree\.indexes\.length,\s*treeKey\s*\)/);
    assert.match(timeline, /class="rows"[\s\S]*?tabindex=\{tabKey != null && !drawn\.has\(tabKey\) \? 0 : -1\}[\s\S]*?\{@attach rowFocus\}/);
    assert.match(timeline, /data-index=\{position\}/, "timeline rows carry their shown position");
    assert.match(timeline, /onkeydowncapture=\{\(event\) => handleRowKeydown\(event, rowIndex\)\}/, "a row folds before the list moves");
    assert.match(timeline, /class=\{markClass\(row\)\}[\s\S]*?tabindex="-1"/, "a span's button is not a Tab stop");
  });

  it("is a feed of articles that know their place in the whole list, in chat and Logs", () => {
    const frames = scaleScenario("transcript5k").frames;
    const items = buildTranscript(frames);
    const messages = items.filter((item) => item.kind === "user" || item.kind === "agent").length;
    const chat = stripComments(
      render(AgentChatPanel, { props: { items, logs: [], agentLabel: "Planner", sessionId: "wf-root" } }).body
    );
    assert.match(chat, /role="feed"/);
    const empty = stripComments(
      render(AgentChatPanel, { props: { items: [], logs: [], agentLabel: "Planner", sessionId: "wf-root" } }).body
    );
    assert.doesNotMatch(empty, /role="feed"/, "an empty chat is no feed");
    assert.match(
      chat,
      new RegExp(`class="message-row[^"]*" role="article" aria-posinset="1" aria-setsize="${messages}" tabindex="-1"`)
    );

    const { groups } = buildReplayLog(frames);
    const logs = stripComments(
      render(TranscriptPanel, { props: { groups, activeTurnNumber: null, activeOrdinal: null } }).body
    );
    assert.match(logs, /class="turns[^"]*" role="feed"/);
    assert.match(
      logs,
      new RegExp(`role="article" aria-label="Turn \\d+" aria-posinset="1" aria-setsize="${groups.length}" tabindex="-1"`)
    );
  });
});
