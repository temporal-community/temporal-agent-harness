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

import SessionControls from "$lib/components/chat/SessionControls.svelte";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { chooseAgentRow } from "./quickSwitch.ts";

const sessionControlsSource = readFileSync(
  fileURLToPath(new URL("../components/chat/SessionControls.svelte", import.meta.url)),
  "utf8"
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

  it("refuses a no-worker agent at the press, not only in the markup", () => {
    /* `aria-disabled` styles a row inert but does not stop it activating — a keyboard
       Enter still fires onclick — so the refusal has to live in the handler too. */
    const start = functionSource(sessionControlsSource, "startNewSession") ?? "";
    assert.match(start, /if \(agentBlocked\(agent\)\) return;/);
    assert.match(start, /startNewSession\(agent: AgentDescriptor\)/);
    /* Only no_worker blocks. `unknown` means the server could not ask, and refusing on a
       failed health check would be a worse lie than the hardcoded "Ready" this replaced. */
    assert.match(functionSource(sessionControlsSource, "agentBlocked") ?? "", /return agent\.worker\?\.status === "no_worker";/);
  });

  /* The New session rows are two lines each, name and description, with the readiness chip on the
     right. Where the queue is named: on the chip's tooltip, and in the hint a press opens. */
  const agentsOnScreen = [
    { ...agent(noWorker), key: "openai-hello", task_queue: "openai-hello", label: "OpenAI Hello" },
    { ...agent(ready), key: "monty", label: "Monty" },
    { ...agent({ ...noWorker, status: "unknown" }), key: "wiki", task_queue: "wiki-queue", label: "Wiki" }
  ];
  const { body } = render(SessionControls, {
    props: { display: "pane", tab: "new", agents: agentsOnScreen, sessionId: "" }
  });
  const rows = [...body.matchAll(/<button[^>]*class="agent-row[^"]*"[\s\S]*?<\/button>/g)].map(([row]) => row);

  it("renders no icon and no sentence under the description", () => {
    assert.equal(rows.length, 3);
    assert.doesNotMatch(body, /agent-glyph/, "the icon repeated the name's initial and the chip's colour");
    assert.doesNotMatch(body, />[^<]*No worker polling/, "the queue is named on the chip's tip, not in a line of text");
    assert.doesNotMatch(body, /agent-note/);
    for (const row of rows) {
      assert.equal([...row.matchAll(/<small/g)].length, 1, "every row is the same two lines");
      assert.doesNotMatch(row, /(?<!aria-)disabled=/, "native `disabled` would swallow the press that opens the hint");
    }
  });

  it("names the queue in the chip's tooltip", () => {
    assert.match(rows[0], /data-tip="No worker polling openai-hello"/);
    assert.doesNotMatch(rows[1], /data-tip=/, "Ready says everything it needs to");
    assert.match(rows[2], /data-tip="Could not check for workers on wiki-queue"/);
    assert.match(rows[0], /<small title="Chats\."[^>]*>Chats\.<\/small>/, "the clipped description is whole on hover");
  });

  it("a press on a no-worker row opens one hint, with the queue to copy", () => {
    /* No DOM here, so the choice is driven through the shipped function and the markup is read
       for the one place the hint renders. */
    let open = null;
    const choose = (row) => {
      const choice = chooseAgentRow(row.key, row.worker.status === "no_worker");
      open = choice.hint;
      return choice.start;
    };
    assert.equal(choose(agentsOnScreen[0]), false, "a no-worker row starts nothing");
    assert.equal(open, "openai-hello");
    assert.equal(choose(agentsOnScreen[2]), true, "unknown still starts: a failed check is not a locked door");
    assert.equal(open, null, "choosing another row clears the hint");

    assert.match(sessionControlsSource, /onclick=\{\(\) => chooseAgent\(agent\)\}/, "the click goes through it");
    assert.match(functionSource(sessionControlsSource, "handleAgentListKeydown") ?? "", /chooseAgent\(/, "and so does Enter");
    const hint = sessionControlsSource.slice(
      sessionControlsSource.indexOf("{#if hintedAgentKey === agent.key}"),
      sessionControlsSource.indexOf("{/if}", sessionControlsSource.indexOf("{#if hintedAgentKey === agent.key}"))
    );
    assert.match(hint, /Start a worker on/);
    assert.match(hint, /<Copyable value=\{agent\.task_queue\}/, "the queue name is the thing a terminal needs");
    assert.match(hint, /to use this agent/);
    assert.equal([...sessionControlsSource.matchAll(/let hintedAgentKey = \$state<string \| null>/g)].length, 1, "one key, so one hint");
    assert.equal([...body.matchAll(/aria-live="polite"/g)].length, 3, "an existing live region per row, so the hint is announced");
    assert.doesNotMatch(body, /class="agent-hint"/, "nothing is open before a press");
  });

  it("the hint clears when the tab changes or the drawer closes", () => {
    assert.match(sessionControlsSource, /class="agent-list"[\s\S]*?\{@attach clearHintOnLeave\}/);
    assert.match(functionSource(sessionControlsSource, "chooseAgent") ?? "", /hintedAgentKey = choice\.hint/);
    const app = readFileSync(fileURLToPath(new URL("../../App.svelte", import.meta.url)), "utf8");
    const drawer = app.slice(app.indexOf("{#if sessionManagerHeld}"));
    assert.ok(
      drawer.indexOf('display="pane"') < drawer.indexOf("{/if}"),
      "the manager must unmount with the drawer, which is what drops the hint's state"
    );
  });

  it("the refresh control re-checks workers when the agent picker is open", async () => {
    /* The popover header is shared by both views, so the button belongs to whichever list
       is on screen. Refreshing sessions while looking at the agent picker was the bug: it
       spun, and nothing the reader was looking at changed. */
    const source = sessionControlsSource;
    assert.match(source, /menuTab === "new" \? onRefreshAgents : onRefreshSessions/);
    assert.match(source, /menuTab === "new" \? refreshingAgents : refreshingSessions/);
    assert.match(source, /if \(menuTab === "new"\) await onRefreshAgents\?\.\(\);/);

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
    assert.doesNotMatch(
      sessionControlsSource,
      /<StatusChip\s+label="Ready"/,
      "the hardcoded Ready chip is the bug this feature removes"
    );
    /* no_worker names the queue, because the reader's next move is to start that worker and
       they need to know which one. */
    assert.match(functionSource(sessionControlsSource, "agentWorkerTip") ?? "", /`No worker polling \$\{agent\.task_queue\}`/);
  });
});
