// ABOUTME: The guarantee the state pane rests on — one snapshot plus every patch in order IS the
// agent's state — checked on the console's own mock session, so the fixture is a contract and not
// just something to look at. Also pins the three things this projection does that a live-applying
// viewer does not have to: it folds a PREFIX of the run (that is what scrubbing is), it keys state
// per agent (two agents may each register a "plan"), and it refuses to invent a document when the
// stream carried no snapshot, because patches without one are ops against something this client
// has never seen.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { UNSYNCED_NOTE, buildAgentStateDocs } from "./agentState.ts";
import { realisticQaScenario } from "$lib/mock/scenarios.ts";

const meta = (turn, offset) => ({
  agent_id: "7f3c1a",
  turn_id: `turn-${turn}`,
  turn_number: turn,
  timestamp: 1_700_000_000 + offset,
  resume_offset: offset
});

const snapshot = (stateId, value, agentId = "7f3c1a") => ({
  event: "state_snapshot",
  data: {
    ...meta(0, 0),
    agent_id: agentId,
    type: "state_snapshot",
    state_id: stateId,
    version: 0,
    value
  }
});

const patch = (stateId, version, ops, agentId = "7f3c1a") => ({
  event: "state_patch",
  data: {
    ...meta(1, version),
    agent_id: agentId,
    type: "state_patch",
    state_id: stateId,
    version,
    ops
  }
});

/* The mock session's own state frames, in the order they are published. */
const mockStateFrames = realisticQaScenario.frames.filter(
  (frame) => frame.event === "state_snapshot" || frame.event === "state_patch"
);

describe("folding agent state out of the stream", () => {
  it("has state frames in the mock session at all", () => {
    /* An empty fold agrees with every claim below just as cheerfully as a correct
       one, so the floor is what tells the two apart if the fixture is ever thinned. */
    assert.ok(mockStateFrames.length >= 4, `only ${mockStateFrames.length} state frames`);
  });

  it("reproduces the document the mock session's ops describe", () => {
    const [doc] = buildAgentStateDocs(realisticQaScenario.frames);
    assert.equal(doc.stateId, "plan");
    assert.equal(doc.problem, null);
    assert.deepEqual(doc.value, {
      goal: "Model the agent UI on the event stream",
      status: "idle",
      steps: [
        { name: "read the route outline", done: true },
        { name: "compare signals, updates and queries", done: true }
      ],
      scratch: { last_tool: "get_api_outline" },
      tags: ["docs", "forum"]
    });
    /* The harness's own version, not a count of frames: it is 0 at registration
       and +1 per committed mutate(), so it has to equal the last patch's. */
    const last = mockStateFrames.at(-1);
    assert.equal(doc.version, last.data.version);
    assert.equal(doc.commits, mockStateFrames.length - 1);
  });

  it("is a function of the cursor, so scrubbing rewinds the state", () => {
    const frames = realisticQaScenario.frames;
    /* Fold every prefix that ends on a state frame and check the document is that
       commit's, not the run's. This is the difference between a projection and a
       subscription, and the reason the panel can be parked mid-run at all. */
    let seen = 0;
    frames.forEach((frame, index) => {
      if (frame.event !== "state_patch") return;
      seen += 1;
      const [doc] = buildAgentStateDocs(frames.slice(0, index + 1));
      assert.equal(doc.version, frame.data.version);
      assert.equal(doc.commits, seen);
      /* What is marked is what THIS commit changed, with `/-` already resolved. */
      assert.deepEqual(
        doc.changed.map((change) => change.op),
        frame.data.ops.map((op) => op.op)
      );
      assert.ok(doc.changed.every((change) => !change.path.endsWith("/-")));
    });
    assert.ok(seen > 0);
  });

  it("keeps every op a commit carried, even two that name the same path", () => {
    /* The state layer records an op per write and never coalesces per path, so one mutate()
       block can publish two ops with one pointer between them — and not only from a doubled
       assignment: `d.x = 1; d.x = 2`, two `pop(0)`s draining the head of a list, and a 
       `sort()` followed by a `reverse()` each emit two ops at one path without the author
       writing anything twice. AgentStatePanel keyed its change list on `op + path`, which
       collides on every one of those, and Svelte throws `each_key_duplicate` in production
       as well as in dev — with no boundary around the pane, so the console died on a commit
       the agent was perfectly entitled to publish. The panel is unkeyed now; what is pinned
       here is the fold's half of the contract, which is that it does NOT deduplicate. A
       change list that quietly dropped the second op would agree with the panel and lie
       about the commit. */
    const cases = {
      "one field written twice": [
        { op: "replace", path: "/steps/0/done", value: true },
        { op: "replace", path: "/steps/0/done", value: true }
      ],
      "one field, two values": [
        { op: "replace", path: "/status", value: "working" },
        { op: "replace", path: "/status", value: "idle" }
      ],
      "two removes off the head of a list": [
        { op: "remove", path: "/steps/0" },
        { op: "remove", path: "/steps/0" }
      ]
    };
    for (const [name, ops] of Object.entries(cases)) {
      const [doc] = buildAgentStateDocs([
        snapshot("plan", { status: "idle", steps: [{ name: "a" }, { name: "b" }] }),
        patch("plan", 1, ops)
      ]);
      assert.equal(doc.problem, null, name);
      assert.equal(doc.changed.length, 2, `${name}: an op went missing`);
      const keys = new Set(doc.changed.map((change) => change.op + change.path));
      assert.equal(keys.size, 1, `${name}: the two ops stopped sharing a path`);
    }
  });

  it("resolves two appends to different pointers, which is why the key looked safe", () => {
    /* The case that made the collision above hard to believe: `/steps/-` is resolved
       against the array it landed in, so the commonest repeated op in a commit — two
       appends — really does produce two distinct paths. Every other repeat does not. */
    const [doc] = buildAgentStateDocs([
      snapshot("plan", { steps: [] }),
      patch("plan", 1, [
        { op: "add", path: "/steps/-", value: { name: "a" } },
        { op: "add", path: "/steps/-", value: { name: "b" } }
      ])
    ]);
    assert.deepEqual(
      doc.changed.map((change) => change.path),
      ["/steps/0", "/steps/1"]
    );
  });

  it("shows the snapshot alone before any commit has landed", () => {
    const [doc] = buildAgentStateDocs([snapshot("plan", { goal: "", steps: [] })]);
    assert.equal(doc.version, 0);
    assert.equal(doc.commits, 0);
    assert.deepEqual(doc.changed, []);
    assert.deepEqual(doc.value, { goal: "", steps: [] });
  });

  it("keeps two agents' states apart even when they share a name", () => {
    /* A subagent gets its own runner, so `state_id` is unique per AGENT and not
       per run. Keyed on the workflow the timeline attributed the frame to. */
    const docs = buildAgentStateDocs([
      { frame: snapshot("plan", { owner: "root" }), workflowId: "wf-root", label: "Root", role: "parent" },
      { frame: snapshot("plan", { owner: "child" }, "7f3c1a-b52e04"), workflowId: "wf-child", label: "Search", role: "subagent" },
      { frame: patch("plan", 1, [{ op: "replace", path: "/owner", value: "moved" }], "7f3c1a-b52e04"), workflowId: "wf-child", label: "Search", role: "subagent" }
    ]);
    assert.equal(docs.length, 2);
    assert.deepEqual(docs[0].value, { owner: "root" });
    assert.equal(docs[0].version, 0);
    assert.deepEqual(docs[1].value, { owner: "moved" });
    assert.equal(docs[1].label, "Search");
    assert.equal(docs[1].role, "subagent");
  });

  it("refuses to invent a document for patches that arrived with no snapshot", () => {
    /* The real case, not a hypothetical: the snapshot is published once, from
       @workflow.init, so a stream attached past that offset carries only patches.
       Folding them into `{}` would render a plan the agent never had. */
    const docs = buildAgentStateDocs([
      patch("plan", 7, [{ op: "replace", path: "/goal", value: "ship" }]),
      patch("plan", 8, [{ op: "replace", path: "/goal", value: "ship it" }])
    ]);
    assert.equal(docs.length, 1);
    assert.equal(docs[0].value, null);
    assert.equal(docs[0].problem, UNSYNCED_NOTE);
    /* Still counted, so the panel can say how far ahead the agent is. */
    assert.equal(docs[0].version, 8);
    assert.equal(docs[0].commits, 2);
  });

  it("re-bases on a later snapshot, which is how a reconnect resyncs", () => {
    const docs = buildAgentStateDocs([
      patch("plan", 4, [{ op: "replace", path: "/goal", value: "unapplied" }]),
      snapshot("plan", { goal: "resynced" }),
      patch("plan", 1, [{ op: "replace", path: "/goal", value: "and moving again" }])
    ]);
    assert.equal(docs.length, 1);
    assert.equal(docs[0].problem, null);
    assert.deepEqual(docs[0].value, { goal: "and moving again" });
  });

  it("keeps the last good value when an op cannot be applied, and says so", () => {
    const docs = buildAgentStateDocs([
      snapshot("plan", { goal: "ship", steps: [] }),
      patch("plan", 1, [
        { op: "replace", path: "/goal", value: "ship it" },
        { op: "replace", path: "/steps/3/done", value: true }
      ])
    ]);
    /* The half that landed is a state the agent really passed through, so it is
       kept and labelled rather than blanked. */
    assert.equal(docs[0].value.goal, "ship it");
    assert.match(docs[0].problem, /could not be applied/);
  });

  it("never writes through into the frames it was built from", () => {
    /* Every projection in the console rebuilds from the same frame array, so a
       fold that mutated its input would poison the rebuild after it — and the
       symptom would be a document that drifts while nothing is happening. */
    const frames = realisticQaScenario.frames;
    const before = JSON.stringify(mockStateFrames);
    buildAgentStateDocs(frames);
    buildAgentStateDocs(frames);
    assert.equal(JSON.stringify(mockStateFrames), before);
    const [first] = buildAgentStateDocs(frames);
    const [second] = buildAgentStateDocs(frames);
    assert.deepEqual(first.value, second.value);
  });

  it("folds frames that are reactive proxies, which in the running app they always are", () => {
    /* THE BUG THIS EXISTS FOR. `run.frames` is `$state`, so Svelte hands every reader a deep
       Proxy — and `structuredClone` throws DataCloneError on a Proxy. The fold used it to take
       its working copy, so `agentStates` threw for every real session while every test here,
       running on plain objects, passed. It surfaced at whichever reader touched the derived
       first (the pane header asking how many states there were), so the symptom was that
       opening the pane did nothing at all.

       A bare `new Proxy(target, {})` reproduces it exactly: structuredClone refuses a Proxy
       whatever its handler does. */
    const proxied = realisticQaScenario.frames.map(
      (frame) => new Proxy({ ...frame, data: new Proxy({ ...frame.data }, {}) }, {})
    );
    const [doc] = buildAgentStateDocs(proxied);
    const [plain] = buildAgentStateDocs(realisticQaScenario.frames);
    assert.deepEqual(doc.value, plain.value);
    assert.equal(doc.version, plain.version);
    /* And the document is a plain object the caller owns, not a live view of the frames. */
    assert.equal(Object.getPrototypeOf(doc.value), Object.prototype);
  });

  it("ignores a stream error, which carries no state and no type", () => {
    const docs = buildAgentStateDocs([
      { event: "error", data: { kind: "timeout", message: "gone", resume_offset: 3 } },
      snapshot("plan", { goal: "ok" })
    ]);
    assert.equal(docs.length, 1);
  });
});
