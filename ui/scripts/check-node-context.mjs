// ABOUTME: Asserts that every kind of graph node gives the inspector something real to show. The
// inspector used to read one field, `detail`, and print "No additional context captured for this
// node." whenever it was unset — which was every model interaction ever rendered, because
// nodeDataFor has never set a `detail` for that kind, while the same turn's reply sat two variables
// away in the same scope. This walks every node kind rather than the one that was reported, since
// the defect was a reachable branch nobody had opened, not a wrong value.
//   node ui/scripts/check-node-context.mjs

import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import "./libAlias.mjs";

const { buildAgentGraph, buildAgentTreeGraph } = await import(
  "../src/lib/state/flowProjection.ts"
);

const frame = (event, data) => ({ event, data: { type: event, ...data } });

/** The node kinds this graph can produce, named the way the ids are built. */
function kindOf(nodeId) {
  const local = nodeId.includes("::") ? nodeId.slice(nodeId.indexOf("::") + 2) : nodeId;
  if (local.startsWith("tool:")) return "tool";
  return local;
}

const EXPECTED_KINDS = new Set([
  "agent-runtime",
  "input",
  "model",
  "reasoning",
  "tool",
  "tool-container",
  "subagent",
  "output"
]);

const toolFrames = (id, name, extra = {}) => [
  frame("tool_requested", { tool_id: id, tool_name: name, tool_input: { q: "lisbon" }, ...extra }),
  frame("tool_start", { tool_id: id, tool_name: name }),
  frame("tool_end", { tool_id: id, tool_name: name, tool_output: "found 3 results" })
];

const fixtures = {
  /* A run with nothing in it still draws the runtime boundary. */
  "empty run": buildAgentGraph([]),

  "full turn": buildAgentGraph([
    frame("turn_started", {
      user_message: '{"type":"ask","payload":{"text":"Plan a two-day trip to Lisbon"}}',
      turn_number: 1
    }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("thought_summary", { delta: { content: { text: "weigh the options" } } }),
    ...toolFrames("t1", "search"),
    frame("subagent_started", { subagent_id: "s1", workflow_id: "wf-1", agent_key: "researcher" }),
    frame("model_interaction_ended", { model: "gpt-5.1" }),
    frame("reply_delta", { text: "Here's a plan" }),
    frame("reply", { output: { text: "Here's a plan" } })
  ]),

  /* The state the screenshot was taken in: a model interaction still RUNNING,
     mid-stream, with no `reply` frame yet. */
  "model still running": buildAgentGraph([
    frame("turn_started", { user_message: "plan something", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("reply_delta", { text: "Here\u2019s a simple, flexible 2-day Lisbon plan" })
  ]),

  /* A model interaction that has not produced one character yet. */
  "model with nothing yet": buildAgentGraph([
    frame("turn_started", { user_message: "plan something", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" })
  ]),

  "code mode": buildAgentGraph([
    frame("turn_started", { user_message: "run it", turn_number: 1 }),
    frame("tool_requested", {
      tool_id: "host",
      tool_name: "code",
      tool_input: { script: "print('hi')" }
    }),
    frame("tool_start", { tool_id: "host", tool_name: "code" }),
    ...toolFrames("child", "read_file"),
    frame("tool_end", { tool_id: "host", tool_name: "code", tool_output: "hi" })
  ]),

  "approval denied": buildAgentGraph([
    frame("turn_started", { user_message: "delete everything", turn_number: 1 }),
    frame("tool_approval_requested", {
      tool_id: "t9",
      tool_name: "rm",
      tool_input: { path: "/" }
    }),
    frame("tool_approval_resolved", {
      tool_id: "t9",
      tool_name: "rm",
      approved: false,
      reason: "denied by operator"
    })
  ]),

  "agent error": buildAgentGraph([
    frame("turn_started", { user_message: "go", turn_number: 1 }),
    frame("model_interaction_started", { model: "gpt-5.1" }),
    frame("error", { message: "upstream refused the request" })
  ]),

  /* A parent with a child, which is the only thing that builds a tool container. */
  "subagent tree": buildAgentTreeGraph([
    {
      workflowId: "parent",
      role: "parent",
      label: "parent run",
      frames: [
        frame("turn_started", { user_message: "delegate this", turn_number: 1 }),
        frame("model_interaction_started", { model: "gpt-5.1" }),
        frame("reply", { output: { text: "delegated" } })
      ]
    },
    {
      workflowId: "child",
      parentWorkflowId: "parent",
      subagentId: "s1",
      agentKey: "researcher",
      role: "subagent",
      label: "child run",
      frames: [
        frame("turn_started", { user_message: "research this", turn_number: 1 }),
        ...toolFrames("c1", "fetch"),
        frame("reply", { output: { text: "done" } })
      ]
    }
  ])
};

// --- every node of every fixture has something real to show -----------------
const seenKinds = new Set();
for (const [name, graph] of Object.entries(fixtures)) {
  assert.ok(graph.nodes.length > 0, `${name}: should build at least one node`);
  for (const node of graph.nodes) {
    seenKinds.add(kindOf(node.id));
    const context = node.data.context;
    assert.ok(
      Array.isArray(context) && context.length > 0,
      `${name}: node "${node.id}" gives the inspector nothing to render`
    );
    for (const section of context) {
      assert.ok(
        typeof section.label === "string" && section.label.trim(),
        `${name}: node "${node.id}" has an unlabelled section`
      );
      assert.ok(
        typeof section.text === "string" && section.text.trim(),
        `${name}: node "${node.id}" section "${section.label}" is empty`
      );
      assert.ok(
        ["text", "code", "json"].includes(section.kind),
        `${name}: node "${node.id}" section "${section.label}" has kind "${section.kind}"`
      );
    }
  }
}

// --- and no kind was left unwalked ------------------------------------------
// The defect was a branch nobody had opened, so a new node kind has to be added
// to EXPECTED_KINDS deliberately — which means someone looked at what it shows.
assert.deepEqual(
  [...seenKinds].sort(),
  [...EXPECTED_KINDS].sort(),
  "the fixtures above must cover every node kind this graph can produce"
);

// --- the reported node, specifically -----------------------------------------
{
  const model = fixtures["model still running"].nodes.find((n) => n.id === "model");
  const labels = model.data.context.map((section) => section.label);
  assert.ok(
    labels.includes("Model output"),
    `a running model interaction should show its output, got ${JSON.stringify(labels)}`
  );
  const output = model.data.context.find((section) => section.label === "Model output");
  assert.match(output.text, /2-day Lisbon plan/, "and it should be the text that streamed");

  /* Even before a single character has arrived, it is a payload rather than an
     apology — the case the old code had no answer for at all. */
  const bare = fixtures["model with nothing yet"].nodes.find((n) => n.id === "model");
  assert.ok(bare.data.context.length > 0, "a silent model interaction still describes itself");
  assert.match(
    bare.data.context.at(-1).text,
    /model_interaction_started/,
    "and what it describes is the frame that created it"
  );
}

// --- a tool keeps both halves of its call ------------------------------------
// `detail` is overwritten by whatever happened last, so the arguments the model
// chose used to be gone the moment the tool answered.
{
  const tool = fixtures["full turn"].nodes.find((n) => n.id === "tool:t1");
  const labels = tool.data.context.map((section) => section.label);
  assert.ok(labels.includes("Tool input"), `a finished tool should still show its input: ${labels}`);
  assert.ok(labels.includes("Tool output"), `and its output: ${labels}`);
}

// --- the message itself is unreachable ---------------------------------------
// Not "no node produces it" — the string is gone from the tree, so no future
// branch can reach for it either.
{
  const root = new URL("../src", import.meta.url).pathname;
  const collect = async (dir) => {
    const entries = await readdir(dir, { withFileTypes: true });
    const files = [];
    for (const entry of entries) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) files.push(...(await collect(path)));
      else if (/\.(svelte|ts)$/.test(entry.name)) files.push(path);
    }
    return files;
  };
  const offenders = [];
  for (const file of await collect(root)) {
    const text = await readFile(file, "utf8");
    if (/No additional context captured/.test(text)) offenders.push(file.slice(root.length));
  }
  assert.deepEqual(offenders, [], "the apology string is still reachable in the tree");
}

console.log(
  `check-node-context: ${seenKinds.size} node kinds, every one of them renders real content`
);
