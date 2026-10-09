/**
 * Opening the agent picker must re-ask the server who is polling.
 *
 * The picker used to paint a hardcoded "Ready" chip on every agent row — a claim about a
 * worker made from the registry alone, which is the one thing the registry cannot tell you.
 * Clicking an agent whose worker was down produced a session that started and then hung: the
 * child workflow's tasks sat on a task queue nobody polls, and nothing on screen said so.
 *
 * `/api/agents` now carries a `worker` block per agent, sourced from DescribeTaskQueue's
 * pollers. That makes the answer *live*, which is the whole reason this file exists:
 * `#loadAgents` caches the list forever, which is correct for the registry half (the set of
 * launchable agents does not change while the page is open) and wrong for the worker half
 * riding along with it. A worker can come up or go down between two opens of the picker, so
 * neither `ensureAgents` (the picker opening) nor `refreshAgents` (the refresh button) has
 * an age gate — unlike `ensureSessionsEnriched`, which does.
 *
 * What breaks this test, each applied to the source and the failure observed:
 *  - `#loadAgentsFresh` reusing `#loadAgents`'s `if (this.agents.length > 0) return` cache: the
 *    second open keeps the stale "no worker" list and never sees the worker come up.
 *  - Adding an age gate to either: same, whenever two opens land inside the window.
 *  - Dropping the `#agentsLoad` in-flight guard: a double-click fires two lists, and the
 *    older answer can land last.
 *  - `ensureAgents` letting a rejection escape: the picker click raises the connection
 *    banner over the chat pane for a list refresh nobody asked to be loud.
 *  - SessionControls not calling `onEnsureAgents` in `openNewSessionMenu`: the source
 *    assertion below. It finds the function through the Svelte parser rather than by slicing
 *    between two strings, so code landing above or below it cannot empty the slice.
 *
 * Run: npx vitest run src/lib/state/agentWorkerReadiness.test.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parse } from "svelte/compiler";
import { render } from "svelte/server";
import { beforeAll, describe, it } from "vitest";

import StartSessionDialog from "$lib/components/chat/StartSessionDialog.svelte";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const sessionControlsSource = readFileSync(
  fileURLToPath(new URL("../components/chat/SessionControls.svelte", import.meta.url)),
  "utf8"
);

const dialogSource = readFileSync(
  fileURLToPath(new URL("../components/chat/StartSessionDialog.svelte", import.meta.url)), "utf8"
);

/** Every AST node, once — the same walk TranscriptPanel.test.mjs uses. */
function* nodes(root) {
  const seen = new Set();
  const stack = [root];
  while (stack.length > 0) {
    const node = stack.pop();
    if (node == null || typeof node !== "object" || seen.has(node)) continue;
    seen.add(node);
    if (typeof node.type === "string") yield node;
    for (const value of Object.values(node)) {
      if (value != null && typeof value === "object") stack.push(value);
    }
  }
}

/** A function declared in the component's script, start to closing brace, or null. */
function functionSource(source, name) {
  const { instance } = parse(source, { modern: true });
  for (const node of nodes(instance)) {
    if (node.type === "FunctionDeclaration" && node.id?.name === name) {
      return source.slice(node.start, node.end);
    }
  }
  return null;
}

function agent(worker) {
  return {
    key: "monty-chat",
    label: "Monty (Conversational)",
    workflow_type: "MontyChatAgent",
    task_queue: "monty-dynamic-agent",
    description: "Chats.",
    worker
  };
}

const noWorker = { status: "no_worker", task_queue: "monty-dynamic-agent", poller_count: 0, last_seen: null, error: null };
const ready = { status: "ready", task_queue: "monty-dynamic-agent", poller_count: 1, last_seen: 1_789_126_400, error: null };

/** A controller wired to a listAgents that walks `responses`, one per call. */
function controllerOver(responses) {
  const calls = [];
  const controller = new AgentRunController({
    async listAgents() {
      const next = responses[Math.min(calls.length, responses.length - 1)];
      calls.push(next);
      if (next instanceof Error) throw next;
      return { agents: [agent(next)] };
    }
  });
  return { controller, calls };
}

describe("agent worker readiness", () => {
  beforeAll(() => {
    installBrowserSurface();
  });

  it("re-lists on every refresh, so a worker coming up is seen", async () => {
    const { controller, calls } = controllerOver([noWorker, ready]);

    await controller.ensureAgents();
    assert.equal(controller.agents[0].worker.status, "no_worker");

    await controller.ensureAgents();

    assert.equal(calls.length, 2, "second open must hit the server again, not a cache");
    assert.equal(controller.agents[0].worker.status, "ready");
    assert.equal(controller.agents[0].worker.poller_count, 1);
  });

  it("collapses concurrent refreshes into one flight", async () => {
    const { controller, calls } = controllerOver([ready]);

    await Promise.all([controller.ensureAgents(), controller.ensureAgents()]);

    assert.equal(calls.length, 1, "a double-click must not race two lists into `agents`");
  });

  it("keeps the last known list when the refresh fails, and stays quiet", async () => {
    const { controller } = controllerOver([ready, new Error("server down")]);

    await controller.ensureAgents();
    await controller.ensureAgents();

    assert.equal(controller.agents[0].worker.status, "ready", "stale beats empty here");
    assert.equal(
      controller.connectionError,
      null,
      "a picker refresh must not raise the chat pane's connection banner"
    );
  });

  it("opening the agent picker asks for a fresh list", () => {
    assert.match(functionSource(sessionControlsSource, "openNewSessionMenu") ?? "", /onEnsureAgents\?\.\(\)/);
  });

  it("finds that function wherever an $effect lands around it", () => {
    /* The check this replaced sliced up to the first `$effect(() => {` in the file, so an effect
       declared anywhere above the function emptied the slice and failed a correct component. */
    const at = sessionControlsSource.indexOf("function openNewSessionMenu");
    const moved = `${sessionControlsSource.slice(0, at)}$effect(() => {});\n  ${sessionControlsSource.slice(at)}`;
    assert.equal(
      functionSource(moved, "openNewSessionMenu"),
      functionSource(sessionControlsSource, "openNewSessionMenu")
    );
    assert.match(functionSource(sessionControlsSource, "openNewSessionMenu") ?? "", /\}$/, "through its own closing brace");
  });

  it("guards form submission against an unavailable worker", () => {
    assert.match(functionSource(dialogSource, "start") ?? "", /if \(!agent \|\| busy \|\| agentBlocked\(agent\)\) return;/);
    assert.match(functionSource(dialogSource, "agentBlocked") ?? "", /return agent\.worker\?\.status === "no_worker";/);
  });

  it("allows selecting an unavailable agent to explain how to enable it, but disables Start", () => {
    const { body } = render(StartSessionDialog, {
      props: { agents: [agent(noWorker)], onStart() {}, onClose() {} }
    });
    assert.match(body, /type="radio"/);
    assert.match(body, /Start a worker on/);
    assert.match(body, /monty-dynamic-agent/);
    assert.match(body, /aria-label="Copy task queue name"/);
    assert.match(body, /<button[^>]*type="submit"[^>]*disabled/);
  });

  it("permits unknown availability and states the uncertainty", () => {
    const { body } = render(StartSessionDialog, {
      props: { agents: [agent({ ...noWorker, status: "unknown" })], onStart() {}, onClose() {} }
    });
    assert.match(body, /availability could not be checked/);
    assert.doesNotMatch(body.match(/<button[^>]*type="submit"[^>]*>/)?.[0] ?? "", /disabled/);
  });

  it("the refresh control re-checks workers when the agent picker is open", async () => {
    assert.match(dialogSource, /onclick=\{\(\) => void onRefresh\?\.\(\)\}/);
    assert.doesNotMatch(sessionControlsSource, /menuTab/);

    /* Loud, unlike the open: it spins its own control and reports into the popover, because
       a silent no-op would read as "checked, still no worker" — the opposite of the truth. */
    const { controller, calls } = controllerOver([noWorker, ready]);
    await controller.ensureAgents();
    const spinning = [];
    const flight = controller.refreshAgents();
    spinning.push(controller.refreshingAgents);
    await flight;

    assert.deepEqual(spinning, [true], "the button must spin while the re-check is in flight");
    assert.equal(controller.refreshingAgents, false);
    assert.equal(calls.length, 2);
    assert.equal(controller.agents[0].worker.status, "ready");
    assert.equal(controller.agentsError, null);
  });

  it("a failed re-check reports in the picker, not the chat pane", async () => {
    const { controller } = controllerOver([new Error("server down")]);

    await controller.refreshAgents();

    assert.match(controller.agentsError ?? "", /server down/);
    assert.equal(controller.connectionError, null);
    assert.equal(controller.refreshingAgents, false, "the spinner must not stick on failure");
  });

  it("states readiness from the worker block rather than hardcoding Ready", () => {
    assert.doesNotMatch(dialogSource, /<StatusChip\s+label="Ready"/);
    assert.match(dialogSource, /option\.worker\?\.status === "ready"/);
  });
});
