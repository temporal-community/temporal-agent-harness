<!-- One run of the flow, drawn as the DAG it turned out to be. Each `run_agent` host call is a
     step; steps whose runs overlapped sit in one column (they were gathered), and each column
     feeds the next. A step's live detail comes from its own subagent view; its spec and reply
     come from the host call on the builder's stream, so the graph survives a reload. -->
<script module lang="ts">
  import type { AgentSession, AgentSseFrame, AgentView, HarnessMessage, ToolPart } from "@temporalio/agent-harness-svelte";
  import type { DagBuilderAgent } from "../client_sdk/DagBuilderAgent";
  import type { DagStepAgent, StepSpec } from "../client_sdk/DagStepAgent";

  type Message = HarnessMessage<DagBuilderAgent>;
  type StepMessage = HarnessMessage<DagStepAgent>;

  export interface Step {
    id: string;
    spec: Required<StepSpec> & { prompt: string };
    status: "running" | "done" | "failed";
    output: string | null;
    error: string | null;
    start: number;
    end: number | null;
    view: AgentView<DagStepAgent> | undefined;
    message: StepMessage | undefined;
  }

  /** The `run_agent` calls of one run, each joined to the subagent it started. */
  export function buildSteps(run: Message, frames: readonly AgentSseFrame[], rootId: string | null, agentOf: (id: string) => AgentView<DagStepAgent> | undefined): Step[] {
    const times = new Map<string, { start: number; end: number | null }>();
    for (const { event, data } of frames) {
      if (event === "stream_error" || data.agent_id !== rootId) continue;
      if (event === "tool_start") times.set(data.tool_id, { start: data.timestamp, end: null });
      else if (event === "tool_end" || event === "tool_error") {
        const t = times.get(data.tool_id);
        if (t) t.end = data.timestamp;
      }
    }

    const children = run.parts.flatMap((p) => {
      if (p.type !== "subagent") return [];
      const view = agentOf(p.subagentId);
      const message = view?.messages.find((m) => m.id === p.messageId) ?? view?.messages[0];
      return [{ view, message, used: false }];
    });

    const calls = run.parts.filter((p): p is ToolPart => p.type === "tool" && p.toolName === "run_agent");
    return calls.map((call, index) => {
      const spec = (call.input as { step: StepSpec & { prompt: string } }).step;
      const child =
        children.find((c) => !c.used && c.view?.states.profile?.name === spec.name && c.message?.input.text === spec.prompt) ??
        children.find((c) => !c.used && c.view?.states.profile?.name === spec.name) ??
        children.filter((c) => !c.used)[0];
      if (child) child.used = true;
      let output: string | null = null;
      try {
        output = call.output ? (JSON.parse(call.output) as { output: string }).output : null;
      } catch {
        output = call.output;
      }
      const t = times.get(call.toolId);
      return {
        id: call.toolId,
        spec: { ...spec, tools: spec.tools ?? [], model: spec.model ?? "gpt-6-luna" },
        status: call.state === "done" ? "done" : call.state === "failed" || call.state === "denied" ? "failed" : "running",
        output,
        error: call.error,
        start: t?.start ?? index,
        end: t?.end ?? (call.state === "done" || call.state === "failed" ? (t?.start ?? index) : null),
        view: child?.view,
        message: child?.message
      } satisfies Step;
    });
  }

  /** Steps grouped into columns: a step joins the current column if it started before any
   *  step in it had finished, i.e. they ran concurrently. */
  export function columns(steps: readonly Step[]): Step[][] {
    const cols: Step[][] = [];
    for (const step of [...steps].sort((a, b) => a.start - b.start)) {
      const current = cols.at(-1);
      const firstEnd = current ? Math.min(...current.map((s) => s.end ?? Infinity)) : -Infinity;
      if (current && step.start < firstEnd) current.push(step);
      else cols.push([step]);
    }
    return cols;
  }

  /** What Code Mode printed and returned, with a returned string unwrapped from its repr. */
  export function readOutput(text: string): { printed: string; result: string | null; error: string | null } {
    const error = /^Script error \(([\s\S]*)\)$/.exec(text.trim());
    if (error) return { printed: "", result: null, error: error[1]! };
    const at = text.lastIndexOf("result: ");
    if (at < 0) return { printed: text, result: null, error: null };
    let result = text.slice(at + 8).trim();
    const quoted = /^(['"])([\s\S]*)\1$/.exec(result);
    if (quoted) {
      result = quoted[2]!.replace(/\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|n|t|r|\\|'|")/g, (_, e: string) =>
        e === "n" ? "\n" : e === "t" ? "\t" : e === "r" ? "" : e.length > 1 ? String.fromCharCode(parseInt(e.slice(1), 16)) : e
      );
    }
    return { printed: text.slice(0, at).trim(), result, error: null };
  }

  export const TOOL_LABELS: Record<string, string> = {
    search_flights: "✈ flights",
    search_hotels: "⌂ hotels",
    book_flight: "✓ book flight",
    book_hotel: "✓ book hotel",
    get_trip_summary: "≡ itinerary",
    get_weather: "☀ weather"
  };
</script>

<script lang="ts">
  import { fly } from "svelte/transition";
  import { markdown } from "./ChatPanel.svelte";

  interface Props {
    agent: AgentSession<DagBuilderAgent>;
    runs: readonly Message[];
    run: Message | null;
    now: number;
    onselectrun: (id: string) => void;
  }
  let { agent, runs, run, now, onselectrun }: Props = $props();

  let tab = $state<"graph" | "output">("graph");
  let selected = $state<string | null>(null);

  const steps = $derived(run ? buildSteps(run, agent.frames, agent.rootAgentId, (id) => agent.agent<DagStepAgent>(id)) : []);
  const cols = $derived(columns(steps));
  const chosen = $derived(steps.find((s) => s.id === selected) ?? null);
  const output = $derived(run?.output ? readOutput(run.output.text) : null);
  const done = $derived(steps.filter((s) => s.status === "done").length);
  const live = $derived(run !== null && run.status !== "done" && run.status !== "error");
  const runNumber = $derived(run ? runs.indexOf(run) + 1 : 0);

  // Layout: fixed-size cards, so every connector can be computed rather than measured.
  const W = 232, H = 108, GAP = 14, COL = 64, PILL = 58, PAD = 20;
  const maxRows = $derived(Math.max(1, ...cols.map((c) => c.length)));
  const height = $derived(maxRows * H + (maxRows - 1) * GAP + PAD * 2);
  const colX = (i: number) => PAD + PILL + COL + i * (W + COL);
  const width = $derived(colX(cols.length) + PILL + PAD);
  const rowY = (col: number, row: number) => {
    const n = cols[col]?.length ?? 1;
    return (height - (n * H + (n - 1) * GAP)) / 2 + row * (H + GAP);
  };
  const mid = $derived(height / 2);

  // Shrink the whole graph to fit the panel, down to a readable minimum; past that it scrolls.
  let canvasWidth = $state(0);
  let canvasHeight = $state(0);
  const scale = $derived(
    canvasWidth && canvasHeight ? Math.max(0.6, Math.min(1, (canvasWidth - 8) / width, (canvasHeight - 8) / height)) : 1
  );

  interface Edge { d: string; live: boolean; done: boolean }
  const edges = $derived.by(() => {
    const out: Edge[] = [];
    const curve = (x1: number, y1: number, x2: number, y2: number) => {
      const dx = (x2 - x1) / 2;
      return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    };
    cols.forEach((col, c) => {
      col.forEach((step, r) => {
        const y = rowY(c, r) + H / 2;
        const sources = c === 0 ? [{ x: PAD + PILL, y: mid, done: true }] : cols[c - 1]!.map((s, pr) => ({ x: colX(c - 1) + W, y: rowY(c - 1, pr) + H / 2, done: s.status === "done" }));
        for (const src of sources) out.push({ d: curve(src.x, src.y, colX(c), y), live: step.status === "running", done: step.status !== "running" && src.done });
      });
    });
    const last = cols.at(-1);
    if (last) {
      last.forEach((s, r) => out.push({ d: curve(colX(cols.length - 1) + W, rowY(cols.length - 1, r) + H / 2, colX(cols.length), mid), live: false, done: run?.status === "done" }));
    }
    return out;
  });

  const liveText = (s: Step) => s.message?.parts.flatMap((p) => (p.type === "reply_delta" ? [p.text] : [])).join("") ?? "";
  const toolCalls = (s: Step) => (s.message?.parts.filter((p): p is ToolPart => p.type === "tool") ?? []);
  const secs = (s: Step) => ((s.end ?? now) - s.start).toFixed(1);

  function activity(s: Step): string {
    if (s.status === "done") return s.output ?? "";
    if (s.status === "failed") return s.error ?? "failed";
    const text = liveText(s);
    if (text) return text;
    const running = toolCalls(s).findLast((p) => p.state !== "done" && p.state !== "failed");
    if (running) return `calling ${running.toolName}…`;
    if (s.message?.status === "running") return "thinking…";
    return "starting…";
  }

  $effect(() => {
    void run?.id;
    selected = null;
  });
</script>

<section class="runs">
  <header>
    <div class="tabs">
      <button class:on={tab === "graph"} onclick={() => (tab = "graph")}>Graph</button>
      <button class:on={tab === "output"} onclick={() => (tab = "output")} disabled={!run}>Output</button>
    </div>
    {#if run}
      <div class="summary" data-status={run.status}>
        {#if live}<i class="spinner"></i>{:else if run.status === "error" || output?.error}<b class="bad">●</b>{:else}<b class="ok">●</b>{/if}
        Run #{runNumber} · {done}/{steps.length} steps · {run.startedAt ? ((run.endedAt ?? now) - run.startedAt).toFixed(1) : "0.0"}s
      </div>
      {#if runs.length > 1}
        <select value={run.id} onchange={(e) => onselectrun(e.currentTarget.value)} aria-label="Choose a run">
          {#each runs as r, i (r.id)}<option value={r.id}>Run #{i + 1}</option>{/each}
        </select>
      {/if}
    {/if}
  </header>

  <div class="body">
    {#if !run}
      <div class="empty">
        <div class="ghost">
          {#each [1, 3, 1] as n, c (c)}
            <div class="gcol">{#each Array(n) as _, r (r)}<span></span>{/each}</div>
          {/each}
        </div>
        <p>Press <b>Run</b> to execute the flow. Each <code>run_agent</code> call starts a fresh agent as a durable subagent, and you'll watch the DAG light up here.</p>
      </div>
    {:else if tab === "graph"}
      <div class="canvas" bind:clientWidth={canvasWidth} bind:clientHeight={canvasHeight}>
        <div class="fit" style:width="{width * scale}px" style:height="{height * scale}px">
        <div class="stage" style:width="{width}px" style:height="{height}px" style:transform="scale({scale})">
          <svg width={width} height={height} aria-hidden="true">
            {#each edges as e, i (i)}
              <path d={e.d} class:live={e.live} class:done={e.done} />
            {/each}
          </svg>
          <div class="pill start" style:left="{PAD}px" style:top="{mid - 15}px">start</div>
          <div class="pill end" class:ok={run.status === "done" && !output?.error} class:bad={run.status === "error" || !!output?.error} style:left="{colX(cols.length)}px" style:top="{mid - 15}px">
            {run.status === "done" ? (output?.error ? "error" : "result") : live ? "…" : "failed"}
          </div>
          {#if steps.length === 0 && live}
            <div class="booting" style:left="{colX(0)}px" style:top="{mid - 20}px"><i class="spinner"></i> type-checking and starting the script…</div>
          {/if}
          {#each cols as col, c (c)}
            {#each col as step, r (step.id)}
              <button
                class="node"
                data-status={step.status}
                class:selected={selected === step.id}
                style:left="{colX(c)}px"
                style:top="{rowY(c, r)}px"
                style:width="{W}px"
                style:height="{H}px"
                onclick={() => (selected = selected === step.id ? null : step.id)}
                in:fly={{ y: 8, duration: 250 }}
              >
                <div class="top">
                  <span class="status">{#if step.status === "running"}<i class="spinner"></i>{:else if step.status === "done"}✓{:else}✕{/if}</span>
                  <b>{step.spec.name}</b>
                  <span class="model">{step.spec.model}</span>
                </div>
                <div class="tools">
                  {#each step.spec.tools as t (t)}<span>{TOOL_LABELS[t] ?? t}</span>{:else}<span class="none">no tools · reasons only</span>{/each}
                </div>
                <p class="activity">{activity(step)}</p>
                <div class="foot">
                  <span>{toolCalls(step).length} tool call{toolCalls(step).length === 1 ? "" : "s"}</span>
                  <span>{secs(step)}s</span>
                </div>
                {#if step.status === "running"}<div class="bar"></div>{/if}
              </button>
            {/each}
          {/each}
        </div>
        </div>
      </div>
    {:else}
      <div class="output">
        {#if !output}
          <p class="muted">{live ? "The flow is still running…" : run.error ?? "No output."}</p>
        {:else}
          {#if output.error}
            <h4 class="bad">Script error</h4>
            <pre class="err">{output.error}</pre>
            <p class="muted">Ask the assistant to fix it — it sees this output on your next message.</p>
          {/if}
          {#if output.printed}
            <h4>Printed</h4>
            <pre>{output.printed}</pre>
          {/if}
          {#if output.result !== null}
            <h4>Result</h4>
            <div class="md result">{@html markdown(output.result)}</div>
          {/if}
        {/if}
      </div>
    {/if}

    {#if chosen && tab === "graph"}
      {@const s = chosen}
      <aside class="detail" transition:fly={{ x: 24, duration: 200 }}>
        <header>
          <div>
            <b>{s.spec.name}</b>
            <small>{s.spec.model} · {s.view?.workflowId ?? "subagent"}</small>
          </div>
          <button onclick={() => (selected = null)} aria-label="Close">✕</button>
        </header>
        <div class="scroll">
          <h5>Instructions</h5>
          <p class="pre">{s.spec.instructions}</p>
          <h5>Prompt</h5>
          <p class="pre">{s.spec.prompt}</p>
          <h5>Tool calls</h5>
          {#each toolCalls(s) as p (p.toolId)}
            <details class="call">
              <summary><span class="dotc" data-state={p.state}></span>{p.toolName}</summary>
              <pre>{JSON.stringify(p.input, null, 2)}</pre>
              {#if p.output}<pre class="out">{p.output}</pre>{/if}
              {#if p.error}<pre class="err">{p.error}</pre>{/if}
            </details>
          {:else}
            <p class="muted">{s.view?.unavailable ? "This step's own events are no longer readable (its subagent has stopped)." : s.spec.tools.length ? "None yet." : "This step has no tools."}</p>
          {/each}
          <h5>Reply</h5>
          {#if s.output ?? liveText(s)}
            <div class="md">{@html markdown(s.output ?? liveText(s))}</div>
          {:else}
            <p class="muted">Waiting…</p>
          {/if}
        </div>
      </aside>
    {/if}
  </div>
</section>

<style>
  .runs { display: flex; flex-direction: column; min-height: 0; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
  .runs > header { height: 38px; flex: none; display: flex; align-items: center; gap: 12px; padding: 0 10px 0 6px; background: var(--panel-2); border-bottom: 1px solid var(--line); }
  .tabs { display: flex; gap: 2px; }
  .tabs button { border: 0; background: transparent; padding: 5px 10px; border-radius: 6px; color: var(--muted); font-weight: 500; font-size: 12.5px; }
  .tabs button.on { background: var(--panel-3); color: var(--ink); }
  .tabs button:disabled { opacity: 0.4; }
  .summary { margin-left: auto; display: flex; align-items: center; gap: 7px; font: 11.5px var(--mono); color: var(--muted); }
  .ok { color: var(--good); }
  .bad { color: var(--bad); }
  select { background: var(--panel-3); border: 1px solid var(--line); border-radius: 6px; font-size: 12px; padding: 2px 6px; }

  .body { position: relative; flex: 1; min-height: 0; display: flex; }
  .canvas { flex: 1; overflow: auto; display: flex; background-image: radial-gradient(var(--line) 1px, transparent 1px); background-size: 18px 18px; }
  .fit { margin: auto; flex: none; }
  .stage { position: relative; transform-origin: 0 0; }
  svg { position: absolute; inset: 0; overflow: visible; }
  path { fill: none; stroke: var(--line-2); stroke-width: 1.6; }
  path.done { stroke: color-mix(in srgb, var(--good) 55%, var(--line-2)); }
  path.live { stroke: var(--cyan); stroke-dasharray: 5 5; animation: flow 0.8s linear infinite; }
  @keyframes flow { to { stroke-dashoffset: -20; } }

  .pill { position: absolute; width: 58px; height: 30px; border-radius: 999px; display: grid; place-items: center; font: 600 11px var(--mono); background: var(--panel-3); border: 1px solid var(--line-2); color: var(--muted); }
  .pill.start { color: var(--cyan); border-color: color-mix(in srgb, var(--cyan) 45%, transparent); }
  .pill.end.ok { color: var(--good); border-color: color-mix(in srgb, var(--good) 45%, transparent); }
  .pill.end.bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, transparent); }
  .booting { position: absolute; display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }

  .node {
    position: absolute;
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 12px 9px;
    text-align: left;
    background: var(--panel-2);
    border: 1px solid var(--line-2);
    border-radius: 11px;
    box-shadow: var(--shadow);
    overflow: hidden;
    transition: border-color 0.2s, transform 0.15s;
  }
  .node:hover { transform: translateY(-1px); }
  .node[data-status="running"] { border-color: color-mix(in srgb, var(--cyan) 60%, transparent); }
  .node[data-status="done"] { border-color: color-mix(in srgb, var(--good) 35%, var(--line-2)); }
  .node[data-status="failed"] { border-color: color-mix(in srgb, var(--bad) 60%, transparent); }
  .node.selected { outline: 2px solid var(--accent); outline-offset: 2px; }
  .top { display: flex; align-items: center; gap: 7px; min-width: 0; }
  .top b { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
  .status { width: 18px; height: 18px; flex: none; border-radius: 5px; display: grid; place-items: center; font-size: 10px; font-weight: 700; }
  [data-status="running"] .status { color: var(--cyan); background: color-mix(in srgb, var(--cyan) 14%, transparent); }
  [data-status="done"] .status { color: var(--good); background: color-mix(in srgb, var(--good) 14%, transparent); }
  [data-status="failed"] .status { color: var(--bad); background: color-mix(in srgb, var(--bad) 14%, transparent); }
  .model { font: 10px var(--mono); color: var(--faint); padding: 1px 5px; border: 1px solid var(--line); border-radius: 4px; }
  .tools { display: flex; gap: 4px; flex-wrap: nowrap; overflow: hidden; }
  .tools span { font-size: 10.5px; padding: 1px 6px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); white-space: nowrap; }
  .tools span.none { background: transparent; color: var(--faint); padding: 1px 0; }
  .activity { margin: 0; flex: 1; font-size: 11.5px; line-height: 1.4; color: var(--muted); overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; }
  .node > * { width: 100%; }
  .foot { display: flex; justify-content: space-between; font: 10.5px var(--mono); color: var(--faint); }
  .bar { position: absolute; left: 0; right: 0; bottom: 0; height: 2px; background: linear-gradient(90deg, transparent, var(--cyan), transparent); background-size: 50% 100%; background-repeat: no-repeat; animation: slide 1.2s linear infinite; }
  @keyframes slide { from { background-position: -50% 0; } to { background-position: 150% 0; } }

  .empty { flex: 1; display: grid; place-content: center; justify-items: center; gap: 12px; padding: 20px; text-align: center; color: var(--muted); }
  .empty p { margin: 0; max-width: 420px; }
  .ghost { display: flex; gap: 26px; align-items: center; opacity: 0.6; }
  .gcol { display: flex; flex-direction: column; gap: 6px; }
  .gcol span { width: 54px; height: 20px; border-radius: 6px; border: 1px dashed var(--line-2); }

  .output { flex: 1; overflow: auto; padding: 14px 18px; }
  .output h4 { margin: 4px 0 6px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--faint); }
  .output pre { margin: 0 0 14px; padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--line); border-radius: 7px; white-space: pre-wrap; font-size: 12px; }
  .result { font-size: 13.5px; }
  .md :global(p) { margin: 0 0 8px; }
  .md :global(ul), .md :global(ol) { margin: 0 0 8px; padding-left: 18px; }
  .md :global(code) { font-size: 12px; padding: 1px 5px; border-radius: 4px; background: var(--panel-3); }
  .err { color: var(--bad); }
  .muted { color: var(--faint); }

  .detail { position: absolute; top: 0; right: 0; bottom: 0; width: min(360px, 90%); display: flex; flex-direction: column; background: var(--panel); border-left: 1px solid var(--line); box-shadow: var(--shadow); }
  .detail header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; padding: 12px 14px 10px; border-bottom: 1px solid var(--line); }
  .detail header b { display: block; font-size: 14px; }
  .detail header small { font: 10.5px var(--mono); color: var(--faint); overflow-wrap: anywhere; }
  .detail header button { border: 0; background: transparent; color: var(--muted); }
  .scroll { flex: 1; overflow: auto; padding: 4px 14px 16px; }
  h5 { margin: 12px 0 5px; font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); }
  .pre { margin: 0; white-space: pre-wrap; font-size: 12.5px; color: var(--muted); }
  .call { border: 1px solid var(--line); border-radius: 7px; margin-bottom: 6px; background: var(--panel-2); }
  .call summary { padding: 6px 9px; font: 12px var(--mono); cursor: pointer; display: flex; align-items: center; gap: 7px; }
  .call pre { margin: 0; padding: 8px 10px; border-top: 1px solid var(--line); font-size: 11px; white-space: pre-wrap; max-height: 180px; overflow: auto; }
  .call pre.out { color: var(--muted); }
  .dotc { width: 7px; height: 7px; border-radius: 50%; background: var(--cyan); }
  .dotc[data-state="done"] { background: var(--good); }
  .dotc[data-state="failed"] { background: var(--bad); }

  .spinner { display: inline-block; width: 10px; height: 10px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.7s linear infinite; }
</style>
