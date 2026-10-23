// ABOUTME: The graph's result box tails text only while it streams. It used to tail every
// change, so a finished, deeply nested reply opened on its last lines — a staircase of closing
// braces — instead of on what the handler said.
// Also the card's anatomy: a glyph only where it is the state, the status as a word rather
// than a box, and an empty reply said in words rather than drawn as an empty box.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it, vi } from "vitest";

import AgentStateNode, { resultScrollTop, setNodeOpener } from "./AgentStateNode.svelte";
import AgentWorkflowNode from "./AgentWorkflowNode.svelte";
import { buildAgentGraph } from "$lib/state/flowProjection.ts";

/* Handle reads a live SvelteFlow canvas from context; nothing here needs one, so it is stubbed,
   keeping the props it was given for the port checks. */
const handles = vi.hoisted(() => []);
vi.mock("@xyflow/svelte", () => ({
  Handle: (_renderer, props) => handles.push(props),
  Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" }
}));

function card(data, { opener = false } = {}) {
  const Component = opener
    ? (...args) => {
        setNodeOpener(() => {});
        return AgentStateNode(...args);
      }
    : AgentStateNode;
  return render(Component, { props: { id: "input", data } }).body;
}

function head(html, name) {
  const match = html.match(new RegExp(`class="${name}\\b[^"]*"([\\s\\S]*?)class="(body|workflow-title-group)\\b`));
  assert.ok(match, `no ${name} in the rendered card`);
  return match[1];
}

const svgCount = (html) => (html.match(/<svg\b/g) ?? []).length;

describe("the result box", () => {
  const box = { scrollHeight: 900 };

  it("opens a finished reply at its top", () => {
    assert.equal(resultScrollTop(box, false, true), 0);
  });

  it("follows a stream's tail while the reader is at the end", () => {
    assert.equal(resultScrollTop(box, true, true), 900);
  });

  it("leaves a reader who scrolled back mid-stream where they are", () => {
    assert.equal(resultScrollTop(box, true, false), null);
  });
});

describe("a card's header carries no decoration", () => {
  // The Input card wore "⊙ TURN 4": a glyph that said nothing, in a box that looked
  // pressable. Glyphs are for state a reader acts on, so an idle card has none.
  it("draws no glyph for a state that is only a label", () => {
    const html = card({ tone: "neutral", title: "Input", state: "turn 4", subtitle: "user message", detail: "hi" });
    assert.equal(svgCount(head(html, "head")), 0);
  });

  it("leads the title with one glyph where the glyph is the state", () => {
    for (const state of ["awaiting approval", "done", "failed", "running"]) {
      const header = head(card({ tone: "tool", title: "run_script", state, detail: "" }), "head");
      assert.equal(svgCount(header), 1, state);
      assert.ok(header.indexOf("<svg") < header.indexOf("run_script"), `${state}: the glyph must come before the title`);
      assert.equal(svgCount(header.slice(header.indexOf("run_script"))), 0, `${state}: the status word carries no glyph`);
    }
  });

  it("keeps an idle runtime group neutral and its header to words", () => {
    const html = render(AgentWorkflowNode, {
      props: { data: { tone: "agent", title: "Agent runtime", state: "idle", subtitle: "model calls" } }
    }).body;
    // Green means something finished; a runtime waiting for its first message has not.
    assert.match(html, /class="workflow-boundary[^"]*\bstatus-neutral\b/);
    assert.equal(svgCount(head(html, "workflow-head")), 0);
  });
});

describe("a runtime boundary whose detail is missing", () => {
  // The reason lived in a hover tip on a span nothing could focus, so a keyboard, a
  // touch screen and a screen reader never got it.
  it("says why on the card itself, not in a tip", () => {
    const reason = "Stalled without delivering its turn";
    const html = render(AgentWorkflowNode, {
      props: { data: { tone: "neutral", title: "task subagent runtime", state: "closed", reason } }
    }).body;
    assert.match(html, new RegExp(`class="workflow-reason[^"]*"[^>]*>${reason}<`));
    assert.doesNotMatch(html, /data-tip="[^"]*Stalled/);
    assert.match(html, /class="workflow-boundary[^"]*\bstatus-neutral\b/, "closed is neutral, not red");
  });
});

describe("a status is a word, not a control", () => {
  // A hairline box means "press me" (typescript/apps/ui/AGENTS.md); the band already carries the tone.
  it("renders the status as a quiet, non-interactive chip", () => {
    const status = head(card({ tone: "neutral", title: "Input", state: "turn 4" }), "head");
    assert.match(status, /<span [^>]*class="chip\b[^"]*\bquiet\b/);
    assert.doesNotMatch(status, /\bfilled\b|\binteractive\b|<button/);
  });
});

describe("the card's ports", () => {
  it("light only the handles an edge uses, in its color, and none can be grabbed or announced", () => {
    handles.length = 0;
    card({ tone: "tool", title: "run_script", state: "awaiting", ports: { "target-left": "var(--live)" } });
    assert.equal(handles.length, 6, "three target and three source handles");
    for (const props of handles) {
      assert.equal(props.isConnectable, false, props.id);
      assert.equal(props["aria-hidden"], true, props.id);
      const lit = props.id === "target-left";
      assert.equal(props.class, lit ? "node-handle port" : "node-handle", props.id);
      assert.equal(props.style, lit ? "--port-color: var(--live)" : undefined, props.id);
    }
  });
});

describe("a long title", () => {
  it("is offered whole in a tip", () => {
    const title = `fetch_krakow_emea_shared_services_export_${"with_retries_".repeat(8)}`;
    assert.match(card({ tone: "tool", title, state: "awaiting" }), new RegExp(`data-tip="${title}"`));
  });
});

describe("an empty reply", () => {
  // An empty bordered scroller read as a broken textarea.
  it("says so in muted text instead of drawing an empty box", () => {
    const html = card({ tone: "neutral", title: "Output", state: "idle", detail: "" });
    assert.match(html, /class="result-empty[^"]*">No result yet</);
    assert.doesNotMatch(html, /result-body/);
  });
});

describe("a reply on the Output card", () => {
  const frame = (event, data) => ({ event, data: { type: event, ...data } });
  const outputCard = (frames) => {
    const node = buildAgentGraph([frame("turn_started", { user_message: "weather?", turn_number: 1 }), ...frames]).nodes.find(
      (n) => n.id === "output"
    );
    assert.ok(node, "the fixture should produce an Output node");
    return card(node.data);
  };

  // It showed "- **Berlin**: 72°F" with its asterisks, where the chat drew a list.
  it("is drawn by the chat's markdown renderer when it is prose", () => {
    const text = "- **Berlin**: 72°F\n- `Paris`: 68°F\n\n<script>alert(1)</script>";
    const html = outputCard([frame("reply_delta", { text }), frame("message_handler_end", { output: { text } })]);
    assert.match(html, /class="markdown-message/);
    assert.match(html, /<ul>[\s\S]*<strong>Berlin<\/strong>/);
    assert.match(html, /<code>Paris<\/code>/);
    assert.doesNotMatch(html, /<script/);
    assert.match(html, /&lt;script>alert\(1\)&lt;\/script>|&lt;script&gt;/);
  });

  it("stays a plain preview when the handler returned data", () => {
    const output = { city: "Berlin", note: "**not markdown**" };
    const html = outputCard([frame("reply_delta", { text: JSON.stringify(output) }), frame("message_handler_end", { output })]);
    assert.doesNotMatch(html, /markdown-message|<strong>/);
    assert.match(html, /&quot;city&quot;: &quot;Berlin&quot;|"city": "Berlin"/);
  });
});

describe("a card's actions", () => {
  it("are named icon buttons, present only where the canvas can open the card", () => {
    const data = { tone: "neutral", title: "Input", state: "turn 4", detail: "hi" };
    assert.doesNotMatch(card(data), /icon-button/);
    const html = card(data, { opener: true });
    assert.match(html, /<button[^>]*aria-label="Open Input details"[^>]*data-tip="Open details"/);
  });
});
