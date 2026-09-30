<!-- The conversation with the builder: each `generate` as a prompt and the agent's answer, with
     every edit it made along the way, and each `execute` as a one-line run summary. -->
<script module lang="ts">
  const escape = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s: string) =>
    s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");

  /** A small, escaping Markdown subset: paragraphs, lists, code spans and fences, bold. */
  export function markdown(text: string): string {
    const out: string[] = [];
    const blocks = escape(text.trim()).split(/\n{2,}/);
    for (const block of blocks) {
      if (block.startsWith("```")) {
        out.push(`<pre>${block.replace(/^```\w*\n?/, "").replace(/```$/, "")}</pre>`);
      } else if (/^\s*([-*]|\d+\.)\s/.test(block)) {
        const items = block.split(/\n(?=\s*(?:[-*]|\d+\.)\s)/).map((i) => `<li>${inline(i.replace(/^\s*([-*]|\d+\.)\s/, ""))}</li>`);
        out.push(/^\s*\d/.test(block) ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      } else if (/^#{1,4}\s/.test(block)) {
        out.push(`<h4>${inline(block.replace(/^#+\s/, ""))}</h4>`);
      } else {
        out.push(`<p>${inline(block).replace(/\n/g, "<br>")}</p>`);
      }
    }
    return out.join("");
  }
</script>

<script lang="ts">
  import type { HarnessMessage, ToolPart } from "@temporalio/agent-harness-svelte";
  import type { DagBuilderAgent } from "../client_sdk/DagBuilderAgent";
  import { readOutput } from "./RunPanel.svelte";

  type Message = HarnessMessage<DagBuilderAgent>;

  interface Props {
    messages: readonly Message[];
    pending: readonly Message[];
    busy: boolean;
    hasCode: boolean;
    selectedRun: string | null;
    now: number;
    onsend: (text: string) => void;
    onopenrun: (id: string) => void;
    ondismiss: (id: string) => void;
  }
  let { messages, pending, busy, hasCode, selectedRun, now, onsend, onopenrun, ondismiss }: Props = $props();

  let draft = $state("");
  let log: HTMLElement | undefined = $state();
  let input: HTMLTextAreaElement | undefined = $state();

  const STARTERS = [
    {
      title: "Weekend trip, fanned out",
      text: "Weekend in Seattle for Ada Lovelace, flying from SFO, 2026-10-16 to 2026-10-18. Scout flights, hotels and the weather in parallel, then a planner books the best combination and a judge double-checks the itinerary."
    },
    {
      title: "City showdown",
      text: "Compare Paris and Rome for a July 1–5 2026 trip. One agent per city checks the weather and hotels, then a judge picks the winner and explains why."
    },
    {
      title: "Just one agent",
      text: "A single travel agent that finds and books the cheapest SFO→JFK flight on 2026-07-01 for Grace Hopper."
    }
  ];
  const FOLLOW_UPS = [
    "Add a budget reviewer before the judge",
    "Run the judge on gpt-6.1-sol",
    "Have the judge return a pass/fail verdict"
  ];

  const all = $derived([...messages, ...pending]);

  /** Why a finished run failed: the handler's error, or the script's. `null` if it didn't. */
  function runFailure(m: Message): string | null {
    if (m.handler !== "execute") return null;
    if (m.status === "error" && m.turnNumber !== null) return m.error ?? "The run failed.";
    if (m.status === "done" && m.output) return readOutput(m.output.text).error;
    return null;
  }

  // When the latest message is a failed run, offer to send its failure to the assistant. It is
  // only offered: the user sends it, so a flow that keeps failing never loops on its own. Any
  // message or run after it withdraws the offer.
  let dismissed = $state<string | null>(null);
  const failedRun = $derived.by(() => {
    const last = all.at(-1);
    const error = last ? runFailure(last) : null;
    return last && error !== null && last.id !== dismissed ? { id: last.id, error } : null;
  });
  const fixRequest = (error: string) => `The flow failed when I ran it:\n\n${error}\n\nFix the script so it runs.`;

  function editFixRequest(run: { id: string; error: string }): void {
    draft = fixRequest(run.error);
    dismissed = run.id;
    input?.focus();
  }

  function send(text = draft): void {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    onsend(trimmed);
    draft = "";
  }

  function keydown(e: KeyboardEvent): void {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  }

  function replyText(m: Message): string {
    if (m.status === "done" && m.output) return m.output.text;
    return m.parts.flatMap((p) => (p.type === "reply_delta" ? [p.text] : [])).join("");
  }

  const editParts = (m: Message) =>
    m.parts.filter((p): p is ToolPart => p.type === "tool" && ["read_code", "write_code", "edit_code", "check_code"].includes(p.toolName));

  /** A finished `check_code` that found errors: its report, to show under the chip. */
  const checkErrors = (p: ToolPart) =>
    p.toolName === "check_code" && p.state === "done" && p.output && !p.output.startsWith("No errors") ? p.output : null;

  function editLabel(p: ToolPart): { icon: string; text: string } {
    const input = p.input as Record<string, string>;
    // Editor calls wait their turn in the page's queue, so a call is only past tense once the
    // page has finished it.
    const finished = p.state === "done" || p.state === "failed";
    if (p.toolName === "read_code") return { icon: "eye", text: finished ? "Read the script" : "Reading the script…" };
    if (p.toolName === "check_code") {
      if (p.state !== "done") return { icon: "check", text: "Type-checking the script…" };
      return checkErrors(p) ? { icon: "warn", text: "Type check found errors" } : { icon: "check", text: "Type-checks cleanly" };
    }
    if (p.toolName === "write_code") {
      const lines = String(input.code ?? "").split("\n").length;
      return { icon: "file", text: `${finished ? "Wrote" : "Writing"} ${lines} lines` };
    }
    const first = String(input.new_text ?? "").trim().split("\n")[0] ?? "";
    if (!input.new_text) return { icon: "pen", text: finished ? "Deleted a block" : "Deleting a block…" };
    return { icon: "pen", text: `${finished ? "Edited" : "Editing"}: ${first.slice(0, 56)}${first.length > 56 ? "…" : ""}` };
  }

  function seconds(m: Message): string {
    if (!m.startedAt) return "";
    return `${((m.endedAt ?? now) - m.startedAt).toFixed(1)}s`;
  }

  // Follow the conversation as it grows.
  $effect(() => {
    void all.length;
    void all.at(-1)?.parts.length;
    log?.scrollTo({ top: log.scrollHeight, behavior: "smooth" });
  });
</script>

<section class="chat">
  <header>
    <b>Assistant</b>
    <span>gpt-6-luna · edits your flow through callback tools</span>
  </header>

  <div class="log" bind:this={log}>
    {#if all.length === 0}
      <div class="welcome">
        <h2>What should your agents do?</h2>
        <p>Describe the job. The assistant designs a flow of agents — who runs in parallel, who waits for whom, which tools each one gets — and writes it into the editor.</p>
        {#each STARTERS as starter (starter.title)}
          <button class="starter" onclick={() => send(starter.text)} disabled={busy}>
            <b>{starter.title}</b>
            <span>{starter.text}</span>
          </button>
        {/each}
      </div>
    {/if}

    {#each all as m (m.id)}
      {#if m.handler === "generate"}
        <div class="user">
          <p>{m.input.text}</p>
          {#if m.status === "sending"}<small>sending…</small>{/if}
        </div>
        {#if m.status !== "sending"}
          {@const text = replyText(m)}
          <div class="agent">
            <div class="avatar" class:thinking={m.status === "running" || m.status === "accepted"}>
              <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 9.6 6.4 14.5 8l-4.9 1.6L8 14.5 6.4 9.6 1.5 8l4.9-1.6z" /></svg>
            </div>
            <div class="body">
              {#if m.status === "accepted"}
                <p class="muted">Queued behind the current turn…</p>
              {/if}
              {#each editParts(m) as p, i (p.toolId)}
                {@const label = editLabel(p)}
                {@const errors = checkErrors(p)}
                {@const fixed = errors !== null && editParts(m).slice(i + 1).some((q) => q.toolName === "check_code" && q.state === "done" && !checkErrors(q))}
                <div class="edit" data-state={p.state} class:warn={errors}>
                  <span class="icon" data-icon={label.icon}></span>
                  <span class="what">{label.text}</span>
                  <span class="state">
                    {#if fixed}fixed{:else if errors}fixing{:else if p.state === "done"}✓{:else if p.state === "failed"}retrying{:else}<i class="spinner"></i>{/if}
                  </span>
                </div>
                {#if errors}
                  <details class="report">
                    <summary>Show what the checker reported</summary>
                    <pre>{errors.replace(/^The script has errors\.[^\n]*\n+/, "")}</pre>
                  </details>
                {/if}
              {/each}
              {#if text}
                <div class="md">{@html markdown(text)}</div>
              {:else if m.status === "running"}
                <p class="muted typing"><i></i><i></i><i></i></p>
              {/if}
              {#if m.status === "error"}
                <p class="error">{m.error}</p>
              {/if}
            </div>
          </div>
        {/if}
      {:else}
        {@const steps = m.parts.filter((p) => p.type === "tool" && p.toolName === "run_agent").length}
        {@const failed = m.status === "error" || runFailure(m) !== null}
        <button class="run" class:selected={selectedRun === m.id} data-status={failed ? "error" : m.status} onclick={() => onopenrun(m.id)}>
          <span class="play">
            {#if m.status === "running" || m.status === "accepted" || m.status === "sending"}<i class="spinner"></i>{:else if failed}!{:else}▶{/if}
          </span>
          <span>
            {failed ? "Run failed" : m.status === "done" ? "Ran the flow" : "Running the flow"}
            · {steps} step{steps === 1 ? "" : "s"}
          </span>
          <span class="time">{seconds(m)}</span>
        </button>
      {/if}
      {#if m.status === "error" && m.turnNumber === null}
        <div class="failed">
          Couldn't send: {m.error}
          <button onclick={() => ondismiss(m.id)}>Dismiss</button>
        </div>
      {/if}
    {/each}
  </div>

  <div class="composer">
    {#if failedRun && !busy}
      <div class="suggest">
        <div class="head"><span class="icon" data-icon="warn"></span><b>The run failed.</b> Ask the assistant to fix it?</div>
        <pre>{failedRun.error}</pre>
        <div class="actions">
          <button class="primary" onclick={() => send(fixRequest(failedRun.error))}>Send to assistant</button>
          <button onclick={() => editFixRequest(failedRun)}>Edit first</button>
          <button onclick={() => (dismissed = failedRun.id)}>Dismiss</button>
        </div>
      </div>
    {:else if hasCode && !busy && draft === ""}
      <div class="chips">
        {#each FOLLOW_UPS as f (f)}
          <button onclick={() => send(f)}>{f}</button>
        {/each}
      </div>
    {/if}
    <div class="box" class:busy>
      <textarea
        bind:this={input}
        bind:value={draft}
        onkeydown={keydown}
        rows="3"
        placeholder={hasCode ? "Describe a change to the flow…" : "Describe the job for your agents…"}
      ></textarea>
      <div class="row">
        <span class="hint"><kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line</span>
        <button class="send" onclick={() => send()} disabled={busy || !draft.trim()} aria-label="Send">
          {#if busy}<i class="spinner"></i>{:else}<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 8h10M8.5 3.5 13 8l-4.5 4.5" /></svg>{/if}
        </button>
      </div>
    </div>
  </div>
</section>

<style>
  .chat { display: flex; flex-direction: column; min-height: 0; background: var(--panel); border-left: 1px solid var(--line); }
  header { height: 50px; flex: none; display: flex; flex-direction: column; justify-content: center; padding: 0 16px; border-bottom: 1px solid var(--line); }
  header b { font-size: 13.5px; }
  header span { font-size: 11.5px; color: var(--faint); }

  .log { flex: 1; overflow: auto; padding: 16px 14px 8px; display: flex; flex-direction: column; gap: 12px; min-height: 0; }

  .welcome h2 { margin: 6px 0 4px; font-size: 17px; letter-spacing: -0.01em; }
  .welcome p { margin: 0 0 14px; color: var(--muted); }
  .starter {
    display: block;
    width: 100%;
    margin-bottom: 8px;
    padding: 11px 12px;
    text-align: left;
    background: var(--panel-2);
    border: 1px solid var(--line);
    border-radius: 9px;
    transition: border-color 0.15s, transform 0.15s;
  }
  .starter:hover:not(:disabled) { border-color: color-mix(in srgb, var(--accent) 55%, transparent); transform: translateY(-1px); }
  .starter b { display: block; font-size: 12.5px; margin-bottom: 3px; color: var(--accent); }
  .starter span { color: var(--muted); font-size: 12.5px; }

  .user { align-self: flex-end; max-width: 88%; background: var(--accent); color: var(--accent-ink); padding: 9px 12px; border-radius: 12px 12px 3px 12px; }
  .user p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
  .user small { opacity: 0.7; font-size: 11px; }

  .agent { display: grid; grid-template-columns: 26px 1fr; gap: 9px; align-items: start; }
  .avatar { width: 26px; height: 26px; border-radius: 8px; display: grid; place-items: center; background: var(--accent-soft); color: var(--accent); }
  .avatar svg { width: 14px; height: 14px; fill: currentColor; }
  .avatar.thinking svg { animation: spin 2.4s linear infinite; }
  .body { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
  .md :global(p) { margin: 0 0 6px; }
  .md :global(ul), .md :global(ol) { margin: 0 0 6px; padding-left: 18px; }
  .md :global(code) { font-size: 12px; padding: 1px 5px; border-radius: 4px; background: var(--panel-3); }
  .md :global(pre) { margin: 0 0 6px; padding: 8px 10px; background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; overflow: auto; font-size: 12px; }
  .md :global(h4) { margin: 4px 0; font-size: 13px; }
  .muted { margin: 0; color: var(--faint); }
  .error { margin: 0; color: var(--bad); }

  .edit {
    display: grid;
    grid-template-columns: 16px 1fr auto;
    align-items: center;
    gap: 8px;
    padding: 5px 9px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--panel-2);
    font: 11.5px var(--mono);
    color: var(--muted);
    animation: rise 0.25s ease-out;
  }
  .edit[data-state="failed"] { opacity: 0.55; text-decoration: line-through; }
  .edit .what { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .edit .state { color: var(--good); font-size: 11px; }
  .edit[data-state="failed"] .state { color: var(--warn); text-decoration: none; }
  .icon { width: 14px; height: 14px; background: var(--accent); mask: var(--m) center / contain no-repeat; -webkit-mask: var(--m) center / contain no-repeat; }
  .icon[data-icon="eye"] { --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M8 3C4 3 1.5 8 1.5 8S4 13 8 13s6.5-5 6.5-5S12 3 8 3zm0 7.5A2.5 2.5 0 1 1 8 5.5a2.5 2.5 0 0 1 0 5z'/%3E%3C/svg%3E"); }
  .icon[data-icon="file"] { --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M3 1.5h6l4 4v9H3zM9 1.5v4h4'/%3E%3C/svg%3E"); background: var(--good); }
  .icon[data-icon="pen"] { --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M11.3 1.7l3 3L5 14H2v-3z'/%3E%3C/svg%3E"); background: var(--syn-host); }
  .icon[data-icon="check"] { --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M8 1l6 2.5V8c0 3.5-2.6 6-6 7-3.4-1-6-3.5-6-7V3.5zm-1 9.6L11.6 6 10.5 4.9 7 8.4 5.5 6.9 4.4 8z'/%3E%3C/svg%3E"); background: var(--cyan); }
  .icon[data-icon="warn"] { --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M8 1l7.5 13h-15zm-.9 5v4h1.8V6zm0 5.2V13h1.8v-1.8z'/%3E%3C/svg%3E"); background: var(--warn); }
  .edit.warn { border-color: color-mix(in srgb, var(--warn) 45%, var(--line)); }
  .edit.warn .state { color: var(--warn); }
  .report { margin: -2px 0 2px; font-size: 11.5px; color: var(--muted); }
  .report summary { cursor: pointer; padding: 0 9px; }
  .report pre { margin: 4px 0 0; padding: 8px 10px; max-height: 200px; overflow: auto; white-space: pre-wrap; font-size: 11px; color: var(--warn); background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; }
  @keyframes rise { from { opacity: 0; transform: translateY(4px); } }

  .typing { display: flex; gap: 4px; padding: 4px 0; }
  .typing i { width: 6px; height: 6px; border-radius: 50%; background: var(--faint); animation: pulse 1s infinite; }
  .typing i:nth-child(2) { animation-delay: 0.15s; }
  .typing i:nth-child(3) { animation-delay: 0.3s; }

  .run {
    display: grid;
    grid-template-columns: 22px 1fr auto;
    align-items: center;
    gap: 9px;
    padding: 8px 11px;
    border-radius: 9px;
    border: 1px dashed var(--line-2);
    background: transparent;
    color: var(--muted);
    text-align: left;
    font-size: 12.5px;
  }
  .run:hover, .run.selected { border-style: solid; border-color: color-mix(in srgb, var(--cyan) 50%, transparent); color: var(--ink); }
  .run .play { width: 22px; height: 22px; border-radius: 6px; display: grid; place-items: center; font-size: 9px; background: color-mix(in srgb, var(--cyan) 16%, transparent); color: var(--cyan); }
  .run[data-status="error"] .play { background: color-mix(in srgb, var(--bad) 16%, transparent); color: var(--bad); font-weight: 700; }
  .run .time { font: 11px var(--mono); color: var(--faint); }

  .failed { font-size: 12px; color: var(--bad); display: flex; gap: 8px; align-items: center; }
  .failed button { border: 1px solid var(--line); background: var(--panel-2); border-radius: 6px; padding: 2px 8px; }

  .suggest { margin-bottom: 8px; padding: 9px 10px; border: 1px solid color-mix(in srgb, var(--warn) 45%, var(--line)); border-radius: 9px; background: color-mix(in srgb, var(--warn) 7%, var(--panel-2)); font-size: 12.5px; animation: rise 0.25s ease-out; }
  .suggest .head { display: flex; align-items: center; gap: 7px; color: var(--muted); }
  .suggest .head b { color: var(--ink); }
  .suggest pre { margin: 7px 0 8px; padding: 7px 9px; max-height: 120px; overflow: auto; white-space: pre-wrap; font-size: 11px; color: var(--warn); background: var(--panel); border: 1px solid var(--line); border-radius: 6px; }
  .suggest .actions { display: flex; gap: 6px; }
  .suggest button { font-size: 11.5px; padding: 4px 10px; border-radius: 7px; border: 1px solid var(--line); background: var(--panel); color: var(--muted); }
  .suggest button:hover { color: var(--ink); border-color: var(--line-2); }
  .suggest button.primary { border: 0; background: var(--accent); color: var(--accent-ink); font-weight: 600; }

  .composer { flex: none; padding: 10px 12px 12px; border-top: 1px solid var(--line); background: var(--panel); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .chips button { font-size: 11.5px; padding: 4px 9px; border-radius: 999px; border: 1px solid var(--line); background: var(--panel-2); color: var(--muted); }
  .chips button:hover { color: var(--ink); border-color: var(--line-2); }
  .box { border: 1px solid var(--line-2); border-radius: 10px; background: var(--panel-2); transition: border-color 0.15s, box-shadow 0.15s; }
  .box:focus-within { border-color: color-mix(in srgb, var(--accent) 60%, transparent); box-shadow: 0 0 0 3px var(--accent-soft); }
  textarea { width: 100%; resize: none; border: 0; outline: none; background: transparent; padding: 10px 12px 2px; }
  .row { display: flex; align-items: center; justify-content: space-between; padding: 4px 6px 6px 12px; }
  .hint { font-size: 10.5px; color: var(--faint); }
  kbd { font-size: 10px; padding: 0 4px; border: 1px solid var(--line-2); border-radius: 3px; }
  .send { width: 30px; height: 30px; border-radius: 8px; border: 0; background: var(--accent); color: var(--accent-ink); display: grid; place-items: center; }
  .send:disabled { opacity: 0.4; }
  .send svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

  .spinner { display: inline-block; width: 11px; height: 11px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.7s linear infinite; }
</style>
