import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  buildStepTimeline,
  findWorkflowIdleGaps,
  foldSummary,
  indexAtTimestamp,
  liveIdleGaps,
  LONG_TAIL,
  openByDefault,
  shownWorkflowRows,
  stepTimelineBuilder,
  timestampAtIndex
} from "./stepTimeline.ts";
import { usageGaps } from "../components/flow/usageChart.ts";

const frame = (event, timestamp, data = {}, turnNumber = 1, agentId = "root") => ({
  event,
  data: {
    type: event,
    agent_id: agentId,
    turn_number: turnNumber,
    timestamp,
    ...data
  }
});

const entry = (event, timestamp, data, scope = {}) => ({
  frame: frame(event, timestamp, data, scope.turnNumber, scope.agentId),
  workflowId: scope.workflowId ?? "root-workflow",
  role: scope.role ?? "parent",
  label: scope.label ?? "Agent",
  parentTurnNumber: scope.parentTurnNumber
});

const callback = (event, timestamp, toolId, outcome) =>
  frame(event, timestamp, {
    tool_id: toolId,
    tool_name: "client_lookup",
    ...(outcome ? { outcome, error: outcome === "ok" ? null : `${outcome} detail` } : {})
  });

const rows = (timeline) =>
  Array.from({ length: timeline.workflow.rowCount }, (_, index) => timeline.workflow.rowAt(index));

describe("workflow lifecycle folding", () => {
  it("projects successful, failed, and ongoing callbacks into the shared waterfall spans", () => {
    const timeline = buildStepTimeline([
      frame("turn_started", 0, { user_message: "go" }),
      callback("callback_requested", 1, "ok"),
      callback("callback_resolved", 3, "ok", "ok"),
      callback("callback_requested", 4, "error"),
      callback("callback_resolved", 7, "error", "error"),
      callback("callback_requested", 8, "pending"),
      frame("reply_delta", 11, { delta: "waiting" })
    ]);
    const callbacks = timeline.turns[0].spans.filter((span) => span.waitType === "callback");

    assert.deepEqual(
      callbacks.map(({ label, tone, durationSeconds, ongoing, detail }) => ({
        label,
        tone,
        durationSeconds,
        ongoing,
        detail
      })),
      [
        {
          label: "callback · client_lookup",
          tone: "done",
          durationSeconds: 2,
          ongoing: false,
          detail: undefined
        },
        {
          label: "callback · client_lookup",
          tone: "error",
          durationSeconds: 3,
          ongoing: false,
          detail: "error detail"
        },
        {
          label: "callback · client_lookup",
          tone: "approval",
          durationSeconds: 3,
          ongoing: true,
          detail: undefined
        }
      ]
    );
    assert.ok(
      callbacks.every((span) => span.kind === "approval"),
      "callbacks reuse the waterfall's approval-colored wait family"
    );
  });

  it("closes subagent lifecycles under a stable key and leaves open ones ongoing", () => {
    const fold = stepTimelineBuilder();
    const frames = [
      entry("subagent_started", 2, {
        workflow_id: "child-a-workflow",
        subagent_id: "child-a",
        agent_key: "researcher"
      }),
      entry("reply_delta", 5, { delta: "parent progressed" })
    ];

    const open = fold(frames).workflow.subagents[0];
    assert.deepEqual(open, {
      key: "subagent:root-workflow:1:child-a",
      source: "subagent",
      kind: "subagent",
      label: "researcher (child-a)",
      detail: "child-a-workflow",
      tone: "model",
      startTs: 2,
      endTs: 5,
      startIndex: 1,
      endIndex: 2,
      ongoing: true,
      parentTurnNumber: 1,
      workflowId: "child-a-workflow",
      subagentId: "child-a",
      depth: 0
    });

    frames.push(
      entry("subagent_stopped", 8, {
        workflow_id: "child-a-workflow",
        subagent_id: "child-a",
        agent_key: "researcher"
      })
    );
    const closed = fold(frames).workflow.subagents[0];
    assert.equal(closed.key, open.key, "closing must not replace the lifecycle identity key");
    assert.equal(closed.endTs, 8);
    assert.equal(closed.endIndex, 3);
    assert.equal(closed.ongoing, false);
    assert.equal(closed.tone, "done");
    assert.equal(fold(frames).turns[0].spans.length, 0, "synthetic rows stay out of the waterfall");
  });

  it("resets lifecycle state when the input is replaced", () => {
    const fold = stepTimelineBuilder();
    fold([
      entry("subagent_started", 1, {
        workflow_id: "old-workflow",
        subagent_id: "old",
        agent_key: "old-agent"
      })
    ]);

    const replacement = [
      entry("subagent_started", 4, {
        workflow_id: "new-workflow",
        subagent_id: "new",
        agent_key: "new-agent"
      })
    ];
    assert.deepEqual(
      fold(replacement).workflow.subagents.map(({ workflowId }) => workflowId),
      ["new-workflow"]
    );
  });

  it("keeps callback and subagent ids separate across workflow scopes", () => {
    const scopes = [
      { workflowId: "parent-a", role: "parent", turnNumber: 1, parentTurnNumber: 1 },
      {
        workflowId: "parent-b",
        role: "subagent",
        turnNumber: 1,
        parentTurnNumber: 2,
        agentId: "parent-b"
      }
    ];
    const frames = scopes.flatMap((scope, index) => [
      entry(
        "callback_requested",
        index * 10,
        { tool_id: "same-tool", tool_name: "lookup", input: {}, output_schema: {} },
        scope
      ),
      entry(
        "subagent_started",
        index * 10 + 1,
        {
          workflow_id: `child-${index}`,
          subagent_id: "same-child",
          agent_key: "worker"
        },
        scope
      ),
      entry(
        "callback_resolved",
        index * 10 + 2,
        {
          tool_id: "same-tool",
          tool_name: "lookup",
          outcome: "ok",
          error: null
        },
        scope
      )
    ]);
    const timeline = buildStepTimeline(frames);

    assert.deepEqual(
      timeline.turns.flatMap((turn) => [
        ...turn.spans.filter((span) => span.waitType === "callback"),
        ...turn.subagentTurns.flatMap((sub) =>
          sub.spans.filter((span) => span.waitType === "callback")
        )
      ]).map(({ durationSeconds }) => durationSeconds),
      [2, 2]
    );
    assert.deepEqual(
      timeline.workflow.subagents.map(({ key }) => key),
      ["subagent:parent-a:1:same-child", "subagent:parent-b:2:same-child"]
    );
  });

  it("matches a full rebuild after incremental appends without replacing untouched rows", () => {
    const frames = [
      frame("model_interaction_started", 1, { model: "first" }, 1),
      frame("model_interaction_ended", 2, {}, 1)
    ];
    const fold = stepTimelineBuilder();
    const first = fold(frames);
    const untouched = first.workflow.rowAt(0);

    frames.push(
      frame("model_interaction_started", 3, { model: "second" }, 2),
      frame("model_interaction_ended", 5, {}, 2)
    );
    const incremental = fold(frames);
    const rebuilt = buildStepTimeline(frames);

    assert.deepEqual(rows(incremental), rows(rebuilt));
    assert.equal(incremental.workflow.rowAt(0), untouched);
    assert.deepEqual(incremental.workflow.points, [
      [1, 1],
      [2, 2],
      [3, 3],
      [4, 5]
    ]);
    assert.equal(timestampAtIndex(incremental.workflow.points, 3.5), 4);
    assert.equal(indexAtTimestamp(incremental.workflow.points, 4), 4);
  });

  it("uses scope-qualified stable keys and nests subagent spans below their lifecycle", () => {
    const timeline = buildStepTimeline([
      entry("model_interaction_started", 1, { model: "parent" }),
      entry("model_interaction_ended", 2, {}),
      entry("subagent_started", 3, {
        workflow_id: "child-workflow",
        subagent_id: "child",
        agent_key: "researcher"
      }),
      entry(
        "tool_start",
        4,
        { tool_id: "search", tool_name: "search" },
        { role: "subagent", workflowId: "child-workflow", parentTurnNumber: 1 }
      ),
      entry(
        "tool_end",
        6,
        { tool_id: "search", tool_name: "search" },
        { role: "subagent", workflowId: "child-workflow", parentTurnNumber: 1 }
      ),
      entry("subagent_stopped", 7, {
        workflow_id: "child-workflow",
        subagent_id: "child",
        agent_key: "researcher"
      })
    ]);
    const projected = rows(timeline);

    assert.match(projected[0].key, /^parent:1:span:model-/);
    assert.equal(projected[1].key, "subagent:root-workflow:1:child");
    assert.match(projected[2].key, /^subagent:child-workflow:1:1:span:tool-/);
    assert.deepEqual(
      projected.map(({ source, depth }) => [source, depth]),
      [
        ["span", 0],
        ["subagent", 0],
        ["span", 1]
      ]
    );
  });

  it("keeps a span key stable while its ongoing row closes", () => {
    const fold = stepTimelineBuilder();
    const frames = [
      frame("model_interaction_started", 1, { model: "streaming" }),
      frame("reply_delta", 2, { delta: "working" })
    ];
    const openKey = fold(frames).workflow.rowAt(0).key;

    frames.push(frame("model_interaction_ended", 3));
    const closed = fold(frames).workflow.rowAt(0);
    assert.equal(closed.key, openKey);
    assert.equal(closed.ongoing, false);
  });

  it("gives automatic approval evaluations their own workflow rows", () => {
    const timeline = buildStepTimeline([
      frame("tool_approval_requested", 1, { tool_id: "tool", tool_name: "shell" }),
      frame("auto_approval_evaluation_started", 2, {
        tool_id: "tool",
        evaluation_id: "eval",
        evaluator: "policy"
      }),
      frame("auto_approval_evaluation_ended", 3, {
        tool_id: "tool",
        evaluation_id: "eval",
        evaluator: "policy",
        verdict: "approve"
      }),
      frame("tool_approval_resolved", 4, {
        tool_id: "tool",
        tool_name: "shell",
        approved: true
      })
    ]);
    const waits = rows(timeline).filter(({ waitType }) => waitType);

    assert.deepEqual(
      waits.map(({ kind, waitType, label }) => [kind, waitType, label]),
      [
        ["approval", "approval", "approval · shell"],
        ["evaluation", "evaluation", "approval check · policy"]
      ]
    );
    assert.equal(new Set(waits.map(({ key }) => key)).size, 2);
  });

  it("nests a tool's gate and checks under it, and subagents under the agent that started them", () => {
    const child = { role: "subagent", workflowId: "child-workflow", parentTurnNumber: 1 };
    const grandchild = { role: "subagent", workflowId: "grandchild-workflow", parentTurnNumber: 1 };
    const gated = { tool_id: "book", tool_name: "book_flight" };
    const timeline = buildStepTimeline([
      entry("model_interaction_started", 1, { model: "parent" }),
      entry("model_interaction_ended", 2, {}),
      entry("tool_approval_requested", 3, gated),
      entry("auto_approval_evaluation_started", 4, { ...gated, evaluation_id: "e1", evaluator: "policy" }),
      entry("auto_approval_evaluation_ended", 5, { ...gated, evaluation_id: "e1", evaluator: "policy", verdict: "approve" }),
      entry("tool_approval_resolved", 6, { ...gated, approved: true }),
      entry("tool_start", 7, gated),
      entry("tool_end", 8, gated),
      entry("subagent_started", 9, { workflow_id: "child-workflow", subagent_id: "child", agent_key: "researcher" }),
      entry("subagent_started", 10, { workflow_id: "grandchild-workflow", subagent_id: "grandchild", agent_key: "checker" }, child),
      entry("tool_start", 11, { tool_id: "look", tool_name: "lookup" }, grandchild),
      entry("tool_end", 12, { tool_id: "look", tool_name: "lookup" }, grandchild),
      entry("subagent_stopped", 13, { workflow_id: "grandchild-workflow", subagent_id: "grandchild", agent_key: "checker" }, child),
      entry("subagent_stopped", 14, { workflow_id: "child-workflow", subagent_id: "child", agent_key: "researcher" })
    ]);
    const projected = rows(timeline);
    const byKey = new Map(projected.map((row) => [row.key, row]));

    assert.deepEqual(
      projected.map(({ label, depth }) => [label, depth]),
      [
        ["parent", 0],
        ["book_flight", 0],
        ["approval", 1],
        ["approval check · policy", 2],
        ["researcher (child)", 0],
        ["checker (grandchild)", 1],
        ["lookup", 2]
      ]
    );
    for (const row of projected) {
      if (row.parentKey) assert.equal(byKey.get(row.parentKey).depth, row.depth - 1, row.label);
      else assert.equal(row.depth, 0, row.label);
    }
    assert.equal(projected[1].startIndex, 7, "a tool row still seeks to where its tool started");
  });

  it("folds a collapsed row's descendants away and counts its direct children", () => {
    const depths = [0, 1, 2, 2, 1, 0, 1];
    const workflow = {
      rowCount: depths.length,
      rowAt: (index) =>
        index < depths.length
          ? { key: `r${index}`, kind: "tool", depth: depths[index], tone: "done", ongoing: false, startTs: index, endTs: index + 1 }
          : undefined
    };
    const folds = (folded) => new Map(depths.map((_, index) => [`r${index}`, folded.includes(`r${index}`)]));
    assert.deepEqual(shownWorkflowRows(workflow, folds([])).indexes, [0, 1, 2, 3, 4, 5, 6]);
    assert.deepEqual(shownWorkflowRows(workflow, folds(["r0"])), {
      indexes: [0, 5, 6],
      hidden: { 0: 2 },
      summaries: { 0: "2 tools" },
      collapsed: new Set(["r0"])
    });
    assert.deepEqual(shownWorkflowRows(workflow, folds(["r1", "r5"])), {
      indexes: [0, 1, 4, 5],
      hidden: { 1: 2, 5: 1 },
      summaries: { 1: "2 tools", 5: "1 tool" },
      collapsed: new Set(["r1", "r5"])
    });
    const leaf = shownWorkflowRows(workflow, folds(["r2"]));
    assert.deepEqual(leaf.hidden, {}, "a collapsed leaf hides nothing and shows no count");
    assert.deepEqual(leaf.collapsed, new Set(), "and is not folded");
  });

  it("folds routine groups by default and opens any that hold a failure or a wait", () => {
    /* tool > approval > approval check, a done subagent > its model call, and a subagent
       whose grandchild callback is still waiting. */
    const rows = [
      { key: "tool", kind: "tool", depth: 0 },
      { key: "approval", kind: "approval", depth: 1 },
      { key: "check", kind: "evaluation", depth: 2 },
      { key: "done-agent", kind: "subagent", depth: 0 },
      { key: "done-model", kind: "model", depth: 1 },
      { key: "live-agent", kind: "subagent", depth: 0, ongoing: true },
      { key: "live-child", kind: "tool", depth: 1, ongoing: true },
      { key: "callback", kind: "approval", waitType: "callback", depth: 2, ongoing: true },
      { key: "failed-host", kind: "tool", depth: 0 },
      { key: "failed-check", kind: "evaluation", depth: 1, tone: "error" }
    ].map((row, index) => ({ tone: "done", ongoing: false, startTs: index, endTs: index + 1, ...row }));
    const workflow = { rowCount: rows.length, rowAt: (index) => rows[index] };
    const keys = (tree) => tree.indexes.map((index) => rows[index].key);

    const tree = shownWorkflowRows(workflow);
    assert.deepEqual(keys(tree), ["tool", "done-agent", "live-agent", "live-child", "callback", "failed-host", "failed-check"]);
    assert.deepEqual(tree.collapsed, new Set(["tool", "done-agent"]));
    assert.deepEqual(tree.hidden, { 0: 1, 3: 1 }, "a folded row still counts its children");

    const reader = shownWorkflowRows(workflow, new Map([["tool", false], ["live-agent", true]]));
    assert.deepEqual(keys(reader), ["tool", "approval", "done-agent", "live-agent", "failed-host", "failed-check"],
      "the reader's fold wins both ways, and the routine approval under the opened tool keeps its own default");
    assert.deepEqual(reader.collapsed, new Set(["approval", "done-agent", "live-agent"]));
  });
});

describe("the default fold policy", () => {
  /* Two routine groups of 1s calls, so neither is the only parent and none is a long tail:
     a tool gated by a granted approval, and a subagent's model and tool calls. */
  const routine = () =>
    [
      { key: "gated", kind: "tool", depth: 0, startTs: 0, endTs: 1 },
      { key: "gate", kind: "approval", waitType: "approval", depth: 1, startTs: 1, endTs: 2 },
      { key: "agent", kind: "subagent", depth: 0, startTs: 2, endTs: 9 },
      { key: "think", kind: "model", depth: 1, startTs: 2, endTs: 3 },
      { key: "search", kind: "tool", depth: 1, startTs: 3, endTs: 4 },
      { key: "fetch", kind: "tool", depth: 1, startTs: 4, endTs: 5 },
      { key: "reply", kind: "model", depth: 1, startTs: 5, endTs: 6 },
      { key: "book", kind: "tool", depth: 1, startTs: 6, endTs: 7 }
    ].map((row) => ({ tone: "done", ongoing: false, ...row }));
  const opened = (rows, at) => {
    const open = openByDefault(rows, at);
    return rows.filter((_, index) => open[index]).map((row) => row.key);
  };
  const edit = (rows, key, patch) => Object.assign(rows.find((row) => row.key === key), patch);

  it("folds every group of finished routine calls", () => {
    assert.deepEqual(opened(routine()), []);
  });

  it("opens a parent that is running, or holds a call still running", () => {
    const rows = routine();
    edit(rows, "agent", { ongoing: true });
    assert.deepEqual(opened(rows), ["agent"]);
    const child = routine();
    edit(child, "fetch", { ongoing: true });
    assert.deepEqual(opened(child), ["agent"]);
  });

  it("opens a parent with a wait on a person still pending", () => {
    const rows = routine();
    edit(rows, "gate", { ongoing: true, tone: "approval" });
    assert.deepEqual(opened(rows), ["gated"]);
  });

  it("opens a parent that holds a failure", () => {
    const rows = routine();
    edit(rows, "search", { tone: "error" });
    assert.deepEqual(opened(rows), ["agent"]);
  });

  it("opens a parent that holds a long tail, at 3× the median call and 10% of the run", () => {
    const rows = routine();
    edit(rows, "gate", { endTs: 60 });
    edit(rows, "agent", { startTs: 60, endTs: 70 });
    for (const [index, key] of ["think", "search", "fetch", "reply", "book"].entries()) {
      edit(rows, key, { startTs: 60 + index, endTs: 61 + index });
    }
    assert.deepEqual(opened(rows), ["gated"], "the 59s gate among 1s calls, the user's 21m approval");
    edit(rows, "fetch", { endTs: 64 + LONG_TAIL.factor - 0.5 });
    assert.deepEqual(opened(rows), ["gated"], "under 10% of the run is not a tail, however slow beside the median");
  });

  it("opens the only parent in a run", () => {
    assert.deepEqual(opened(routine().slice(2)), ["agent"]);
  });

  it("opens a parent at a cursor while it was running, and folds it again past its end", () => {
    const rows = routine();
    assert.deepEqual(opened(rows, 4.5), ["agent"], "scrubbed back into the subagent");
    assert.deepEqual(opened(rows, 9.5), []);
  });

  it("keeps the reader's fold over the default, both ways, at any cursor", () => {
    const rows = routine();
    edit(rows, "fetch", { tone: "error" });
    const workflow = { rowCount: rows.length, rowAt: (index) => rows[index] };
    const reader = new Map([["agent", true], ["gated", false]]);
    for (const at of [undefined, 0.5, 4.5]) {
      const tree = shownWorkflowRows(workflow, reader, at);
      assert.ok(tree.collapsed.has("agent"), `the failure does not reopen what the reader folded (at ${at})`);
      assert.ok(!tree.collapsed.has("gated"), `nor fold what the reader opened (at ${at})`);
    }
  });

  it("reads the run once and hands a scrub that opens nothing new the same rows back", () => {
    /* 50,000 rows: a gated tool every 10th and a subagent with three calls every 100th, each
       call 1s and the run's one failure deep in it, so the median sort is real work. */
    const rows = [];
    for (let i = 0; rows.length < 50_000; i += 1) {
      const t = i * 2;
      rows.push({ key: `t${i}`, kind: i % 100 === 0 ? "subagent" : "tool", depth: 0, startTs: t, endTs: t + 1 });
      if (i % 10 === 0) rows.push({ key: `g${i}`, kind: "approval", waitType: "approval", depth: 1, startTs: t, endTs: t + 0.5 });
      if (i % 100 === 0) for (const k of [0, 1, 2]) rows.push({ key: `c${i}-${k}`, kind: "model", depth: 1, startTs: t + k / 4, endTs: t + k / 4 + 0.2 });
    }
    for (const row of rows) Object.assign(row, { tone: row.key === "c2500-1" ? "error" : "done", ongoing: false });
    const workflow = { rowCount: rows.length, rowAt: (index) => rows[index] };
    const folds = new Map();

    let started = performance.now();
    const first = shownWorkflowRows(workflow, folds, 3.5);
    const firstCost = performance.now() - started;
    const routineStep = shownWorkflowRows(workflow, folds, 6.5);
    assert.equal(routineStep, first, "a cursor move that changes no fold returns the same object");
    assert.notEqual(shownWorkflowRows(workflow, folds, 200.1), first, "one into a subagent opens it");
    assert.deepEqual(
      shownWorkflowRows(workflow, folds, 200.1).indexes.length - first.indexes.length,
      4,
      "and shows its approval and three calls"
    );

    /* The same comparison the projection cost check makes: work against work in one
       process, so a loaded machine slows both sides. Rebuilding per step costs 100x. */
    started = performance.now();
    for (let step = 0; step < 100; step += 1) shownWorkflowRows(workflow, folds, step * 1_000 + 2.7);
    const scrubCost = performance.now() - started;
    assert.ok(
      scrubCost < 25 * firstCost,
      `100 scrub steps cost ${(scrubCost / firstCost).toFixed(1)}x the first read (${firstCost.toFixed(1)}ms)`
    );
  });

  it("summarises a folded row's direct children by kind, most first", () => {
    const tree = shownWorkflowRows({ rowCount: 8, rowAt: (index) => routine()[index] });
    assert.deepEqual(tree.summaries, { 0: "1 approval", 2: "3 tools · 2 models" });
    assert.equal(
      foldSummary([{ kind: "approval", waitType: "callback" }, { kind: "evaluation" }, { kind: "subagent" }, { kind: "subagent" }]),
      "2 agents · 1 callback · 1 check"
    );
    assert.throws(() => foldSummary([{ kind: "workflow" }]), /Unknown timeline row kind/);
  });
});

describe("workflow idle gaps", () => {
  it("keeps incremental gaps equal to a full row recompute, including late history", () => {
    const fold = stepTimelineBuilder();
    const frames = [
      frame("turn_started", 0),
      frame("model_interaction_started", 100, { model: "parent" }),
      frame("model_interaction_ended", 110)
    ];
    fold(frames);
    frames.push(
      entry(
        "tool_start",
        20,
        { tool_id: "late", tool_name: "late" },
        { role: "subagent", workflowId: "child", parentTurnNumber: 1 }
      ),
      entry(
        "tool_end",
        30,
        { tool_id: "late", tool_name: "late" },
        { role: "subagent", workflowId: "child", parentTurnNumber: 1 }
      )
    );

    const incremental = fold(frames);
    assert.deepEqual(
      incremental.workflow.gaps,
      findWorkflowIdleGaps(rows(incremental), incremental.workflow.bounds)
    );
  });

  it("collapses leading idle time before the first span", () => {
    const timeline = buildStepTimeline([
      frame("turn_started", 0),
      frame("model_interaction_started", 60, { model: "late" }),
      frame("model_interaction_ended", 65),
      frame("reply_delta", 70, { delta: "done" })
    ]);

    assert.deepEqual(timeline.workflow.gaps, [
      { key: "idle:0:60", startTs: 0, endTs: 60 }
    ]);
  });

  it("merges overlapping and touching activity before selecting idle gaps", () => {
    const timeline = buildStepTimeline([
      frame("turn_started", 0),
      frame("model_interaction_started", 10, { model: "one" }),
      frame("tool_start", 15, { tool_id: "tool", tool_name: "tool" }),
      frame("model_interaction_ended", 20),
      frame("tool_end", 25, { tool_id: "tool", tool_name: "tool" }),
      frame("model_interaction_started", 25, { model: "two" }),
      frame("model_interaction_ended", 35),
      frame("reply_delta", 40, { delta: "done" })
    ]);

    assert.deepEqual(timeline.workflow.gaps, [
      { key: "idle:0:10", startTs: 0, endTs: 10 },
      { key: "idle:35:40", startTs: 35, endTs: 40 }
    ], "the 10s call from 25 to 35 is a quarter of the run, but a call, not a pause");
  });

  /* A burst, 53 minutes with only an open subagent and a wait running, a second burst. */
  const quietRun = () => [
    frame("turn_started", 0),
    frame("model_interaction_started", 1320, { model: "early" }),
    frame("model_interaction_ended", 1326),
    frame("tool_approval_requested", 1327, { tool_id: "gate", tool_name: "monty_run_script" }),
    frame("subagent_started", 1328, { subagent_id: "watch", agent_key: "watcher", workflow_id: "wf-watch" }),
    frame("reply_delta", 3000, { delta: "." }),
    frame("tool_approval_resolved", 4570, { tool_id: "gate", tool_name: "monty_run_script", approved: true }),
    frame("model_interaction_started", 4571, { model: "late" }),
    frame("model_interaction_ended", 4577)
  ];

  it("folds a long quiet stretch that an open span and a long wait run through", () => {
    const timeline = buildStepTimeline(quietRun());
    assert.deepEqual(timeline.workflow.gaps, [
      { key: "idle:0:1320", startTs: 0, endTs: 1320 },
      { key: "idle:1328:4570", startTs: 1328, endTs: 4570 }
    ]);
    assert.deepEqual(
      timeline.workflow.gaps,
      findWorkflowIdleGaps(rows(timeline), timeline.workflow.bounds),
      "a full recompute from the rows agrees"
    );
    const crossing = rows(timeline).filter((row) => row.startTs <= 1328 && (row.ongoing || row.endTs >= 4570));
    assert.deepEqual(crossing.map((row) => row.kind).sort(), ["approval", "subagent"]);
    assert.equal(crossing.find((row) => row.kind === "approval").durationSeconds, 3243, "true duration kept");
  });

  it("folds the token chart over the same stretch", () => {
    const timeline = buildStepTimeline(quietRun());
    assert.deepEqual(usageGaps(timeline, 1320), [
      { key: "idle:0:1320", startTs: -1320, endTs: 0 },
      { key: "idle:1328:4570", startTs: 8, endTs: 3250 }
    ]);
  });

  it("folds an open session's quiet tail on to now by the same rule", () => {
    const { workflow } = buildStepTimeline(quietRun().slice(0, 5));
    assert.equal(workflow.bounds.endTs, 1328);
    const near = liveIdleGaps(workflow.gaps, workflow.bounds, 1328 + 30);
    assert.equal(near, workflow.gaps, "half a minute under an open span is not idle yet");
    const tail = liveIdleGaps(workflow.gaps, workflow.bounds, 4000).at(-1);
    assert.deepEqual(tail, { key: "idle:1328:now", startTs: 1328, endTs: 4000 });
    assert.equal(liveIdleGaps(workflow.gaps, workflow.bounds, 4100).at(-1).key, tail.key, "one key while now moves");
    assert.equal(liveIdleGaps(workflow.gaps, workflow.bounds, null), workflow.gaps, "closed: the frames' own gaps");
  });
});
