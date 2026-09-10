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
 *    assertion below, which is the only reachable statement of it — no check here compiles
 *    Svelte.
 *
 * Run: npx vitest run src/lib/state/agentWorkerReadiness.test.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";

const sessionControlsSource = readFileSync(
  fileURLToPath(new URL("../components/chat/SessionControls.svelte", import.meta.url)),
  "utf8"
);

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
    const open = sessionControlsSource.slice(
      sessionControlsSource.indexOf("function openNewSessionMenu"),
      sessionControlsSource.indexOf("$effect(() => {")
    );
    assert.match(open, /onEnsureAgents\?\.\(\)/);
  });

  it("refuses a no-worker agent at the press, not only in the markup", () => {
    /* `aria-disabled` styles a row inert but does not stop it activating — a keyboard
       Enter still fires onclick — so the refusal has to live in the handler too. */
    const start = sessionControlsSource.slice(
      sessionControlsSource.indexOf("async function startNewSession"),
      sessionControlsSource.indexOf("async function openSession")
    );
    assert.match(start, /if \(agentBlocked\(agent\)\) return;/);
    assert.match(start, /startNewSession\(agent: AgentDescriptor\)/);
    /* Only no_worker blocks. `unknown` means the server could not ask, and refusing on a
       failed health check would be a worse lie than the hardcoded "Ready" this replaced. */
    assert.match(
      sessionControlsSource,
      /function agentBlocked[\s\S]*?return agent\.worker\?\.status === "no_worker";/
    );
  });

  it("explains the refusal with a native title, which a scrolling list cannot clip", () => {
    /* app.css: the `data-tip` bubble is "painted, not portalled — inside an ancestor that
       clips, it will be clipped", and `.agent-list` is a scroll container inside a popover
       that hides overflow. A tip on the first row would vanish exactly when hovered. */
    assert.match(sessionControlsSource, /title=\{agentBlockedReason\(agent\)\}/);
    assert.match(sessionControlsSource, /Start a worker polling \$\{agent\.task_queue\}/);
    const row = sessionControlsSource.slice(
      sessionControlsSource.indexOf('class="agent-row"'),
      sessionControlsSource.indexOf("<AgentGlyph")
    );
    assert.doesNotMatch(row, /data-tip/, "a painted tip here would be clipped away");
    assert.doesNotMatch(
      row,
      /(?<!aria-)disabled=/,
      "native `disabled` drops the row from the tab order and kills its tooltip"
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
    /* no_worker names the queue, because the reader's next move is to start that worker
       and they need to know which one — and it is inline, not a `data-tip`, because the
       agent list scrolls inside a popover that would clip a tooltip. */
    assert.match(sessionControlsSource, /lead: "No worker polling"/);
    assert.match(sessionControlsSource, /\{note\.lead\} <code>\{agent\.task_queue\}<\/code>\{note\.tail\}/);
    /* The instruction rides on that same always-visible line rather than a hover surface:
       `data-tip` is clipped by this scroll box and a native `title` costs a second of still
       hover, which is too well hidden for the line that says why the row will not open. */
    assert.match(sessionControlsSource, /tail: " — start one to use this agent"/);
  });
});
