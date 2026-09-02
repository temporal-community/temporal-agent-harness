<script lang="ts">
  import { Braces, RefreshCw, Search, X } from "@lucide/svelte";
  import type { AccountResource, ToolCallRecord } from "$lib/api/types";
  import StatusChip, {
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";

  interface Props {
    server: AccountResource;
    calls: ToolCallRecord[];
    loading?: boolean;
    error?: string | null;
    left: number;
    top: number;
    onRefresh: () => void | Promise<void>;
    onClose: () => void;
  }

  let {
    server,
    calls,
    loading = false,
    error = null,
    left,
    top,
    onRefresh,
    onClose
  }: Props = $props();

  let search = $state("");
  let expandedCallId = $state<string | null>(null);
  const searchTerm = $derived(search.trim().toLowerCase());
  const visibleCalls = $derived(
    searchTerm
      ? calls.filter((call) =>
          [
            call.tool_name,
            call.status,
            call.workflow_id ?? "",
            call.agent_id ?? "",
            call.execution_id
          ].some((value) => value.toLowerCase().includes(searchTerm))
        )
      : calls
  );

  function statusKind(status: ToolCallRecord["status"]): StatusKind {
    if (status === "completed") return "complete";
    if (status === "running") return "tool";
    return "error";
  }

  function timeLabel(seconds: number): string {
    return new Date(seconds * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
  }

  function json(value: unknown): string {
    if (value == null) return "—";
    return JSON.stringify(value, null, 2);
  }
</script>

<section
  class="tool-history"
  aria-label={`${server.name} tool calls`}
  style={`--tool-history-left: ${left}px; --tool-history-top: ${top}px`}
>
  <header class="history-head">
    <span class="history-title">
      <Braces size={15} />
      <span>{server.name} tool calls</span>
      <small>{calls.length}</small>
    </span>
    <div class="history-actions">
      <button
        type="button"
        class:spinning={loading}
        class="icon-button"
        aria-label={`Refresh ${server.name} tool calls`}
        disabled={loading}
        onclick={() => void onRefresh()}
      >
        <RefreshCw size={14} />
      </button>
      <button type="button" class="icon-button" aria-label="Close tool calls" onclick={onClose}>
        <X size={15} />
      </button>
    </div>
  </header>

  <div class="server-route">
    <span>{server.kind === "nexus" ? "Nexus" : "External HTTP"}</span>
    <code>{server.endpoint}</code>
    {#if server.service}<code>{server.service}</code>{/if}
  </div>

  <label class="history-search">
    <Search size={14} aria-hidden="true" />
    <input bind:value={search} placeholder="Search tool calls" aria-label="Search tool calls" />
  </label>

  <div class="call-list">
    {#if loading && calls.length === 0}
      <p class="empty">Reading retained Temporal history…</p>
    {:else if error}
      <p class="error-message">{error}</p>
    {:else if visibleCalls.length === 0}
      <p class="empty">No retained calls found for this server.</p>
    {/if}

    {#each visibleCalls as call (call.call_id)}
      <article class:expanded={expandedCallId === call.call_id} class="call-row">
        <button
          type="button"
          class="call-summary"
          aria-expanded={expandedCallId === call.call_id}
          onclick={() =>
            (expandedCallId = expandedCallId === call.call_id ? null : call.call_id)}
        >
          <span class="call-copy">
            <time>{timeLabel(call.scheduled_at)}</time>
            <strong>{call.tool_name}</strong>
            <small>
              {call.agent_id ?? call.transport} · {call.namespace}
              {#if call.duration_ms != null} · {Math.round(call.duration_ms)} ms{/if}
            </small>
          </span>
          <StatusChip label={call.status.replace("_", " ")} kind={statusKind(call.status)} compact />
        </button>

        {#if expandedCallId === call.call_id}
          <div class="call-detail">
            <dl>
              <div>
                <dt>{call.transport === "nexus" ? "Scheduled event" : "Activity execution"}</dt>
                <dd>{call.execution_id}</dd>
              </div>
              {#if call.nexus_request_id}
                <div><dt>Nexus request ID</dt><dd>{call.nexus_request_id}</dd></div>
              {/if}
              {#if call.nexus_operation_id}
                <div><dt>Nexus operation ID</dt><dd>{call.nexus_operation_id}</dd></div>
              {/if}
              {#if call.workflow_id}
                <div><dt>Caller agent workflow</dt><dd>{call.workflow_id}</dd></div>
              {/if}
              <div><dt>Transport</dt><dd>{call.transport}</dd></div>
            </dl>
            <section>
              <span>Input</span>
              <pre>{json(call.input)}</pre>
            </section>
            <section>
              <span>{call.error ? "Error" : "Output"}</span>
              <pre class:error={Boolean(call.error)}>{call.error ?? json(call.output)}</pre>
            </section>
          </div>
        {/if}
      </article>
    {/each}
  </div>
</section>

<style>
  .tool-history {
    position: fixed;
    top: var(--tool-history-top);
    left: var(--tool-history-left);
    z-index: 40;
    width: min(620px, calc(100vw - var(--tool-history-left) - 16px));
    max-height: min(720px, calc(100dvh - var(--tool-history-top) - 16px));
    min-height: 0;
    overflow-x: hidden;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 12px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface-1);
    box-shadow: var(--shadow-popover);
  }

  .history-head,
  .history-title,
  .history-actions,
  .server-route,
  .call-summary {
    display: flex;
    align-items: center;
  }

  .history-head {
    min-width: 0;
    justify-content: space-between;
    gap: 10px;
  }

  .history-title {
    min-width: 0;
    gap: 7px;
    color: var(--text-1);
    font-size: 13px;
    font-weight: 700;
  }

  .history-title small {
    color: var(--text-3);
    font-size: 10px;
  }

  .history-actions {
    gap: 6px;
  }

  .icon-button {
    width: 28px;
    height: 28px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-3);
    background: var(--control-bg);
    cursor: pointer;
  }

  .icon-button:hover:not(:disabled),
  .icon-button:focus-visible {
    border-color: var(--border-strong);
    color: var(--text-1);
    outline: 0;
  }

  .icon-button:disabled {
    cursor: wait;
    opacity: 0.55;
  }

  .icon-button.spinning :global(svg) {
    animation: history-spin 800ms linear infinite;
  }

  @keyframes history-spin {
    to { transform: rotate(360deg); }
  }

  .server-route {
    min-width: 0;
    gap: 6px;
    color: var(--text-3);
    font-size: 10px;
  }

  .server-route span,
  .server-route code {
    overflow: hidden;
    padding: 3px 6px;
    border: 1px solid var(--border);
    border-radius: 999px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .history-search {
    min-width: 0;
    height: 34px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 8px;
    align-items: center;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-3);
    background: var(--control-bg);
  }

  .history-search:focus-within {
    border-color: color-mix(in srgb, var(--accent) 48%, var(--border-strong));
    box-shadow: 0 0 0 3px var(--focus-ring);
  }

  .history-search input {
    min-width: 0;
    border: 0;
    outline: 0;
    color: var(--text-1);
    background: transparent;
    font: inherit;
    font-size: 12px;
  }

  .call-list {
    flex: 0 0 auto;
    min-height: 0;
    overflow: visible;
    display: grid;
    align-content: start;
    gap: 8px;
  }

  .empty,
  .error-message {
    margin: 4px;
    font-size: 11px;
  }

  .empty { color: var(--text-3); }
  .error-message { color: var(--error); }

  .call-row {
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-2) 48%, var(--surface-1));
  }

  .call-row:hover,
  .call-row.expanded {
    border-color: color-mix(in srgb, var(--warning) 42%, var(--border-strong));
  }

  .call-summary {
    width: 100%;
    min-width: 0;
    justify-content: space-between;
    gap: 12px;
    padding: 10px;
    border: 0;
    color: inherit;
    background: transparent;
    cursor: pointer;
    text-align: left;
  }

  .call-copy {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .call-copy time,
  .call-copy small {
    overflow: hidden;
    color: var(--text-3);
    font-size: 10px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .call-copy strong {
    overflow: hidden;
    color: var(--text-1);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .call-detail {
    display: grid;
    gap: 10px;
    padding: 0 10px 10px;
    border-top: 1px solid var(--border);
  }

  .call-detail dl {
    margin: 10px 0 0;
    display: grid;
    gap: 4px;
  }

  .call-detail dl > div {
    min-width: 0;
    display: grid;
    grid-template-columns: 66px minmax(0, 1fr);
    gap: 8px;
    font-size: 10px;
  }

  .call-detail dt,
  .call-detail section > span { color: var(--text-3); }

  .call-detail dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--text-2);
  }

  .call-detail section {
    min-width: 0;
    display: grid;
    gap: 5px;
    font-size: 10px;
  }

  pre {
    max-height: 220px;
    margin: 0;
    overflow: auto;
    padding: 9px;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-2);
    background: var(--surface-0);
    font-family: var(--font-mono);
    font-size: 10px;
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  pre.error { color: var(--error); }
</style>
