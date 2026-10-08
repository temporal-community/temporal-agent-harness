<script lang="ts">
  import { ArrowRight, Bot, MessageCircle } from "@lucide/svelte";
  import type { ReplayLogRow } from "$lib/state/replayLog";
  let { logs, agentLabel, sessionId, closed }: {
    logs: ReplayLogRow[]; agentLabel: string; sessionId: string; closed: boolean;
  } = $props();
  let agents = $derived.by(() => {
    const roster = new Map<string, { id: string; label: string; parent: string; stopped: boolean }>();
    for (const row of logs) {
      if (row.event === "subagent_started" && row.subagentWorkflowId) {
        roster.set(row.subagentWorkflowId, {
          id: row.subagentWorkflowId, label: row.body ?? "Subagent",
          parent: row.workflowId ?? sessionId, stopped: false
        });
      }
      if (row.event === "subagent_stopped" && row.subagentWorkflowId) {
        const child = roster.get(row.subagentWorkflowId);
        if (child) child.stopped = true;
      }
    }
    return [...roster.values()];
  });
  let messages = $derived.by(() => logs.flatMap((row) => {
    if (row.event === "agent_message_sent") return [{
      ...row, kind: "Signal", statusLabel: "Delivered"
    }];
    const child = agents.find((agent) => agent.id === row.workflowId);
    if (!child || (row.event !== "turn_started" && row.event !== "reply")) return [];
    const request = row.event === "turn_started";
    return [{ ...row,
      senderWorkflowId: request ? child.parent : child.id,
      recipientWorkflowId: request ? child.id : child.parent,
      kind: request ? "Turn request" : "Turn reply",
      statusLabel: request ? "Accepted" : "Returned"
    }];
  }));
  function label(id?: string): string {
    if (id === sessionId) return agentLabel;
    return agents.find((agent) => agent.id === id)?.label ?? id ?? "Agent";
  }
  function role(id?: string): string {
    if (id === sessionId) return "Parent";
    return agents.some((agent) => agent.id === id) ? "Subagent" : "Agent";
  }
  function time(timestamp: number): string {
    return new Date(timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
</script>

{#if agents.length || messages.length}
  <details class="messaging" open>
    <summary><MessageCircle size={15} /> Agent communication <span>{agents.length} subagent{agents.length === 1 ? "" : "s"} · {messages.length} message{messages.length === 1 ? "" : "s"}</span></summary>
    <div class="content">
      <div class="roster" aria-label="Agent hierarchy">
        <div class="agent"><Bot size={15} /><span class="role">Parent</span><strong>{agentLabel}</strong><span class="status">{closed ? "Closed" : "Running"}</span></div>
        {#each agents as agent (agent.id)}
          <div class="agent child"><span aria-hidden="true">↳</span><Bot size={15} /><span class="role">Subagent</span><strong>{agent.label}</strong><span class:stopped={agent.stopped} class="status">{agent.stopped ? "Stopped" : "Active"}</span>
            <small>Reports to {role(agent.parent)}: {label(agent.parent)}</small>
            <details class="identity"><summary>Workflow ID</summary><code>{agent.id}</code></details>
          </div>
        {/each}
      </div>
      <div class="exchange" aria-label="Messages between agents" aria-live="polite">
        {#each messages as message (message.id)}
          <article class="message" class:incoming={message.recipientWorkflowId === sessionId}>
            <header>
              <span class="direction">{role(message.senderWorkflowId)} → {role(message.recipientWorkflowId)}</span>
              <span class="message-kind">{message.kind}</span>
              <time>{time(message.timestamp)}</time>
            </header>
            <div class="route">
              <div class="endpoint" title={message.senderWorkflowId}>
                <span class="endpoint-label">From · {role(message.senderWorkflowId)}</span>
                <strong>{label(message.senderWorkflowId)}</strong>
              </div>
              <ArrowRight size={20} aria-hidden="true" />
              <div class="endpoint" title={message.recipientWorkflowId}>
                <span class="endpoint-label">To · {role(message.recipientWorkflowId)}</span>
                <strong>{label(message.recipientWorkflowId)}</strong>
              </div>
            </div>
            <p>{message.body}</p>
            <footer>{message.statusLabel} · {message.kind} · Turn {message.turnNumber}</footer>
          </article>
        {:else}
          <p class="empty">No messages yet.</p>
        {/each}
      </div>
      <p class="note">Signals are delivered without starting a turn. Turn requests and replies are shown separately.</p>
    </div>
  </details>
{/if}

<style>
  .messaging { margin: 12px 16px 0; border: 1px solid var(--border, #334155); border-radius: 10px; background: var(--surface-1, #111827); color: var(--text-1, #e2e8f0); font-size: 12px; flex-shrink: 0; }
  summary { display: flex; align-items: center; gap: 8px; cursor: pointer; padding: 12px; font-weight: 600; }
  summary span { margin-left: auto; font-weight: 400; opacity: .7; }
  .content { padding: 0 12px 12px; max-height: 340px; overflow: auto; }
  .roster { display: grid; gap: 6px; margin-bottom: 14px; }
  .agent { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 7px 9px; background: #64748b15; border-radius: 6px; }
  .child { margin-left: 14px; border-left: 2px solid #818cf8; }
  .agent strong { overflow-wrap: anywhere; }
  .status { margin-left: auto; color: #6ee7b7; font-size: 10px; }
  .status.stopped { color: #94a3b8; }
  small { flex-basis: 100%; opacity: .65; }
  .identity { width: 100%; opacity: .65; }
  .identity summary { padding: 0; font-weight: 400; font-size: 10px; }
  code { overflow-wrap: anywhere; font-size: 10px; }
  .exchange { display: grid; gap: 8px; }
  .message { padding: 10px; border: 1px solid #818cf840; border-left: 3px solid #818cf8; border-radius: 7px; }
  .message.incoming { border-left-color: #2dd4bf; }
  header { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 11px; }
  .direction { font-size: 12px; font-weight: 700; color: #c4b5fd; }
  .incoming .direction { color: #5eead4; }
  .message-kind { color: var(--text-2); font-size: 10px; }
  .role { color: var(--text-1); border: 1px solid var(--border-strong); border-radius: 4px; padding: 2px 5px; font-size: 10px; }
  .route { display: grid; grid-template-columns: minmax(0, 1fr) 20px minmax(0, 1fr); align-items: center; gap: 8px; margin: 10px 0; }
  .endpoint { min-width: 0; padding: 8px; border-radius: 5px; background: var(--surface-2); }
  .endpoint-label { display: block; margin-bottom: 4px; font-size: 10px; color: var(--text-2); font-weight: 600; }
  .endpoint strong { display: block; overflow-wrap: anywhere; font-size: 11px; line-height: 1.4; }
  time { margin-left: auto; opacity: .55; font-size: 10px; }
  p { margin: 8px 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.5; }
  footer, .note, .empty { opacity: .6; font-size: 10px; }
  .note { margin-bottom: 0; }
</style>
