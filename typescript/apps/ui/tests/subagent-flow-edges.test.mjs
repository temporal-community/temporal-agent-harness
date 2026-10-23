// ABOUTME: Asserts that a subagent's own flow arrows are drawn AND can be seen. The projection has
// always emitted them — `wf-a::flow-input-model` and `wf-a::flow-model-output` between a subagent's
// Input, Model interaction and Output — but they were invisible in the canvas, which reads exactly
// like a subagent whose interior was deliberately left unconnected. Two separate things buried them,
// so both are pinned here: a stacking order that put an embedded graph's edges under the very card
// they are embedded in, and a `z-index` on `.svelte-flow__edges` that flattened every edge in the
// canvas onto one rung and made per-edge z-index meaningless. Measured in a browser: with either one
// back, `document.elementFromPoint` at a subagent edge's own midpoint returns the Subagent activity
// container rather than the edge.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { kindFromState } from "$lib/components/flow/AgentStateNode.svelte";
import {
  buildAgentTreeGraph,
  unreadableChildState,
  unreadableReasonLine
} from "$lib/state/flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });

const parent = {
  workflowId: "wf-parent",
  role: "parent",
  label: "parent run",
  frames: [
    frame("turn_started", { user_message: "delegate", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("subagent_started", { subagent_id: "s1", workflow_id: "wf-a", agent_key: "task" }),
    frame("message_handler_end", { output: { text: "delegated" } })
  ],
  agentInterface: []
};

const sub = {
  workflowId: "wf-a",
  role: "subagent",
  label: "child run",
  parentWorkflowId: "wf-parent",
  subagentId: "s1",
  agentKey: "task",
  frames: [
    frame("turn_started", { user_message: "summarize the README", turn_number: 2 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("model_interaction_ended", { model: "gpt-5.1" }),
    frame("message_handler_end", { output: { text: "one concise paragraph" } }),
    frame("turn_end", { turn_number: 2 })
  ],
  agentInterface: [{ name: "ask", description: "ask the subagent something" }],
  stopped: true
};

const graph = buildAgentTreeGraph([parent, sub]);
const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
const zOf = (id) => {
  const node = nodeById.get(id);
  assert.ok(node, `expected a node "${id}"`);
  return node.zIndex ?? 0;
};

const subagentEdges = graph.edges.filter((edge) => edge.id.startsWith("wf-a::"));

describe("a subagent's own flow arrows", () => {
  /* The flow itself, before anything about how it paints: a subagent that took a
     turn runs Input -> Model interaction -> Output, and the graph should say so. */
  it("are drawn", () => {
    assert.deepEqual(
      subagentEdges.map((edge) => `${edge.source} -> ${edge.target}`).sort(),
      ["wf-a::input -> wf-a::model", "wf-a::model -> wf-a::output"],
      "a subagent that ran a turn should draw the same Input -> Model -> Output chain its parent does"
    );
  });

  /* A rewritten node id that no edge follows is the other way this fails, and it
     looks identical on screen: scopedGraph prefixes both, so they must still meet. */
  it("meet the nodes they name, prefixes and all", () => {
    for (const edge of graph.edges) {
      assert.ok(nodeById.has(edge.source), `edge "${edge.id}" dangles: no node "${edge.source}"`);
      assert.ok(nodeById.has(edge.target), `edge "${edge.id}" dangles: no node "${edge.target}"`);
    }
  });

  /* The ladder each subagent edge has to sit on. Above the Subagent activity
     container it is drawn inside — the rung that was missing, since the container
     is a card at 10 and the edges were lifted only to 5 — and above the subagent's
     own boundary, whose gradient is near-opaque. Below the two cards it runs
     between, so it passes behind them rather than across their faces. */
  it("sit above what buries them and below the cards they run between", () => {
    const containerZ = zOf("wf-parent::tool-container");
    const boundaryZ = zOf("wf-a::agent-runtime");
    for (const edge of subagentEdges) {
      const z = edge.zIndex ?? 0;
      assert.ok(
        z > containerZ,
        `edge "${edge.id}" at z ${z} is under the Subagent activity container at ${containerZ}`
      );
      assert.ok(
        z > boundaryZ,
        `edge "${edge.id}" at z ${z} is under the subagent boundary at ${boundaryZ}`
      );
      for (const end of [edge.source, edge.target]) {
        assert.ok(
          z < zOf(end),
          `edge "${edge.id}" at z ${z} paints over the card "${end}" at ${zOf(end)}`
        );
      }
    }
  });

  /* Lifting the nodes and leaving the edges behind is precisely the bug, so the
     parent's own ladder has to keep its shape too — an edge that cleared the
     container by also clearing its endpoints would have satisfied the loop above. */
  it("do not cost the parent its own ladder", () => {
    for (const edge of graph.edges.filter((item) => item.id.startsWith("wf-parent::"))) {
      const z = edge.zIndex ?? 0;
      assert.ok(
        z > zOf("wf-parent::agent-runtime"),
        `parent edge "${edge.id}" at z ${z} is under its own boundary`
      );
      assert.ok(z < zOf(edge.source), `parent edge "${edge.id}" paints over "${edge.source}"`);
    }
  });

  /**
   * And the CSS half, which no projection assertion can reach.
   *
   * `.svelte-flow__edges` is positioned absolutely by the library, so any z-index
   * here opens a stacking context around every edge in the canvas: each one then
   * paints at that single number regardless of its own, and the ordering asserted
   * above stops meaning anything on screen. Its sibling `.svelte-flow__nodes` is
   * static and may keep whatever it likes, because z-index on a static box does
   * nothing — which is why the pair used to read as symmetric and was not.
   */
  it("leave the Subagent activity container saying what its children are doing", () => {
    const containerOf = (child) =>
      buildAgentTreeGraph([parent, child]).nodes.find((node) => node.id === "wf-parent::tool-container").data;
    const chipOf = (data) => kindFromState(data.state, data.tone);

    assert.equal(chipOf(containerOf(sub)), "complete", "every child finished");
    const running = { ...sub, stopped: false, frames: sub.frames.slice(0, 2) };
    assert.equal(chipOf(containerOf(running)), "delegating", "a child still running");
    assert.equal(chipOf(containerOf({ ...sub, frames: [] })), "delegating", "dispatched, not yet started");
    const failed = {
      ...sub,
      frames: [
        ...sub.frames.slice(0, 2),
        frame("message_handler_error", { message: "boom" }),
        frame("turn_end", { turn_number: 2 })
      ]
    };
    assert.equal(chipOf(containerOf(failed)), "error", "a child failed, even once its turn ended");
  });

  /* agent-dag: both children completed before the merge could replay them, so the
     parent carries their starts and two stream_unavailable markers, and the children
     carry nothing. Their boundaries painted under the container's opaque fill. */
  it("show children whose streams are unavailable above the container, and let it finish", () => {
    const reason = "its workflow has completed and is not yet replayable";
    const unreadParent = {
      ...parent,
      frames: [
        frame("turn_started", { user_message: "write and translate", turn_number: 1 }),
        frame("subagent_started", { subagent_id: "s1", workflow_id: "wf-a", agent_key: "haiku_writer" }),
        frame("subagent_started", { subagent_id: "s2", workflow_id: "wf-b", agent_key: "french_translator" }),
        frame("subagent_stream_unavailable", { subagent_id: "s1", workflow_id: "wf-a", reason }),
        frame("subagent_stream_unavailable", { subagent_id: "s2", workflow_id: "wf-b", reason })
      ]
    };
    const child = (workflowId, subagentId, agentKey) => ({
      ...sub,
      workflowId,
      subagentId,
      agentKey,
      frames: [],
      stopped: true
    });
    const tree = buildAgentTreeGraph([
      unreadParent,
      child("wf-a", "s1", "haiku_writer"),
      child("wf-b", "s2", "french_translator")
    ]);
    const byId = new Map(tree.nodes.map((node) => [node.id, node]));
    const container = byId.get("wf-parent::tool-container");

    for (const [workflowId, name] of [["wf-a", "haiku_writer"], ["wf-b", "french_translator"]]) {
      const boundary = byId.get(`${workflowId}::agent-runtime`);
      assert.ok(boundary, `${name} should be projected`);
      assert.match(boundary.data.title, new RegExp(name));
      assert.equal(boundary.data.state, "closed", "stopped, with no reply seen");
      assert.equal(boundary.data.reason, reason, "the reason rides on the boundary");
      assert.ok(
        boundary.zIndex > container.zIndex,
        `${name} at z ${boundary.zIndex} is under the container at ${container.zIndex}`
      );
    }
    assert.equal(kindFromState(container.data.state, container.data.tone), "complete");
  });

  it("are not flattened onto one rung by the edge layer's own z-index", () => {
    const flowSource = readFileSync(
      new URL("../src/lib/components/flow/AgentStateFlow.svelte", import.meta.url),
      "utf8"
    );
    /* The rule being absent is the fix, so the match below is expected to be empty
       — which means an empty read would look exactly like a pass. This is the floor
       that tells the two apart. */
    assert.match(
      flowSource,
      /\.svelte-flow__/,
      "AgentStateFlow should still be the file that reaches into the canvas's own layers"
    );
    const edgesLayerRule = flowSource.match(/\.svelte-flow__edges\)\s*\{([^}]*)\}/);
    assert.ok(
      !edgesLayerRule || !/z-index/.test(edgesLayerRule[1]),
      "a z-index on .svelte-flow__edges flattens every edge onto one rung — " +
        `found: ${edgesLayerRule?.[1].trim()}`
    );
  });
});

/* A child the merge gave up on is read from what the parent saw and what Temporal
   says, never from the free-text reason, and a missing stream alone is never red. */
describe("a subagent whose stream could not be read", () => {
  const marker = (workflowId, extra = {}) =>
    frame("subagent_stream_unavailable", { subagent_id: "s1", workflow_id: workflowId, reason: "gave up", ...extra });
  const reply = (outcome) =>
    frame("subagent_reply_received", { subagent_id: "s1", workflow_id: "wf-a", subagent_turn: 1, outcome });
  const treeWith = (parentFrames, child = {}) => {
    const tree = buildAgentTreeGraph([
      {
        ...parent,
        frames: [
          frame("turn_started", { user_message: "delegate", turn_number: 1 }),
          frame("subagent_started", { subagent_id: "s1", workflow_id: "wf-a", agent_key: "task" }),
          frame("subagent_message_sent", { subagent_id: "s1", workflow_id: "wf-a", subagent_turn: 1 }),
          ...parentFrames
        ]
      },
      { ...sub, frames: [], stopped: false, ...child }
    ]);
    const byId = new Map(tree.nodes.map((node) => [node.id, node]));
    return { boundary: byId.get("wf-a::agent-runtime"), container: byId.get("wf-parent::tool-container").data };
  };

  it("takes its state from the parent's reply, the stop and Temporal, in that order", () => {
    const rows = [
      [{ reply: "ok", stoppedByParent: false, closed: true, executionStatus: "FAILED" }, "complete"],
      [{ reply: "error", stoppedByParent: false, closed: false }, "error"],
      ...["FAILED", "TIMED_OUT", "TERMINATED"].map((status) => [
        { stoppedByParent: false, closed: true, executionStatus: status },
        "error"
      ]),
      [{ stoppedByParent: true, closed: true }, "closed"],
      [{ stoppedByParent: true, closed: false, executionStatus: "RUNNING" }, "closed"],
      ...["CANCELED", "COMPLETED"].map((status) => [
        { stoppedByParent: false, closed: true, executionStatus: status },
        "closed"
      ]),
      [{ stoppedByParent: false, closed: false, executionStatus: "RUNNING" }, "delegating"],
      [{ stoppedByParent: false, closed: false }, "idle"]
    ];
    for (const [facts, expected] of rows) {
      assert.equal(unreadableChildState(facts), expected, JSON.stringify(facts));
    }
  });

  it("wears each state's tone on the boundary and counts it in the container", () => {
    const cases = [
      [[marker("wf-a"), reply("ok")], {}, "complete", "complete", "complete"],
      [[marker("wf-a"), reply("error")], {}, "error", "error", "error"],
      [[marker("wf-a")], { executionStatus: "FAILED", stopped: true }, "error", "error", "error"],
      [[marker("wf-a"), frame("subagent_stopped", { subagent_id: "s1", workflow_id: "wf-a" })], { stopped: true }, "closed", "closed", "complete"],
      [[marker("wf-a")], { executionStatus: "RUNNING" }, "delegating", "delegating", "delegating"],
      [[marker("wf-a")], {}, "idle", "idle", "complete"]
    ];
    for (const [frames, child, state, chip, containerChip] of cases) {
      const { boundary, container } = treeWith(frames, child);
      assert.equal(boundary.data.state, state);
      assert.equal(kindFromState(boundary.data.state, boundary.data.tone), chip, `${state} chip`);
      assert.equal(kindFromState(container.state, container.tone), containerChip, `container around ${state}`);
    }
  });

  it("reads a reply as the latest turn's only", () => {
    const { boundary } = treeWith(
      [reply("ok"), frame("subagent_message_sent", { subagent_id: "s1", workflow_id: "wf-a", subagent_turn: 2 }), marker("wf-a")],
      { executionStatus: "RUNNING" }
    );
    assert.equal(boundary.data.state, "delegating", "turn 1's reply does not answer turn 2");
  });

  /* Gated on nodes.length === 1, a child that streamed its opening frames before the merge
     gave up kept saying "running", and held the container delegating, forever. */
  it("settles a partly streamed child once the parent has its reply", () => {
    const partial = {
      frames: [
        frame("turn_started", { user_message: "summarize", turn_number: 1 }),
        frame("model_interaction_started", { model: "gpt-5.1" }),
        marker("wf-a")
      ]
    };
    const replied = treeWith([reply("ok")], partial);
    assert.ok(replied.boundary.data.boundaryHeight > 88, "what it drew stays drawn");
    assert.equal(replied.boundary.data.state, "complete");
    assert.equal(kindFromState(replied.container.state, replied.container.tone), "complete");

    const waiting = treeWith([], { ...partial, executionStatus: "RUNNING" });
    assert.equal(waiting.boundary.data.state, "delegating");
    assert.equal(kindFromState(waiting.container.state, waiting.container.tone), "delegating");
  });

  /* node() named every boundary "Agent runtime, idle" before scopedGraph rewrote its title
     and state, and the inspector, built from context alone, never said why. */
  it("announces its own name, state and reason, and explains itself in the inspector", () => {
    const { boundary } = treeWith([marker("wf-a"), reply("ok")]);
    assert.equal(boundary.ariaLabel, "task subagent runtime, complete, gave up");
    const why = boundary.data.context[0];
    assert.equal(why.label, "Why the detail is missing");
    assert.match(why.text, /gave up/);
    assert.match(why.text, /Parent reply: ok/);

    const plain = buildAgentTreeGraph([parent, sub]).nodes.find((node) => node.id === "wf-a::agent-runtime");
    assert.equal(plain.ariaLabel, `${plain.data.title}, stopped`);
  });

  /* The reason line grows the header, so a child that drew something keeps its cards clear of it. */
  it("moves what a partly streamed child drew below its reason line", () => {
    const frames = [
      frame("turn_started", { user_message: "summarize", turn_number: 1 }),
      frame("model_interaction_started", { model: "gpt-5.1" })
    ];
    const plain = buildAgentTreeGraph([parent, { ...sub, frames, stopped: false }]).nodes;
    const { boundary } = treeWith([], { frames: [...frames, marker("wf-a")] });
    const shifted = buildAgentTreeGraph([
      { ...parent, frames: [...parent.frames, marker("wf-a")] },
      { ...sub, frames, stopped: false }
    ]).nodes;
    const yOf = (nodes, id) => nodes.find((node) => node.id === id).position.y;
    const top = (nodes) => yOf(nodes, "wf-a::agent-runtime");
    assert.equal(
      yOf(shifted, "wf-a::model") - top(shifted),
      yOf(plain, "wf-a::model") - top(plain) + 36
    );
    assert.ok(boundary.data.boundaryHeight > 88 + 36);
  });

  it("words its reason from reason_code when the backend sends one", () => {
    const coded = treeWith([marker("wf-a", { reason_code: "stalled" }), reply("ok")]);
    assert.equal(coded.boundary.data.reason, "Stalled without delivering its turn");
    assert.equal(unreadableReasonLine(undefined, "  "), "Its detail could not be read");
    for (const code of ["closed_not_replayable", "read_failed", "ended_mid_turn", "stalled"]) {
      assert.ok(unreadableReasonLine(code, "free text").length > 0);
      assert.notEqual(unreadableReasonLine(code, "free text"), "free text", code);
    }
  });
});
