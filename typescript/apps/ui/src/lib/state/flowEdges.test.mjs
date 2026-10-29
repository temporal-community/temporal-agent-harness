// ABOUTME: The state graph's edges are xyflow's own beziers with no marker, ending on a lit port
// on the card's border; a wrap onto the next row is a square smoothstep. Each edge and its
// target port take the state of the step it leads into: solid in its flow color once
// delivered, xyflow's `animated` (the timeline's marching dash) while that step runs, amber
// and marching while it waits on a person, muted still dots before it is reached, and red
// when it failed.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { buildAgentGraph } from "./flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });
const tool = { tool_id: "t1", tool_name: "run_script", tool_input: { script: "print(1)" } };
const ask = { tool_id: "t1", tool_name: "ask_user", tool_input: { question: "Which one?" } };
const toolId = "tool:t1";

function frames(phase) {
  const call = phase === "callback" ? ask : tool;
  const list = [
    frame("turn_started", { user_message: "go", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt" }),
    frame("tool_requested", call)
  ];
  if (phase === "awaiting") list.push(frame("tool_approval_requested", tool));
  if (phase === "running") list.push(frame("model_interaction_ended", {}), frame("tool_start", tool));
  if (phase === "callback") {
    list.push(
      frame("model_interaction_ended", {}),
      frame("tool_start", ask),
      frame("callback_requested", { ...ask, output_schema: { type: "string" } })
    );
  }
  if (phase === "failed") {
    list.push(frame("model_interaction_ended", {}), frame("tool_error", { ...tool, message: "boom" }));
  }
  if (phase === "settled") {
    list.push(
      frame("tool_end", { ...tool, tool_output: "ok" }),
      frame("model_interaction_ended", {}),
      frame("message_handler_end", { text: "done" }),
      frame("turn_end", { turn_number: 1 })
    );
  }
  return list;
}

const graph = (phase, options = {}) => buildAgentGraph(frames(phase), options);
const flowColor = (edge) => `var(--${edge.class})`;
const portOf = (nodes, nodeId, handle) => nodes.find((item) => item.id === nodeId)?.data.ports?.[handle];

/** What the edge draws: its stroke, its dash, whether it marches, and its target port's color. */
function look(edge, nodes) {
  const style = String(edge.style ?? "");
  return {
    stroke: style.match(/stroke:\s*([^;]+)/)?.[1].trim() ?? flowColor(edge),
    dash: style.match(/stroke-dasharray:\s*([^;]+)/)?.[1].trim() ?? null,
    animated: Boolean(edge.animated),
    port: portOf(nodes, edge.target, edge.targetHandle)
  };
}

const delivered = (edge) => ({ stroke: flowColor(edge), dash: null, animated: false, port: flowColor(edge) });
const flowing = (edge) => ({ ...delivered(edge), animated: true });
const waiting = { stroke: "var(--live)", dash: null, animated: true, port: "var(--live)" };
const unreached = { stroke: "var(--text-4)", dash: "1 4", animated: false, port: "var(--text-4)" };
const failed = { stroke: "var(--error)", dash: null, animated: false, port: "var(--error)" };

function assertInto({ nodes, edges }, target, expected) {
  const found = edges.filter((edge) => edge.target === target);
  assert.ok(found.length > 0, `the fixture should draw an edge into ${target}`);
  for (const edge of found) {
    assert.deepEqual(look(edge, nodes), typeof expected === "function" ? expected(edge) : expected, edge.id);
  }
}

/* Four tools in a row overflow the three-column grid, so the fourth wraps onto the next row. */
function wrappingGraph() {
  const list = [frame("turn_started", { user_message: "go", turn_number: 1 }), frame("model_interaction_started", { model: "gpt" })];
  for (const n of [1, 2, 3, 4]) {
    const call = { tool_id: `w${n}`, tool_name: "step", tool_input: {} };
    list.push(frame("tool_start", call), frame("tool_end", { ...call, tool_output: "ok" }));
  }
  return buildAgentGraph(list);
}

describe("the state graph's edges", () => {
  it("carry no marker: a port on the card says where they end", () => {
    for (const phase of ["settled", "running", "awaiting", "queued", "failed", "callback"]) {
      const { nodes, edges } = graph(phase);
      assert.ok(edges.length > 0);
      for (const edge of edges) {
        assert.equal(edge.markerEnd, undefined, `${edge.id} has an arrowhead`);
        assert.equal(edge.markerStart, undefined, `${edge.id} has a source marker`);
        assert.equal(portOf(nodes, edge.source, edge.sourceHandle), flowColor(edge), `${edge.id} source port`);
        assert.ok(portOf(nodes, edge.target, edge.targetHandle), `${edge.id} target port`);
      }
    }
  });

  it("are beziers on a row and square smoothsteps where they wrap onto the next", () => {
    const { nodes, edges } = wrappingGraph();
    const y = new Map(nodes.map((item) => [item.id, item.position.y]));
    const wraps = edges.filter((edge) => y.get(edge.source) !== y.get(edge.target) && edge.id.startsWith("flow-tool"));
    assert.ok(wraps.length > 0, "the fixture should wrap a row");
    for (const edge of edges) {
      if (wraps.includes(edge)) {
        assert.equal(edge.type, "smoothstep", edge.id);
        assert.deepEqual(edge.pathOptions, { borderRadius: 0 }, edge.id);
        assert.equal(edge.targetHandle, "target-top", edge.id);
      } else {
        assert.equal(edge.type ?? "default", "default", `${edge.id} is not a bezier`);
        assert.equal(edge.pathOptions, undefined, edge.id);
      }
    }
  });

  it("light only the ports an edge uses, and drop a port once its edge is gone", () => {
    const { nodes, edges } = graph("running");
    const used = new Set(edges.flatMap((edge) => [`${edge.source}/${edge.sourceHandle}`, `${edge.target}/${edge.targetHandle}`]));
    for (const item of nodes) {
      for (const handle of Object.keys(item.data.ports ?? {})) assert.ok(used.has(`${item.id}/${handle}`), `${item.id}/${handle}`);
    }
    assert.equal(buildAgentGraph([]).nodes.some((item) => item.data.ports), false);
  });

  it("end on the border's midline: zero-sized handles drawn as a centered pip-sized port", () => {
    const source = readFileSync(new URL("../components/flow/AgentStateNode.svelte", import.meta.url), "utf8");
    const rule = (selector) => source.match(new RegExp(`:global\\(${selector.replace(/[.:()]/g, "\\$&")}\\)\\s*\\{([^}]*)\\}`))?.[1];
    const handle = rule(".node-handle");
    assert.ok(handle, "no .node-handle rule");
    for (const property of ["width", "height", "min-width", "min-height"]) {
      assert.match(handle, new RegExp(`(^|[\\s;])${property}:\\s*0;`), property);
    }
    assert.match(handle, /(^|[\s;])margin:\s*-0\.5px;/, "half the 1px border, so the edge ends on its midline");
    assert.doesNotMatch(handle, /\b(top|bottom|left|right|transform):/, "the handle is offset off the border");
    const port = rule(".node-handle.port::before");
    assert.ok(port, "no port drawing");
    assert.match(port, /inset:\s*calc\(var\(--pip\)\s*\/\s*-2\);/, "the port is centered on the edge's end");
    assert.match(
      port,
      /background:\s*linear-gradient\(var\(--port-color\),\s*var\(--port-color\)\),\s*var\(--flow-canvas\);/,
      "the flow colors are translucent, so the port is laid over the opaque canvas"
    );
  });

  it("are solid in their flow color once the step they lead into is delivered", () => {
    const { nodes, edges } = graph("settled");
    for (const edge of edges) assert.deepEqual(look(edge, nodes), delivered(edge), edge.id);
    assert.ok(edges.some((edge) => edge.class === "edge-output"), "the reply edge is green, not purple");
  });

  it("march only into a running or streaming step", () => {
    const running = graph("running");
    assertInto(running, toolId, flowing);
    for (const edge of running.edges) {
      if (edge.target !== toolId) assert.equal(edge.animated, false, edge.id);
    }
  });

  it("march in the timeline's open-span dash, and only while the step is open", () => {
    const running = graph("running").edges.find((edge) => edge.target === toolId);
    const done = graph("settled").edges.find((edge) => edge.target === toolId);
    assert.equal(running?.animated, true, "the edge into a running step marches");
    assert.equal(done?.animated, false, "the edge into a completed step is still");

    const flow = readFileSync(new URL("../components/flow/AgentStateFlow.svelte", import.meta.url), "utf8");
    const app = readFileSync(new URL("../../app.css", import.meta.url), "utf8");
    const timeline = readFileSync(new URL("../components/flow/WorkflowTimeline.svelte", import.meta.url), "utf8");
    const march = flow.match(/\.flow-wrap :global\(\.svelte-flow__edge\.animated path\)\s*\{([^}]*)\}/)?.[1] ?? "";
    assert.match(march, /stroke-dasharray:\s*3 3;/, "3px on, 3px off");
    assert.match(march, /animation:\s*edge-march var\(--dash-cycle\) linear infinite;/);
    assert.match(flow, /@keyframes edge-march\s*\{\s*from\s*\{\s*stroke-dashoffset:\s*6;\s*\}/, "one 6px period per cycle");
    assert.match(
      flow,
      /prefers-reduced-motion: reduce\)\s*\{\s*\.flow-wrap :global\(\.svelte-flow__edge\.animated path\)\s*\{\s*animation:\s*none;/,
      "reduced motion keeps the dash but stops it"
    );
    assert.match(app, /--dash-cycle:\s*1\.8s;/);
    assert.match(
      timeline,
      /animation:\s*march var\(--dash-cycle\) steps\(5\) infinite;/,
      "the timeline shares the cycle, a whole pixel a step so its 1px sticks stay crisp"
    );
  });

  it("wait in marching amber into a card awaiting approval or a pending callback", () => {
    assertInto(graph("awaiting"), toolId, waiting);
    const callback = graph("callback");
    assert.equal(callback.nodes.find((item) => item.id === toolId)?.data.state, "awaiting");
    assertInto(callback, toolId, waiting);
  });

  it("dot, muted and still, into a step not reached yet, including past the replay cursor", () => {
    assertInto(graph("queued"), toolId, unreached);

    const all = frames("settled");
    const cursor = all.findIndex((item) => item.event === "tool_end");
    assertInto(buildAgentGraph(all.slice(0, cursor)), toolId, unreached);
    const end = buildAgentGraph(all);
    assert.ok(
      end.edges.every((edge) => look(edge, end.nodes).dash == null),
      "the same edge is solid once the cursor reaches the end"
    );
  });

  it("turn red, solid, into a failed step", () => {
    assertInto(graph("failed"), toolId, failed);
  });

  it("stand still, still dashed, under reduced motion and leave the other three alone", () => {
    assertInto(graph("running", { reducedMotion: true }), toolId, (edge) => ({ ...delivered(edge), dash: "3 3" }));
    assertInto(graph("awaiting", { reducedMotion: true }), toolId, { ...waiting, dash: "3 3", animated: false });
    assertInto(graph("queued", { reducedMotion: true }), toolId, unreached);
    assertInto(graph("failed", { reducedMotion: true }), toolId, failed);
  });

  it("wait rather than march once a running call parks on a person", () => {
    const list = frames("running");
    list.push(frame("callback_requested", { ...tool, output_schema: {} }));
    assertInto(buildAgentGraph(list), toolId, waiting);
  });
});
