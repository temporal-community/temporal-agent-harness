/**
 * A subagent's approval gate must be POSTed to the SUBAGENT's session.
 *
 * The gate is a `wait_condition` inside the child workflow, and /api/approve is
 * an update against whatever `session_id` the body names (web/app.py). So the
 * child's own workflow id is the only id that can resolve it: sending the
 * parent's answers `UnknownToolApproval`, measured against the dev stack.
 *
 * But the frame arrives on the PARENT's stream — the merge carries a child's
 * events up, stamped with the child's `agent_id` (the handle the parent pushed
 * down; agent_workflow.py:2961) — so the stream a frame arrived on is exactly
 * the wrong id to answer with, and it is the ambient one every other action in
 * the console uses. Commit f545f27 routed this by the row's own workflow id;
 * nothing pinned it, and the failure it prevents is a permanently unclickable
 * Approve button on the one thing that blocks a run.
 *
 * The routing is a chain, so this drives the whole of it rather than the last
 * link: `subagent_started` teaches the controller the child's workflow id,
 * buildReplayTimeline attributes later frames to it by `agent_id`, and
 * approveTool sends that. A break anywhere reads as the parent's id.
 *
 * What breaks this check, each applied to the source and the failure observed:
 *  - approveTool sending `session_id: session.workflow_id`, the pre-f545f27
 *    body: the child case sends the parent's id.
 *  - buildReplayTimeline tagging every entry `session.workflow_id`: same, one
 *    layer earlier.
 *  - AgentChatPanel calling `onApproveTool(sessionId, ...)`: the source
 *    assertion below, which is the only reachable statement of it — the panel
 *    is a component, and no check here compiles Svelte.
 *  - Dropping approveTool's unknown-workflow guard: the ghost case sends a
 *    stranger's id to /api/approve.
 *
 * Run: node ui/scripts/check-approval-routing.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createCheckServer } from "./checkServer.mjs";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(label, predicate, timeoutMs = 6_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(25);
  }
  assert.fail(`timed out after ${timeoutMs}ms waiting for ${label}`);
}

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
    key: (i) => [...map.keys()][i] ?? null,
    get length() {
      return map.size;
    }
  };
}

const storage = { local: memoryStorage(), session: memoryStorage() };
globalThis.window = {
  get localStorage() {
    return storage.local;
  },
  get sessionStorage() {
    return storage.session;
  },
  setTimeout: (...args) => setTimeout(...args),
  clearTimeout: (...args) => clearTimeout(...args),
  setInterval: (...args) => setInterval(...args),
  clearInterval: (...args) => clearInterval(...args),
  addEventListener: () => {},
  removeEventListener: () => {}
};
globalThis.localStorage = new Proxy({}, { get: (_, p) => storage.local[p] });
globalThis.sessionStorage = new Proxy({}, { get: (_, p) => storage.session[p] });
globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

// --- fixtures ----------------------------------------------------------------

const parentWorkflowId = "agent-session-parent";
const childWorkflowId = "task-subagent-child";
/* Conforms to `AgentId`: one six-wide hex segment for the root, plus one fresh
   segment for the child. isRootAgentEvent() reads exactly that shape, and the
   handle the parent pushes down as the child's `agent_id` is the same string
   `subagent_started` carries as its `subagent_id`. */
const rootAgentId = "de539b";
const childAgentId = "de539b-093b70";
/* The same tool_id on both agents, which is the trap: a decision keyed on the
   tool alone cannot say which workflow it belongs to, and tool ids are only
   unique within an agent. */
const gatedToolId = "tool-write-1";

const session = (id) => ({
  workflow_id: id,
  agent_workflow_type: "CodingAgentWorkflow",
  run_id: `${id}-run`,
  execution_status: "RUNNING",
  closed: false
});

const subagentStarted = (offset) => ({
  event: "subagent_started",
  data: {
    type: "subagent_started",
    agent_id: rootAgentId,
    turn_id: "t1",
    turn_number: 1,
    timestamp: offset,
    resume_offset: offset + 1,
    event_offset: offset,
    subagent_id: childAgentId,
    agent_key: "task",
    workflow_id: childWorkflowId,
    replay: true
  }
});

const approvalRequested = (agentId, offset) => ({
  event: "tool_approval_requested",
  data: {
    type: "tool_approval_requested",
    agent_id: agentId,
    turn_id: "t1",
    turn_number: 1,
    timestamp: offset,
    resume_offset: offset + 1,
    event_offset: offset,
    tool_id: gatedToolId,
    tool_name: "write",
    tool_input: { path: "/workspace/notes.md" },
    replay: true
  }
});

/** A stream that delivers its frames and then stays open, like a live attach. */
const openStream = (frames) =>
  (async function* () {
    for (const frame of frames) yield frame;
    await sleep(50_000);
  })();

// --- harness -----------------------------------------------------------------

const vite = await createCheckServer(import.meta.url);
const { AgentRunController } = await vite.ssrLoadModule(
  "/src/lib/state/agentRun.svelte.ts"
);

const approveCalls = [];
const api = {
  async listSessions() {
    return [];
  },
  async agentInterface() {
    return [];
  },
  async operatorInterface() {
    return [];
  },
  async workflowStatus(workflowId) {
    return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
  },
  attach(sessionId) {
    /* Only the parent's stream carries anything: the child's own attach is
       opened as soon as `subagent_started` lands, and the gate under test came
       up the merge rather than down that stream. */
    return sessionId === parentWorkflowId
      ? openStream([
          subagentStarted(0),
          approvalRequested(childAgentId, 1),
          approvalRequested(rootAgentId, 2)
        ])
      : openStream([]);
  },
  async approve(request) {
    approveCalls.push(request);
    return { tool_id: request.tool_id, accepted: true };
  }
};

const controller = new AgentRunController(api);
controller.sessions = [session(parentWorkflowId)];
void controller.selectSession(parentWorkflowId);
await waitFor("the parent's frames to reach the view", () => controller.frames.length === 3);
await waitFor(
  "the child's workflow id to be learned",
  () => controller.observedSubagents.some((agent) => agent.workflowId === childWorkflowId)
);

// --- the rows the Approve buttons are rendered from --------------------------

const approvalRows = controller.fullReplayLog.rows.filter(
  (row) => row.event === "tool_approval_requested"
);
assert.equal(approvalRows.length, 2, "both gates must reach the log the panel renders");

const [childRow, parentRow] = approvalRows;
assert.equal(
  childRow.workflowId,
  childWorkflowId,
  "a subagent's gate must carry the SUBAGENT's workflow id, not the stream it arrived on"
);
assert.equal(
  parentRow.workflowId,
  parentWorkflowId,
  "the root's own gate must still carry the root's workflow id"
);
assert.notEqual(
  childRow.workflowId,
  parentRow.workflowId,
  "two gates sharing a tool_id are only distinguishable by workflow — keep them keyed by it"
);

// --- what the approve call actually sends ------------------------------------

await controller.approveTool(childRow.workflowId, gatedToolId, true);
assert.deepEqual(
  approveCalls.at(-1),
  {
    session_id: childWorkflowId,
    tool_id: gatedToolId,
    approved: true,
    reason: null,
    remember: false
  },
  "approving a subagent's gate must POST the subagent's session id"
);

await controller.approveTool(parentRow.workflowId, gatedToolId, false);
assert.equal(
  approveCalls.at(-1).session_id,
  parentWorkflowId,
  "the root's own gate must still be answered against the root"
);

const sent = approveCalls.length;
await assert.rejects(
  () => controller.approveTool("agent-session-ghost", gatedToolId, true),
  /unknown agent workflow/,
  "an id belonging to neither this session nor its children must not be sent at all"
);
assert.equal(approveCalls.length, sent, "and nothing must reach /api/approve");

// --- the surface that supplies the id ----------------------------------------
// The one link a node check cannot execute. Both surfaces that offer an Approve
// action route through resolveApproval(), so this is the shared call, and the
// bug is precisely passing the ambient session instead.

const panel = readFileSync(
  fileURLToPath(new URL("../src/lib/components/agent/AgentChatPanel.svelte", import.meta.url)),
  "utf8"
);
assert.match(
  panel,
  /onApproveTool\(\s*approvalWorkflowId\(row\)/,
  "the panel must approve against the row's own workflow, not the selected session"
);

await vite.close();
console.log("approval routing: ok");
process.exit(0);
