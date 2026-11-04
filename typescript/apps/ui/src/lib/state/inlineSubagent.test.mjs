/**
 * An inline subagent runs inside its parent (a Claude Code subagent, for one): it publishes the
 * same lifecycle as a child workflow, on its parent's stream, with `inline: true` on its markers.
 * The console must draw it exactly as it draws that child workflow, but never query it, never
 * offer it as somewhere to send a message, and send its approvals to the workflow it runs in.
 *
 * Run: npx vitest run src/lib/state/inlineSubagent.test.mjs
 */
import assert from "node:assert/strict";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface, session, sleep, waitFor } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const parentWorkflowId = "agent-session-parent";
const rootAgentId = "de539b";
const childAgentId = "de539b-093b70";
/* An inline subagent's key is the tool call that started it; the real child uses the same id
   here so the two runs' projections compare directly. */
const childKey = "toolu_agent";

/* A Claude-style run: the parent's Agent call starts a subagent, which runs one turn. */
function run(inline, { cutOff = false } = {}) {
  let offset = 0;
  const at = (agentId, turn, data) => ({
    event: data.type,
    data: {
      agent_id: agentId,
      turn_id: turn.id,
      turn_number: turn.number,
      message_id: turn.message,
      timestamp: (offset += 1),
      resume_offset: offset,
      replay: true,
      ...data
    }
  });
  const parentTurn = { id: "t1", number: 1, message: "m1" };
  const childTurn = { id: `${childAgentId}-turn-1`, number: 1, message: childKey };
  const parent = (data) => at(rootAgentId, parentTurn, data);
  const child = (data) => at(childAgentId, childTurn, data);
  const ids = { subagent_id: childAgentId, agent_key: "file-finder", workflow_id: childKey, inline };
  const bracket = { ...ids, handler: "Agent", subagent_turn: 1 };
  const agentCall = { tool_id: childKey, tool_name: "Agent", tool_input: { prompt: "find it" } };
  const glob = { tool_id: "toolu_glob", tool_name: "Glob", tool_input: { pattern: "*.txt" } };
  return [
    parent({ type: "message_accepted", handler: "ask", payload: { text: "Delegate it." }, disposition: "opened" }),
    parent({ type: "turn_started", message_id: null }),
    parent({ type: "message_handler_start" }),
    parent({ type: "model_interaction_started", model: "claude" }),
    parent({ type: "model_interaction_ended", model: "claude", usage: null }),
    parent({ type: "tool_requested", ...agentCall }),
    parent({ type: "tool_start", ...agentCall }),
    parent({ type: "subagent_started", ...ids }),
    parent({ type: "subagent_message_sent", ...bracket, from_offset: 0 }),
    child({ type: "message_accepted", handler: "Agent", payload: { prompt: "find it" }, disposition: "opened" }),
    child({ type: "turn_started", message_id: null }),
    child({ type: "message_handler_start" }),
    child({ type: "model_interaction_started", model: "claude" }),
    child({ type: "model_interaction_ended", model: "claude", usage: null }),
    child({ type: "tool_requested", ...glob }),
    child({ type: "tool_start", ...glob }),
    ...(cutOff
      ? [
          child({ type: "tool_error", ...glob, message: "connection lost" }),
          child({ type: "message_handler_error", message: "connection lost" })
        ]
      : [
          child({ type: "tool_end", ...glob, tool_output: "notes.txt" }),
          child({ type: "model_interaction_started", model: "claude" }),
          child({ type: "reply_delta", text: "Found notes.txt." }),
          child({ type: "model_interaction_ended", model: "claude", usage: null }),
          child({ type: "message_handler_end", output: { text: "Found notes.txt." } })
        ]),
    child({ type: "turn_end", message_id: null }),
    parent({ type: "subagent_reply_received", ...bracket, outcome: cutOff ? "error" : "ok" }),
    parent({ type: "subagent_stopped", ...ids }),
    cutOff
      ? parent({ type: "tool_error", ...agentCall, message: "connection lost" })
      : parent({ type: "tool_end", ...agentCall, tool_output: "Found notes.txt." }),
    parent({ type: "model_interaction_started", model: "claude" }),
    parent({ type: "reply_delta", text: "It found notes.txt." }),
    parent({ type: "model_interaction_ended", model: "claude", usage: null }),
    parent({ type: "message_handler_end", output: { text: "It found notes.txt." } }),
    parent({ type: "turn_end", message_id: null })
  ];
}

async function replay(frames) {
  installBrowserSurface();
  const interfaceRequests = [];
  const approvals = [];
  const api = {
    async approve(request) {
      approvals.push(request);
      return { accepted: true };
    },
    async listSessions() {
      return [];
    },
    async agentInterface(workflowId) {
      interfaceRequests.push(workflowId);
      return [];
    },
    async operatorInterface() {
      return [];
    },
    async workflowStatus(workflowId) {
      return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
    },
    attach() {
      return (async function* () {
        for (const frame of frames) yield frame;
        await sleep(50_000);
      })();
    }
  };
  const controller = new AgentRunController(api);
  controller.sessions = [session(parentWorkflowId)];
  void controller.selectSession(parentWorkflowId);
  await waitFor("the frames to reach the view", () => controller.frames.length === frames.length);
  await sleep(50);
  return { controller, interfaceRequests, approvals };
}

/* What each pane draws, with nothing that only says how the subagent was run. */
const nodes = (c) =>
  c.graph.nodes.map(({ id, data }) => ({
    id,
    title: data.title,
    state: data.state,
    tone: data.tone,
    headerPrefix: data.headerPrefix
  }));
const logRows = (c) =>
  c.fullReplayLog.rows.map((row) => ({
    event: row.event,
    workflowId: row.workflowId,
    turnNumber: row.turnNumber,
    sourceTurnNumber: row.sourceTurnNumber,
    parentTurnNumber: row.parentTurnNumber,
    sourceLabel: row.sourceLabel
  }));
const timeline = (c) => JSON.parse(JSON.stringify(c.stepTimeline));
const targets = (c) =>
  c.messageTargets.map(({ workflowId, role, label, closed }) => ({ workflowId, role, label, closed }));

describe.each([
  ["a finished subagent", {}],
  ["a cut-off subagent", { cutOff: true }]
])("%s run inline", (_name, options) => {
  let inline;
  let real;

  beforeAll(async () => {
    real = await replay(run(false, options));
    inline = await replay(run(true, options));
  });

  it("leaves the graph with its call, where a child workflow stays in the shared container", () => {
    const ids = (c) => nodes(c).map((node) => node.id);
    assert.ok(ids(real.controller).includes(`${childKey}::agent-runtime`));
    assert.ok(!ids(inline.controller).some((id) => id.startsWith(`${childKey}::`)));
    assert.ok(!ids(inline.controller).some((id) => id.endsWith("::tool-container")));
  });

  it("draws the same timeline and latency rows as a child workflow", () => {
    assert.deepEqual(timeline(inline.controller), timeline(real.controller));
  });

  it("logs and chats the same as a child workflow", () => {
    assert.deepEqual(logRows(inline.controller), logRows(real.controller));
    assert.deepEqual(inline.controller.chatTranscript, real.controller.chatTranscript);
    const row = inline.controller.fullReplayLog.rows.find((r) => r.event === "tool_requested" && r.workflowId === childKey);
    assert.ok(row, "the child's events must be filed under the child");
  });

  it("lists it as its own agent, closed as a stopped child is", () => {
    assert.deepEqual(targets(inline.controller), targets(real.controller));
    assert.deepEqual(
      targets(inline.controller).map((target) => [target.workflowId, target.role]),
      [
        [parentWorkflowId, "parent"],
        [childKey, "subagent"]
      ]
    );
  });

  it("never queries it", () => {
    assert.ok(real.interfaceRequests.includes(childKey), "a child workflow is queried");
    assert.ok(!inline.interfaceRequests.includes(childKey), "an inline subagent has no workflow to query");
  });
});

describe("an inline subagent", () => {
  it("is closed to messages while it runs, unlike a running child workflow", async () => {
    const frames = run(true);
    const running = frames.slice(0, frames.findIndex((f) => f.event === "tool_start" && f.data.tool_id === "toolu_glob") + 1);
    const { controller } = await replay(running);
    const target = controller.messageTargets.find((t) => t.workflowId === childKey);
    assert.equal(target.closed, true);
    const runtime = controller.graph.nodes.find((node) => node.id === `${childKey}::agent-runtime`);
    assert.notEqual(runtime.data.state, "stopped", "it is drawn running, as a running child is");
  });

  it("is drawn inside its call's own Subagent tool box while it runs", async () => {
    const frames = run(true);
    const running = frames.slice(0, frames.findIndex((f) => f.event === "tool_start" && f.data.tool_id === "toolu_glob") + 1);
    const { controller } = await replay(running);
    const byId = new Map(controller.graph.nodes.map((node) => [node.id, node]));
    const host = byId.get(`${parentWorkflowId}::tool:${childKey}`);
    assert.equal(host.data.title, "Subagent tool");
    assert.equal(host.data.size, "container");
    const runtime = byId.get(`${childKey}::agent-runtime`);
    const inside = (outer, inner) =>
      inner.position.x >= outer.position.x &&
      inner.position.y >= outer.position.y &&
      inner.position.x + inner.data.boundaryWidth <= outer.position.x + outer.data.nodeWidth &&
      inner.position.y + inner.data.boundaryHeight <= outer.position.y + outer.data.nodeHeight;
    assert.ok(inside(host, runtime), "the subagent's runtime sits inside its call's box");
    assert.ok(![...byId.keys()].some((id) => id.endsWith("::tool-container")), "no shared container");
  });

  it("sends its approvals to the workflow it runs in", async () => {
    const { controller, approvals } = await replay(run(true));
    await controller.approveTool(childKey, "toolu_glob", true);
    assert.equal(approvals.at(-1).session_id, parentWorkflowId, "the child's key is not a workflow");
  });

  it("carries on cleanly when a re-run takes over its cut-off call", async () => {
    /* A lost connection leaves the Agent call and the child's Glob open, its approval pending.
       The re-run takes both over under the same ids: no second tool_requested or tool_start,
       and no second subagent_started, only a fresh model call on each agent. */
    const frames = run(true);
    const cut = frames.findIndex((f) => f.event === "tool_start" && f.data.tool_id === "toolu_glob");
    const like = (base, data) => ({ event: data.type, data: { ...base.data, ...data } });
    const globStart = frames[cut];
    const parentFrame = frames.find((f) => f.event === "model_interaction_started" && f.data.agent_id === rootAgentId);
    const childModel = frames.find((f) => f.event === "model_interaction_started" && f.data.agent_id === childAgentId);
    const glob = { tool_id: "toolu_glob", tool_name: "Glob" };
    const taken = [
      ...frames.slice(0, cut),
      like(globStart, { type: "tool_approval_requested", ...glob, tool_input: { pattern: "*.txt" } }),
      /* The reconnect: the parent repeats its Agent call, the child its Glob. */
      like(parentFrame, { type: "model_interaction_started", model: "claude" }),
      like(parentFrame, { type: "model_interaction_ended", model: "claude", usage: null }),
      like(childModel, { type: "model_interaction_started", model: "claude" }),
      like(childModel, { type: "model_interaction_ended", model: "claude", usage: null }),
      like(globStart, { type: "tool_approval_resolved", ...glob, approved: true, reason: null, remember: false }),
      ...frames.slice(cut)
    ];
    let offset = 0;
    for (const frame of taken) {
      offset += 1;
      frame.data = { ...frame.data, timestamp: offset, resume_offset: offset };
    }
    const { controller } = await replay(taken);
    const finished = (await replay(run(true))).controller;
    const toolNodes = (c) => nodes(c).filter((node) => /toolu_/.test(node.id.split("::").at(-1) ?? ""));
    assert.deepEqual(toolNodes(controller), toolNodes(finished), "one finished card per call");
    const runtime = (c) => nodes(c).find((node) => node.id === `${childKey}::agent-runtime`);
    assert.deepEqual(runtime(controller), runtime(finished), "the subagent ends as it would have");
    assert.deepEqual(targets(controller), targets(finished));
    const chat = (c) => c.chatTranscript.map(({ timestamp, ...entry }) => entry);
    assert.deepEqual(chat(controller), chat(finished));
    const toolRows = (c) =>
      c.stepTimeline.workflow
        .rows()
        .filter((row) => row.toolId != null)
        .map(({ kind, toolId, workflowId, ongoing, tone }) => ({ kind, toolId, workflowId, ongoing, tone }));
    const approval = { kind: "approval", toolId: "toolu_glob", workflowId: childKey, ongoing: false, tone: "done" };
    assert.deepEqual(toolRows(controller), [...toolRows(finished), approval], "one settled row per call");
    assert.equal(controller.stepTimeline.workflow.ongoing, false);
  });

  it("moves the resume cursor, since its events are on its parent's stream", async () => {
    const frames = run(true);
    const upToChild = frames.slice(0, frames.findIndex((f) => f.data.agent_id === childAgentId) + 1);
    const { controller } = await replay(upToChild);
    assert.equal(controller.lastResumeOffset, upToChild.at(-1).data.resume_offset);
  });
});

/* A four-turn run like the reported session: turn 2 delegates to one subagent, turn 3 to none,
   turn 4 to another. Each turn's graph shows only the subagents that turn involves. */
function turns(inline, script) {
  let offset = 0;
  const at = (agentId, turn, data) => ({
    event: data.type,
    data: {
      agent_id: agentId,
      turn_id: turn.id,
      turn_number: turn.number,
      message_id: turn.message,
      timestamp: (offset += 1),
      resume_offset: offset,
      replay: true,
      ...data
    }
  });
  const frames = [];
  const childTurns = new Map();
  script.forEach((steps, index) => {
    const number = index + 1;
    const turn = { id: `t${number}`, number, message: `m${number}` };
    const parent = (data) => frames.push(at(rootAgentId, turn, data));
    parent({ type: "message_accepted", handler: "ask", payload: { text: `turn ${number}` }, disposition: "opened" });
    parent({ type: "turn_started", message_id: null });
    for (const [action, key] of steps) {
      const agentId = `${rootAgentId}-${key}`;
      const ids = { subagent_id: agentId, agent_key: key, workflow_id: `wf-${key}`, inline };
      const childTurn = childTurns.get(key) ?? 0;
      const bracket = { ...ids, handler: "Agent", subagent_turn: childTurn };
      const child = (data) =>
        frames.push(at(agentId, { id: `${agentId}-turn-${childTurn}`, number: childTurn, message: `wf-${key}` }, data));
      if (action === "start") {
        parent({ type: "subagent_started", ...ids });
      } else if (action === "send") {
        childTurns.set(key, childTurn + 1);
        bracket.subagent_turn = childTurn + 1;
        parent({ type: "subagent_message_sent", ...bracket, from_offset: 0 });
        frames.push(at(agentId, { id: `${agentId}-turn-${childTurn + 1}`, number: childTurn + 1, message: `wf-${key}` }, {
          type: "turn_started",
          message_id: null
        }));
      } else if (action === "reply") {
        child({ type: "message_handler_end", output: { text: "done" } });
        child({ type: "turn_end", message_id: null });
        parent({ type: "subagent_reply_received", ...bracket, outcome: "ok" });
      } else if (action === "stop") {
        parent({ type: "subagent_stopped", ...ids });
      } else if (action === "unavailable") {
        parent({ type: "subagent_stream_unavailable", subagent_id: agentId, workflow_id: `wf-${key}`, reason: "gone" });
      }
    }
    parent({ type: "message_handler_end", output: { text: `answer ${number}` } });
    parent({ type: "turn_end", message_id: null });
  });
  return frames;
}

/* The subagents each turn's graph draws, with the playhead on that turn's last frame. */
function subagentsByTurn(controller) {
  const ends = controller.frames.flatMap((frame, index) =>
    frame.event === "turn_end" && frame.data.agent_id === rootAgentId ? [index + 1] : []
  );
  return ends.map((end) => {
    controller.goTo(end);
    const runtimes = controller.graph.nodes
      .filter((node) => node.id.endsWith("::agent-runtime") && !node.id.startsWith(parentWorkflowId))
      .map((node) => node.id.split("::")[0]);
    const container = controller.graph.nodes.some((node) => node.data.title === "Subagent activity");
    assert.equal(container, runtimes.length > 0, "the Subagent activity box shows only around subagents");
    return runtimes.sort();
  });
}

describe.each([
  ["inline", true],
  ["child-workflow", false]
])("%s subagents in a multi-turn run", (_name, inline) => {
  it("shows only in the turns that delegated to it", async () => {
    const { controller } = await replay(
      turns(inline, [
        [],
        [["start", "file-finder"], ["send", "file-finder"], ["reply", "file-finder"], ["stop", "file-finder"]],
        [],
        [["start", "general"], ["send", "general"], ["reply", "general"], ["stop", "general"]]
      ])
    );
    assert.deepEqual(subagentsByTurn(controller), [[], ["wf-file-finder"], [], ["wf-general"]]);
  });

  it("shows a long-lived child in each turn that messages it, and parallel children together", async () => {
    const { controller } = await replay(
      turns(inline, [
        [["start", "a"], ["start", "b"], ["send", "a"], ["send", "b"], ["reply", "a"], ["reply", "b"]],
        [],
        [["send", "a"], ["reply", "a"]]
      ])
    );
    assert.deepEqual(subagentsByTurn(controller), [["wf-a", "wf-b"], [], ["wf-a"]]);
  });

  it("shows a child still working when the next turn starts, until it answers", async () => {
    const { controller } = await replay(
      turns(inline, [[["start", "slow"], ["send", "slow"]], [], [["reply", "slow"]], []])
    );
    assert.deepEqual(subagentsByTurn(controller), [["wf-slow"], ["wf-slow"], ["wf-slow"], []]);
  });

  it("shows an unreadable child only in its own turn", async () => {
    const { controller } = await replay(
      turns(inline, [[["start", "lost"], ["send", "lost"], ["unavailable", "lost"], ["reply", "lost"], ["stop", "lost"]], []])
    );
    assert.deepEqual(subagentsByTurn(controller), [["wf-lost"], []]);
  });
});
