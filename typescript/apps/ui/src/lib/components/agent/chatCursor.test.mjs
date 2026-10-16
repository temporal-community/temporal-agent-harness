// ABOUTME: Pins the chat pane reading the run as of the replay cursor, like Logs, State and
// Decisions — and pins what must NOT rewind with it. At the live head the chat has to be exactly
// what it was before it could rewind: same arrays in, same markup out. Scrubbed back, it shows the
// conversation up to the cursor and says so, but a pending approval is live state — the agent is
// blocked on it now — so it stays on screen and answerable, marked as ahead of the cursor, and
// the composer still sends to the live run. The Code Mode nesting and the folded thought are
// checked at a past cursor through the same replay-log build the chat is handed.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface, stripComments } from "../../../../tests/support/controllerHarness.mjs";
import { realisticQaScenario } from "$lib/mock/scenarios.ts";
import { AgentRunController } from "$lib/state/agentRun.svelte.ts";
import { codeModeHostsByRow } from "$lib/state/codeModeNesting.ts";
import { buildReplayLog } from "$lib/state/replayLog.ts";
import { foldTurnThought } from "$lib/state/thoughtSummary.ts";
import AgentChatPanel from "./AgentChatPanel.svelte";

const common = {
  agentLabel: "Planner",
  sessionId: "wf-root",
  sending: true,
  onSend: async () => {},
  agentInterface: [
    {
      name: "ask",
      parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      mid_turn: "enqueue"
    }
  ]
};
const composerOf = (body) => body.match(/<input[^>]*aria-label="Message[^"]*"[^>]*>/)?.[0];
const html = (props) => stripComments(render(AgentChatPanel, { props: { ...common, ...props } }).body);

describe("the chat pane and the replay cursor", () => {
  let run;

  beforeAll(() => {
    installBrowserSurface();
    run = new AgentRunController(new Proxy({}, { get: () => async () => [] }));
    run.sessions = realisticQaScenario.sessions;
    run.session = realisticQaScenario.sessions[0];
    run.frames = realisticQaScenario.frames;
  });

  it("is exactly what it was before, at the live head", () => {
    run.goTo(run.total);
    const view = run.chatView;
    assert.equal(view.live, true);
    /* Deep, not identity: under test the runes compile for the server, where a $derived
       recomputes on every read. */
    assert.deepEqual(view.items, run.chatTranscript);
    assert.deepEqual(view.logs, run.fullReplayLog.rows);

    /* Raw, hydration markers and all: the block structure must not change either. */
    const raw = (props) => render(AgentChatPanel, { props: { ...common, ...props } }).body;
    const before = raw({ items: run.chatTranscript, logs: run.fullReplayLog.rows });
    const after = raw({ ...view, onJumpToLive: () => {} });
    assert.equal(after, before);
    assert.match(after, /bubble thinking/, "the reply arriving still shows at the live head");
    assert.doesNotMatch(after, /replay-strip/);
  });

  it("shows the conversation as of the cursor when scrubbed back, and says so", () => {
    run.goTo(run.total);
    const liveBody = html({ ...run.chatView });
    const liveCount = (liveBody.match(/class="message /g) ?? []).length;
    const firstReply = run.replyRuns[0];
    run.goTo(firstReply.startIndex);
    const view = run.chatView;
    const body = html({ ...view, onJumpToLive: () => {} });

    assert.equal(view.live, false);
    assert.ok(
      (body.match(/class="message /g) ?? []).length < liveCount,
      "messages after the cursor are not shown"
    );
    const strip = body.match(/<div class="replay-strip[\s\S]*?<\/div>/)?.[0] ?? "";
    assert.match(
      strip,
      new RegExp(`Replay\\s·\\sevent ${firstReply.startIndex} / ${run.total}`),
      "the strip reads the cursor the way the transport counts it"
    );
    assert.match(strip, /aria-label="Jump to latest step"[^>]*>\s*Latest/);
    assert.doesNotMatch(body, /bubble thinking/, "no live 'thinking' dots under a past view");
    const composer = composerOf(body);
    assert.ok(composer, "the composer is still there");
    assert.doesNotMatch(composer, /disabled/, "scrubbing back does not disable the composer");
    assert.equal(composer, composerOf(liveBody), "and it is the live composer, unchanged");
    run.goTo(run.total);
  });
});

/* One turn: think, then run a Code Mode host that calls two children — one of them
   published straight at tool_start — then gate a call on a human. */
const frame = (event, timestamp, data = {}) => ({
  event,
  data: { type: event, turn_number: 1, turn_id: "turn-1", timestamp, ...data }
});
const SCRIPT = { script: "await search('x')" };
const FRAMES = [
  frame("turn_started", 100, { user_message: "go" }),
  frame("model_interaction_started", 100, { model: "gpt" }),
  frame("thought_summary", 101, { delta: { delta: "Weighing " } }),
  frame("thought_summary", 103, { delta: { delta: "options." } }),
  frame("model_interaction_ended", 104, { model: "gpt" }),
  frame("tool_requested", 105, { tool_id: "host", tool_name: "run_temporal_code", tool_input: SCRIPT }),
  frame("tool_start", 105, { tool_id: "host", tool_name: "run_temporal_code", tool_input: SCRIPT }),
  frame("tool_requested", 106, { tool_id: "child-a", tool_name: "search", tool_input: { q: "x" } }),
  frame("tool_start", 106, { tool_id: "child-a", tool_name: "search" }),
  frame("tool_start", 107, { tool_id: "child-b", tool_name: "fetch" }),
  frame("tool_end", 108, { tool_id: "child-b", tool_name: "fetch", tool_output: "ok" }),
  frame("tool_end", 109, { tool_id: "host", tool_name: "run_temporal_code", tool_output: "ok" }),
  frame("tool_approval_requested", 110, { tool_id: "gate", tool_name: "refund", tool_input: {} })
];
const rowsAt = (cursor) => buildReplayLog(FRAMES.slice(0, cursor)).rows;
const toolIdsNestedAt = (cursor) => {
  const rows = rowsAt(cursor);
  const hosts = codeModeHostsByRow(rows);
  return rows.filter((row) => hosts.has(row.id)).map((row) => `${row.event}:${row.toolId}`);
};

describe("chat activity at a past cursor", () => {
  it("nests Code Mode children as of the cursor", () => {
    /* Parked just after child-b arrived with no tool_requested: both children are under
       the host already, and nothing past the cursor is. */
    assert.deepEqual(toolIdsNestedAt(10), [
      "tool_requested:child-a",
      "tool_start:child-a",
      "tool_start:child-b"
    ]);
    assert.deepEqual(toolIdsNestedAt(7), [], "before any child, nothing is nested");
    assert.deepEqual(toolIdsNestedAt(FRAMES.length).length, 4, "the live head adds child-b's end");
  });

  it("reads the thought's duration and state as of the cursor", () => {
    const midThought = foldTurnThought(rowsAt(3));
    assert.equal(midThought.seconds, 1, "model call opened at 100, last thought so far at 101");
    assert.equal(midThought.thinking, true, "the call is still open at this cursor");

    const settled = foldTurnThought(rowsAt(FRAMES.length));
    assert.equal(settled.seconds, 3);
    assert.equal(settled.thinking, false);
    assert.equal(settled.text, "Weighing options.");
  });

  it("keeps a live pending approval answerable, marked as ahead of the cursor", () => {
    const liveLogs = rowsAt(FRAMES.length);
    const body = html({
      items: [],
      logs: rowsAt(5),
      liveItems: [],
      liveLogs,
      live: false,
      onApproveTool: async () => {}
    });
    assert.match(body, /1 approval needed/i);
    assert.match(body, /refund/);
    assert.match(body, /Requested ahead of the replay cursor/);

    const atHead = html({ items: [], logs: liveLogs, onApproveTool: async () => {} });
    assert.match(atHead, /1 approval needed/i);
    assert.doesNotMatch(atHead, /ahead of the replay cursor/, "at the head nothing is ahead");
  });
});

describe("the turn card under a moving cursor", () => {
  /* Read from source: remounting and `in:` transitions only exist in a mounted DOM, which
     these server renders never build. */
  const source = readFileSync(new URL("./AgentChatPanel.svelte", import.meta.url), "utf8");

  const feed = source.match(/<div class=\{`activity-feed[\s\S]*?turn-summary[\s\S]*?<\/button>/)?.[0] ?? "";

  it("updates in place instead of remounting on every step", () => {
    assert.match(feed, /turn-summary/, "the turn card is present");
    assert.doesNotMatch(feed, /\{#key/);
  });

  it("never fades, live or replayed", () => {
    assert.doesNotMatch(feed, /\s(in|transition):/);
  });
});

describe("the chat footer", () => {
  const twoHandlers = [
    ...common.agentInterface,
    { name: "cancel", parameters: common.agentInterface[0].parameters, mid_turn: "accept" }
  ];

  it("is one toolbar of chips, then the input — and no search glyph on a message box", () => {
    const body = html({ items: [], agentInterface: twoHandlers, onStopAgent: async () => {} });
    const toolbar = body.match(/<div class="composer-toolbar[\s\S]*?<form/)?.[0] ?? "";
    assert.match(toolbar, /class="chip sm quiet model toned interactive[^"]*"[^>]*>[\s\S]*?ask/);
    assert.match(toolbar, /class="chip sm quiet neutral[^"]*">\s*<span class="chip-label[^"]*">queues/);
    assert.match(toolbar, /class="chip sm quiet error toned interactive stop-chip[^"]*"/);
    assert.match(toolbar, /aria-label="Stop Planner"[\s\S]*?Stop\s*<\/button>/);
    assert.doesNotMatch(body, /lucide-search/);
    assert.doesNotMatch(body, /class="composer-actions/, "Stop is no longer a row of its own");
  });

  it("keeps Stop in the toolbar when there is nothing to choose between", () => {
    const body = html({ items: [], onStopAgent: async () => {} });
    const toolbar = body.match(/<div class="composer-toolbar[\s\S]*?<form/)?.[0] ?? "";
    assert.match(toolbar, /stop-chip/);
    assert.doesNotMatch(toolbar, /queues/, "a single-handler agent still looks like a chat box");
  });
});

describe("the composer's handler surface", () => {
  const ask = common.agentInterface;
  /* What App hands the pane: the cursor-scoped view plus the live message targets. */
  const paneOf = (controller) =>
    html({
      ...controller.chatView,
      agentInterface: undefined,
      messageTargets: controller.messageTargets,
      agentLabel: controller.runInfo.agentLabel,
      sessionId: controller.runInfo.sessionId,
      onStopAgent: async () => {},
      onRetryInterface: () => {}
    });
  const controllerWith = (agentInterface) => {
    const controller = new AgentRunController(
      new Proxy({}, { get: (_, key) => (key === "agentInterface" ? agentInterface : async () => []) })
    );
    controller.sessions = realisticQaScenario.sessions;
    controller.session = realisticQaScenario.sessions[0];
    controller.frames = realisticQaScenario.frames;
    return controller;
  };

  it("comes from the live run however far back the cursor is", () => {
    const controller = controllerWith(async () => ask);
    controller.agentInterfaces = { [controller.session.workflow_id]: ask };
    controller.goTo(1);
    assert.equal(controller.chatView.live, false);
    const composer = composerOf(paneOf(controller));
    assert.doesNotMatch(composer, /disabled/);
    assert.match(composer, /placeholder="text → /i, "addressed to the live handler");
  });

  it("says a failed lookup failed, offers Retry, and recovers when it succeeds", async () => {
    let workerUp = false;
    const controller = controllerWith(async () => {
      if (!workerUp) throw new Error("query timed out");
      return ask;
    });
    const id = controller.session.workflow_id;
    await controller.retryAgentInterface(id);
    assert.equal(controller.messageTargets[0].interfaceStatus, "failed");
    const failed = paneOf(controller);
    assert.match(composerOf(failed), /placeholder="Couldn't load the messages [^"]+ accepts"/);
    assert.doesNotMatch(failed, /declares no messages/, "a failure is not an agent with none");
    assert.match(failed, /<button[^>]*title="Ask the agent for the messages it accepts again"/);

    workerUp = true;
    await controller.retryAgentInterface(id);
    assert.equal(controller.messageTargets[0].interfaceStatus, "loaded");
    const recovered = paneOf(controller);
    assert.doesNotMatch(composerOf(recovered), /disabled/);
    assert.doesNotMatch(recovered, /messages it accepts again/, "Retry goes once it worked");
  });

  it("says it is loading until the lookup answers, and 'none' only once it has", async () => {
    const pending = controllerWith(() => new Promise(() => {}));
    void pending.retryAgentInterface(pending.session.workflow_id);
    assert.match(composerOf(paneOf(pending)), /placeholder="Loading the messages [^"]+ accepts…"/);

    const none = controllerWith(async () => []);
    await none.retryAgentInterface(none.session.workflow_id);
    assert.match(composerOf(paneOf(none)), /placeholder="This agent declares no messages"/);
  });
});
