<!-- One flow: its script, its runs and its conversation. The script lives on this page, never in
     the workflow: the builder agent reads and edits it through callback tools this page fulfils
     (`callbackTools` below), and the script shown is those edits replayed from the session's
     stream, so a reload, or another browser, rebuilds the same script. `execute` sends it back
     to run. Mounted once per session (keyed by its id); a draft with no session yet has no agent
     and creates the session on its first prompt. -->
<script lang="ts">
  import { AgentSession, type HttpTransport, type ToolCall, type ToolPart } from "@temporalio/agent-harness-svelte";
  import { onMount } from "svelte";
  import { SvelteSet } from "svelte/reactivity";

  import type { DagBuilderAgent } from "../client_sdk/DagBuilderAgent";
  import ChatPanel from "./ChatPanel.svelte";
  import CodeEditor, { changedRanges, type Flash } from "./CodeEditor.svelte";
  import RunPanel from "./RunPanel.svelte";

  interface Props {
    sessionId: string | null;
    transport: HttpTransport;
    title: string;
    temporalUi: string;
    /** A prompt to send as soon as the session opens: the draft's first message. */
    initialPrompt: string | null;
    /** Create the session for a draft's first prompt. */
    oncreate: (prompt: string) => void;
    /** The flow's first prompt, to title it by. */
    ontitle: (text: string) => void;
  }
  let { sessionId, transport, title, temporalUi, initialPrompt, oncreate, ontitle }: Props = $props();

  // -- the script -------------------------------------------------------------------------------

  /** Edit calls this page has applied, before the stream reports them done. */
  const applied = new SvelteSet<string>();
  /** A whole-script write being typed out: the text so far. */
  let typing = $state<string | null>(null);
  let flashes = $state<Flash[]>([]);
  let cursor = $state<number | null>(null);
  let flashIds = 0;

  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
  const numbered = (code: string) =>
    code ? code.split("\n").map((line, i) => `${String(i + 1).padStart(4)} | ${line}`).join("\n") : "(empty)";
  const occurrences = (haystack: string, needle: string) => (needle ? haystack.split(needle).length - 1 : 0);

  /** The script: every write and edit the agent made in this session, in order. */
  function replay(messages: readonly { handler: string; parts: readonly unknown[] }[]): string {
    let code = "";
    for (const m of messages) {
      if (m.handler !== "generate") continue;
      for (const p of m.parts as ToolPart[]) {
        if (p.type !== "tool" || (p.toolName !== "write_code" && p.toolName !== "edit_code")) continue;
        if (p.state !== "done" && !applied.has(p.toolId)) continue;
        const input = p.input as Record<string, string>;
        if (p.toolName === "write_code") code = input.code ?? "";
        else if (occurrences(code, input.old_text ?? "") === 1) code = code.replace(input.old_text!, () => input.new_text ?? "");
      }
    }
    return code;
  }

  function flash(from: number, to: number): void {
    const id = ++flashIds;
    flashes = [...flashes.slice(-24), { id, from, to }];
    setTimeout(() => (flashes = flashes.filter((f) => f.id !== id)), 2600);
  }

  const editorTools: Record<string, (call: ToolCall) => Promise<string>> = {
    async read_code() {
      return numbered(code);
    },
    // The agent's `check_code` reads the script through this to type-check it.
    async export_code() {
      return code;
    },
    async write_code(call) {
      const previous = code;
      const next = String(call.input.code ?? "");
      const lines = next.split("\n");
      const stride = Math.max(1, Math.ceil(lines.length / 60));
      for (let n = stride; n < lines.length; n += stride) {
        typing = lines.slice(0, n).join("\n");
        cursor = n;
        await sleep(16);
      }
      applied.add(call.toolId);
      typing = null;
      cursor = null;
      for (const [from, to] of changedRanges(previous, next)) flash(from, to);
      return `Wrote ${lines.length} lines.`;
    },
    async edit_code(call) {
      const oldText = String(call.input.old_text ?? "");
      const newText = String(call.input.new_text ?? "");
      const count = occurrences(code, oldText);
      if (count === 0) throw new Error("old_text is not in the script. Call read_code and copy the text exactly, without the line-number prefixes.");
      if (count > 1) throw new Error(`old_text appears ${count} times. Include more surrounding lines so it matches exactly once.`);
      const from = code.slice(0, code.indexOf(oldText)).split("\n").length;
      const to = from + Math.max(0, newText.split("\n").length - 1);
      cursor = from;
      await sleep(260);
      applied.add(call.toolId);
      flash(from, to);
      await sleep(420);
      cursor = null;
      return newText ? `Replaced it; the new text is on lines ${from}-${to}.` : `Deleted it at line ${from}.`;
    }
  };

  // The builder may make several calls in one response, and the session starts each handler as
  // soon as it sees the request, in stream order. Chain them so each runs on the script the one
  // before it left: the same order `replay` rebuilds the script in.
  let queue: Promise<unknown> = Promise.resolve();
  const inOrder =
    (tool: (call: ToolCall) => Promise<string>) =>
    (call: ToolCall): Promise<string> => {
      const run = queue.then(() => tool(call));
      queue = run.catch(() => undefined);
      return run;
    };
  const callbackTools = Object.fromEntries(Object.entries(editorTools).map(([name, tool]) => [name, inOrder(tool)]));

  // The parent re-mounts this component for each session, so these props never change here.
  // svelte-ignore state_referenced_locally
  const agent = sessionId
    ? new AgentSession<DagBuilderAgent>({
        sessionId,
        transport,
        callbackTools,
        // A send made while an attach is still opening can miss its wake-up; poll often so the
        // turn it starts shows up promptly anyway.
        idlePollMs: 1000
      })
    : null;

  const code = $derived(typing ?? (agent ? replay(agent.messages) : ""));

  // -- actions ----------------------------------------------------------------------------------

  function prompt(text: string): void {
    if (agent) void agent.sendMessage("generate", { text });
    else oncreate(text);
  }

  function run(): void {
    if (!agent || !code || busy) return;
    selectedRun = null;
    void agent.sendMessage("execute", { script: code });
  }

  onMount(() => {
    if (initialPrompt) prompt(initialPrompt);
  });

  $effect(() => {
    const first = messages.find((m) => m.handler === "generate");
    if (first?.handler === "generate") ontitle(first.input.text);
  });

  // -- derived view state -----------------------------------------------------------------------

  const inFlight = (m: { status: string }) => m.status === "sending" || m.status === "accepted" || m.status === "running";
  const messages = $derived(agent?.messages ?? []);
  // A send the server has admitted can sit in `pending` under its real id until the stream's
  // own copy lands; show it once.
  const pending = $derived((agent?.pending ?? []).filter((p) => !messages.some((m) => m.id === p.id)));
  const busy = $derived(!!agent && (agent.agentStatus === "busy" || pending.some(inFlight) || messages.some(inFlight)));
  const editing = $derived(messages.some((m) => m.handler === "generate" && m.status === "running"));
  const runs = $derived(messages.filter((m) => m.handler === "execute"));
  let selectedRun = $state<string | null>(null);
  const shownRun = $derived(runs.find((r) => r.id === selectedRun) ?? runs.at(-1) ?? null);
  const running = $derived(runs.some(inFlight) || pending.some((m) => m.handler === "execute"));

  let now = $state(Date.now() / 1000);
  $effect(() => {
    if (!busy) return;
    const timer = setInterval(() => (now = Date.now() / 1000), 100);
    return () => clearInterval(timer);
  });

  function keydown(e: KeyboardEvent): void {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.shiftKey) {
      e.preventDefault();
      run();
    }
  }
</script>

<svelte:window onkeydown={keydown} />

<main>
  <header class="bar">
    <div class="title">
      <h1>{title}</h1>
      {#if agent && sessionId}
        <span class="conn" data-state={agent.connection}>{agent.connection}</span>
        <a href="{temporalUi}/{encodeURIComponent(sessionId)}" target="_blank" rel="noreferrer">open in Temporal ↗</a>
      {/if}
    </div>
    <button class="run" onclick={run} disabled={!code || busy} title="Run the flow (⌘/Ctrl + Shift + Enter)">
      {#if running}<i class="spinner"></i> Running…{:else}<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2.8v10.4L13 8z" /></svg> Run flow{/if}
    </button>
  </header>

  {#if agent?.error}
    <div class="banner">{agent.error.message} <button onclick={() => agent.clearError()}>Dismiss</button></div>
  {/if}

  <div class="work">
    <CodeEditor {code} {flashes} {cursor} typing={typing !== null} {editing} />
    {#if agent}
      <RunPanel {agent} {runs} run={shownRun} {now} onselectrun={(id) => (selectedRun = id)} />
    {:else}
      <section class="placeholder">Runs of this flow will appear here.</section>
    {/if}
  </div>
</main>

<ChatPanel
  {messages}
  {pending}
  {busy}
  hasCode={!!code}
  selectedRun={shownRun?.id ?? null}
  {now}
  onsend={prompt}
  onopenrun={(id) => (selectedRun = id)}
  ondismiss={(id) => agent?.dismiss(id)}
/>

<style>
  main { display: flex; flex-direction: column; min-width: 0; min-height: 0; padding: 0 12px 12px; gap: 10px; }
  .bar { height: 50px; flex: none; display: flex; align-items: center; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--line); margin: 0 -12px; padding: 0 14px 0 18px; }
  .title { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
  h1 { margin: 0; font-size: 14.5px; font-weight: 600; letter-spacing: -0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .title a { font-size: 11.5px; color: var(--faint); text-decoration: none; white-space: nowrap; }
  .title a:hover { color: var(--accent); }
  .conn { font: 11px var(--mono); color: var(--faint); display: inline-flex; align-items: center; gap: 5px; }
  .conn::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--faint); }
  .conn[data-state="live"]::before, .conn[data-state="idle"]::before { background: var(--good); }
  .conn[data-state="connecting"]::before, .conn[data-state="reconnecting"]::before { background: var(--warn); animation: pulse 1s infinite; }
  .conn[data-state="error"]::before { background: var(--bad); }

  .run {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    height: 32px;
    padding: 0 14px;
    border-radius: 8px;
    border: 0;
    font-weight: 600;
    background: linear-gradient(180deg, color-mix(in srgb, var(--good) 92%, white), var(--good));
    color: #06150e;
    box-shadow: 0 4px 14px color-mix(in srgb, var(--good) 30%, transparent);
    transition: transform 0.1s, opacity 0.15s;
  }
  .run:hover:not(:disabled) { transform: translateY(-1px); }
  .run:disabled { opacity: 0.4; box-shadow: none; }
  .run svg { width: 13px; height: 13px; fill: currentColor; }

  .banner { padding: 8px 12px; border-radius: 8px; border: 1px solid color-mix(in srgb, var(--bad) 50%, transparent); background: color-mix(in srgb, var(--bad) 10%, transparent); color: var(--bad); font-size: 12.5px; }
  .banner button { margin-left: 8px; border: 1px solid var(--line); background: var(--panel); border-radius: 5px; font-size: 11.5px; }

  .work { flex: 1; min-height: 0; display: grid; grid-template-rows: minmax(160px, 1.15fr) minmax(200px, 1fr); gap: 10px; }
  .placeholder { display: grid; place-items: center; border: 1px dashed var(--line); border-radius: var(--radius); color: var(--faint); }

  .spinner { display: inline-block; width: 11px; height: 11px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.7s linear infinite; }

  @media (max-width: 900px) {
    .work { grid-template-rows: 420px 460px; }
  }
</style>
