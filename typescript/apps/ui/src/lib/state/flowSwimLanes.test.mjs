// ABOUTME: A message handler that joins the open turn gets its own lane inside the runtime box,
// attributed by the envelope's message_id: its Input, its cards and its reply are its own, and
// the opener's Input, reply and status are left alone. Lanes are outlined only while two or
// more handlers share the turn, and the opener's cards stay where they were when they appear.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentGraph, settledNearCursor } from "./flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, message_id: null, ...data } });
const opener = (event, data = {}) => frame(event, { message_id: "msg-open", turn_number: 1, ...data });
const joined = (id, event, data = {}) => frame(event, { message_id: id, turn_number: 1, ...data });

const search = { tool_id: "t-search", tool_name: "search_flights", tool_input: { to: "JFK" } };
const lookup = { tool_id: "t-lookup", tool_name: "lookup_fares", tool_input: { cabin: "economy" } };

const openerStart = [
  opener("message_accepted", { handler: "ask", payload: { text: "Find me a flight" }, disposition: "opened" }),
  frame("turn_started", { turn_number: 1 }),
  opener("message_handler_start"),
  opener("model_interaction_started", { model: "gemini" }),
  opener("model_interaction_ended", { model: "gemini" }),
  opener("tool_requested", search),
  opener("tool_start", search)
];

const steerJoin = (id, steeringId, text, reply = "") => [
  joined(id, "message_accepted", { handler: "ask", payload: { text }, disposition: "joined" }),
  joined(id, "message_handler_start"),
  joined(id, "steering_enqueued", { steering_id: steeringId, text }),
  joined(id, "message_handler_end", { output: { text: reply } })
];

const byId = (graph, id) => graph.nodes.find((item) => item.id === id);
const lanes = (graph) => graph.nodes.filter((item) => item.type === "agentLane");
const edgeBetween = (graph, source, target) =>
  graph.edges.find((item) => item.source === source && item.target === target);
const bottom = (item) => item.position.y + item.height;

describe("swim lanes for message handlers in one turn", () => {
  it("leaves the opener's Input and the runtime status alone when a handler joins", () => {
    const graph = buildAgentGraph([...openerStart, ...steerJoin("msg-steer", "s1", "cheapest please")]);
    assert.equal(byId(graph, "input").data.detail, "Find me a flight");
    assert.equal(byId(graph, "agent-runtime").data.state, "running");
    assert.equal(graph.status, "running");
    assert.equal(byId(graph, "output"), undefined, "the joined handler's reply is not the opener's Output");
  });

  it("draws no outline while one handler runs, and wires Input to its first card", () => {
    const graph = buildAgentGraph(openerStart);
    assert.deepEqual(lanes(graph), []);
    assert.ok(edgeBetween(graph, "input", "model"));
  });

  it("outlines every lane once a second handler joins, without moving the opener's cards inside the box", () => {
    const before = buildAgentGraph(openerStart);
    const after = buildAgentGraph([...openerStart, ...steerJoin("msg-steer", "s1", "cheapest please")]);
    assert.deepEqual(
      lanes(after).map((item) => item.id),
      ["lane:opener", "lane:msg-steer"]
    );
    for (const id of ["model", "tool:t-search"]) {
      assert.deepEqual(byId(after, id).position, byId(before, id).position, `${id} moved`);
    }
    const [openerLane, steerLane] = lanes(after);
    const box = byId(after, "agent-runtime");
    assert.ok(openerLane.position.y > box.position.y + 88, "the lane sits under the runtime's header");
    assert.ok(steerLane.position.y >= bottom(openerLane), "lanes stack in arrival order");
    assert.ok(bottom(steerLane) <= box.position.y + box.height, "lanes are inside the runtime box");
  });

  it("wires each Input to its own lane rather than to the cards inside it", () => {
    const graph = buildAgentGraph([...openerStart, ...steerJoin("msg-steer", "s1", "cheapest please")]);
    assert.ok(edgeBetween(graph, "input", "lane:opener"));
    assert.ok(edgeBetween(graph, "input:msg-steer", "lane:msg-steer"));
    assert.equal(edgeBetween(graph, "input", "model"), undefined);
    assert.equal(byId(graph, "input:msg-steer").data.detail, "cheapest please");
  });

  it("titles each Input and Output card with its handler", () => {
    const frames = [
      ...openerStart,
      joined("msg-policy", "message_accepted", { handler: "set_approval_policy", payload: { mode: "skip" }, disposition: "joined" }),
      joined("msg-policy", "message_handler_start"),
      joined("msg-policy", "message_handler_end", { output: { text: "Updated." } }),
      opener("tool_end", { ...search, tool_output: "3 flights" }),
      opener("message_handler_end", { output: { text: "Booked." } })
    ];
    const graph = buildAgentGraph(frames);
    assert.equal(byId(graph, "input").data.title, "ask");
    assert.equal(byId(graph, "output").data.title, "ask");
    assert.equal(byId(graph, "input:msg-policy").data.title, "set_approval_policy");
    assert.equal(byId(graph, "output:msg-policy").data.title, "set_approval_policy");
  });

  it("puts the steer card in the steering handler's lane and lights its claim for one beat", () => {
    const frames = [
      ...openerStart,
      ...steerJoin("msg-steer", "s1", "cheapest please"),
      opener("tool_end", { ...search, tool_output: "3 flights" }),
      opener("steering_claimed", { steering_ids: ["s1"], tool_id: search.tool_id })
    ];
    const timeline = frames.map((item) => ({ frame: item }));
    const onClaim = buildAgentGraph(frames, { linger: settledNearCursor(timeline) });
    const steer = byId(onClaim, "steer:s1");
    const steerLane = byId(onClaim, "lane:msg-steer");
    assert.ok(steer.position.y > steerLane.position.y && bottom(steer) <= bottom(steerLane));
    assert.equal(steer.data.state, "claimed");
    assert.equal(steer.data.subtitle, "sent with search_flights");
    assert.equal(steer.data.active, true, "lit on the frame that claims it");
    const claim = edgeBetween(onClaim, "steer:s1", "tool:t-search");
    assert.ok(claim, "a claim edge runs from the steer to the call that carried it");
    assert.equal(claim.class, "edge-steer");

    const next = [...frames, opener("model_interaction_started", { model: "gemini" })];
    const after = buildAgentGraph(next, { linger: settledNearCursor(next.map((item) => ({ frame: item }))) });
    const dimmed = byId(after, "steer:s1");
    assert.equal(dimmed.data.active, false, "dimmed once the run moves on");
    assert.equal(dimmed.data.tone, "neutral");
    assert.equal(after.edges.filter((item) => item.source === "steer:s1").length, 0, "its edge is gone");
  });

  it("marks a dropped steer and draws no claim edge for it", () => {
    const frames = [
      ...openerStart,
      ...steerJoin("msg-steer", "s1", "cheapest please"),
      frame("steering_dropped", { steering_ids: ["s1"] }),
      frame("turn_end", { turn_number: 1 })
    ];
    const graph = buildAgentGraph(frames);
    assert.equal(byId(graph, "steer:s1").data.state, "dropped");
    assert.equal(graph.edges.filter((item) => item.source === "steer:s1").length, 0);
  });

  it("gives a joined handler an Output card as soon as it returns, even with an empty reply", () => {
    const steering = [
      joined("msg-quiet", "message_accepted", { handler: "ask", payload: { text: "cheapest" }, disposition: "joined" }),
      joined("msg-quiet", "message_handler_start"),
      joined("msg-quiet", "steering_enqueued", { steering_id: "s1", text: "cheapest" })
    ];
    const running = buildAgentGraph([...openerStart, ...steering]);
    assert.equal(byId(running, "output:msg-quiet"), undefined, "no Output while the handler runs");

    const silent = buildAgentGraph([
      ...openerStart,
      ...steering,
      joined("msg-quiet", "message_handler_end", { output: { text: "" } })
    ]);
    const output = byId(silent, "output:msg-quiet");
    assert.ok(output, "an empty reply still shows the handler has returned");
    assert.equal(output.data.state, "available");
    assert.deepEqual(JSON.parse(output.data.detail), { text: "" }, "it shows what the handler returned");
    assert.equal(output.data.markdown, false);
    assert.equal(byId(silent, "lane:msg-quiet").data.state, "replied");
    assert.equal(byId(silent, "steer:s1").data.state, "queued", "the steer settles on its own, later");
    assert.ok(edgeBetween(silent, "lane:msg-quiet", "output:msg-quiet"));

    const chatty = buildAgentGraph([...openerStart, ...steerJoin("msg-chat", "s1", "cheapest", "Noted.")]);
    assert.equal(byId(chatty, "output:msg-chat").data.detail, "Noted.");
  });

  it("centres every lane's Input and Output cards on the lane, and on its ports", () => {
    const graph = buildAgentGraph([
      ...openerStart,
      ...steerJoin("msg-chat", "s1", "cheapest", "Noted."),
      ...steerJoin("msg-two", "s2", "window seat", "Noted.")
    ]);
    const middle = (item) => item.position.y + item.height / 2;
    for (const [laneId, inputId, outputId] of [
      ["lane:opener", "input", null],
      ["lane:msg-chat", "input:msg-chat", "output:msg-chat"],
      ["lane:msg-two", "input:msg-two", "output:msg-two"]
    ]) {
      const laneNode = byId(graph, laneId);
      assert.equal(middle(byId(graph, inputId)), middle(laneNode), `${inputId} is centred on ${laneId}`);
      if (outputId) assert.equal(middle(byId(graph, outputId)), middle(laneNode), `${outputId} is centred`);
      const port = laneNode.handles.find((handle) => handle.id === "target-left");
      assert.equal(laneNode.position.y + port.y, middle(laneNode), `${laneId}'s port is at its middle`);
    }
    const inputs = ["input", "input:msg-chat", "input:msg-two"].map((id) => byId(graph, id));
    for (let index = 1; index < inputs.length; index += 1) {
      assert.ok(inputs[index].position.y >= bottom(inputs[index - 1]), "Input cards never overlap");
    }
  });

  it("shrinks a lane with nothing in it to its label row", () => {
    const frames = [
      ...openerStart,
      joined("msg-policy", "message_accepted", { handler: "set_approval_policy", payload: { mode: "skip" }, disposition: "joined" }),
      joined("msg-policy", "message_handler_start"),
      joined("msg-policy", "message_handler_end", { output: { text: "Updated." } })
    ];
    const graph = buildAgentGraph(frames);
    const policyLane = byId(graph, "lane:msg-policy");
    const openerLane = byId(graph, "lane:opener");
    assert.ok(policyLane.height < 60, `an empty lane is a thin band, got ${policyLane.height}px`);
    assert.ok(policyLane.height < openerLane.height / 3);
  });

  it("draws a joined handler's own model and tool work inside its lane", () => {
    const frames = [
      ...openerStart,
      joined("msg-side", "message_accepted", { handler: "ask", payload: { text: "what fares?" }, disposition: "joined" }),
      joined("msg-side", "message_handler_start"),
      joined("msg-side", "model_interaction_started", { model: "gemini" }),
      joined("msg-side", "model_interaction_ended", { model: "gemini" }),
      joined("msg-side", "tool_requested", lookup),
      joined("msg-side", "tool_start", lookup)
    ];
    const graph = buildAgentGraph(frames);
    const sideLane = byId(graph, "lane:msg-side");
    for (const id of ["model:msg-side", "tool:t-lookup"]) {
      const card = byId(graph, id);
      assert.ok(card, `${id} is drawn`);
      assert.ok(card.position.y > sideLane.position.y && bottom(card) <= bottom(sideLane), `${id} is in its lane`);
    }
    assert.ok(edgeBetween(graph, "model:msg-side", "tool:t-lookup"));
    assert.equal(byId(graph, "model").data.state, "finished", "the opener's model card is its own");
  });

  it("nests a call under a running Code Mode script only when both come from the same message", () => {
    const script = { tool_id: "t-code", tool_name: "run_travel_code", tool_input: { script: "await book()" } };
    const hostCall = { tool_id: "t-book", tool_name: "book_flight", tool_input: { id: "FL-1" } };
    const frames = [
      opener("message_accepted", { handler: "ask", payload: { text: "Book it" }, disposition: "opened" }),
      frame("turn_started", { turn_number: 1 }),
      opener("tool_requested", script),
      opener("tool_start", script),
      joined("msg-side", "message_accepted", { handler: "ask", payload: { text: "fares?" }, disposition: "joined" }),
      joined("msg-side", "message_handler_start"),
      joined("msg-side", "tool_requested", lookup),
      joined("msg-side", "tool_start", lookup),
      opener("tool_requested", hostCall),
      opener("tool_start", hostCall)
    ];
    const graph = buildAgentGraph(frames);
    const host = byId(graph, "tool:t-code");
    const sideLane = byId(graph, "lane:msg-side");
    const sideCall = byId(graph, "tool:t-lookup");
    assert.ok(
      sideCall.position.y > sideLane.position.y && bottom(sideCall) <= bottom(sideLane),
      "the joined handler's call is in its own lane"
    );
    const nested = byId(graph, "tool:t-book");
    assert.ok(
      nested.position.y > host.position.y && bottom(nested) <= bottom(host),
      "the opener's own host call is still nested in its script"
    );
  });

  it("keeps the runtime running while a joined handler still runs after the opener replied", () => {
    const frames = [
      ...openerStart,
      joined("msg-side", "message_accepted", { handler: "ask", payload: { text: "status?" }, disposition: "joined" }),
      joined("msg-side", "message_handler_start"),
      opener("tool_end", { ...search, tool_output: "3 flights" }),
      opener("message_handler_end", { output: { text: "Booked." } })
    ];
    const graph = buildAgentGraph(frames);
    assert.equal(graph.status, "running");
    assert.equal(byId(graph, "lane:opener").data.state, "replied");
    assert.equal(byId(graph, "lane:msg-side").data.state, "running");
  });

  it("keeps the lanes until the next turn starts, then clears them", () => {
    const firstTurn = [
      ...openerStart,
      ...steerJoin("msg-steer", "s1", "cheapest please"),
      opener("tool_end", { ...search, tool_output: "3 flights" }),
      opener("message_handler_end", { output: { text: "Booked." } }),
      frame("turn_end", { turn_number: 1 })
    ];
    assert.equal(lanes(buildAgentGraph(firstTurn)).length, 2, "outlines outlast the joined handler");

    const next = buildAgentGraph([
      ...firstTurn,
      frame("message_accepted", { message_id: "msg-2", turn_number: 2, handler: "ask", payload: { text: "And a hotel" }, disposition: "opened" }),
      frame("turn_started", { turn_number: 2 })
    ]);
    assert.deepEqual(lanes(next), []);
    assert.equal(byId(next, "steer:s1"), undefined);
    assert.equal(byId(next, "input:msg-steer"), undefined);
    assert.equal(byId(next, "input").data.detail, "And a hotel");
  });
});
