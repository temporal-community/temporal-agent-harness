<script lang="ts">
  import { tick } from "svelte";
  import {
    AlertTriangle,
    Bot,
    BrainCircuit,
    CheckCircle2,
    ChevronDown,
    ChevronRight,
    Clock3,
    Cpu,
    MessageSquarePlus,
    Radio,
    UserRound,
    Wrench,
    XCircle
  } from "@lucide/svelte";
  import { Search, X } from "@lucide/svelte";
  import Badge from "$lib/components/primitives/Badge.svelte";
  import StatusChip, {
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";
  import {
    formatDuration,
    type ReplayLogRow,
    type TurnLogGroup
  } from "$lib/state/replayLog";
  import { formatCost, formatTokens } from "$lib/cost/pricing";

  export type TranscriptFilter = "all" | "model" | "tool" | "approval" | "error";

  interface Props {
    groups: TurnLogGroup[];
    activeTurnNumber: number | null;
    activeRowId?: string | null;
    activeOrdinal: number | null;
    filter?: TranscriptFilter;
    onFilterChange?: (filter: TranscriptFilter) => void;
  }

  let {
    groups,
    activeTurnNumber,
    activeRowId = null,
    activeOrdinal,
    filter = "all",
    onFilterChange
  }: Props = $props();
  let expandedRows = $state<Record<string, boolean>>({});
  let query = $state("");

  const filters: Array<{ key: TranscriptFilter; label: string }> = [
    { key: "all", label: "All" },
    { key: "model", label: "Model" },
    { key: "tool", label: "Tools" },
    { key: "approval", label: "Approvals" },
    { key: "error", label: "Errors" }
  ];

  function matchesFilter(row: ReplayLogRow, key: TranscriptFilter): boolean {
    if (key === "all") return true;
    if (key === "model") return row.actor === "model" || row.actor === "reasoning";
    if (key === "tool") return row.actor === "tool";
    if (key === "approval") return row.actor === "approval";
    if (key === "error") return row.tone === "error" || row.actor === "error";
    return true;
  }

  interface PrimaryPayload {
    label: string;
    text: string;
    kind: "block" | "text";
  }

  function formatLogValue(value: unknown): string {
    if (value == null) return "";
    if (typeof value === "string") return value.trim();
    return JSON.stringify(value, null, 2);
  }

  function inputText(row: ReplayLogRow): string {
    return formatLogValue(row.input);
  }

  function primaryPayload(row: ReplayLogRow): PrimaryPayload | null {
    const output = formatLogValue(row.output);
    if (output) return { label: "output", text: output, kind: "block" };

    return null;
  }

  function fullLogDetail(row: ReplayLogRow): string {
    const sections: string[] = [];
    const body = formatLogValue(row.body);
    const detail = formatLogValue(row.detail);
    const input = inputText(row);
    const output = formatLogValue(row.output);
    const citations = row.citations
      .map(
        (citation) =>
          citation.custom_metadata?.deep_url ??
          citation.document_uri ??
          citation.file_name ??
          "Source"
      )
      .filter(Boolean);
    const metadata = [
      `event: ${row.event}`,
      `actor: ${row.actor}`,
      `turn: ${row.turnNumber}`,
      `source_turn: ${row.sourceTurnNumber}`,
      row.sourceLabel ? `source: ${row.sourceLabel}` : "",
      row.workflowId ? `workflow_id: ${row.workflowId}` : "",
      row.status ? `status: ${row.status}` : "",
      row.toolName ? `tool: ${row.toolName}` : "",
      row.toolId ? `tool_id: ${row.toolId}` : "",
      row.model ? `model: ${row.model}` : "",
      `timestamp: ${time(row.timestamp)}`
    ].filter(Boolean);

    if (output) sections.push(`output:\n${output}`);
    if (body) sections.push(`message:\n${body}`);
    if (detail) sections.push(`detail:\n${detail}`);
    if (input) sections.push(`input:\n${input}`);
    if (citations.length > 0) sections.push(`citations:\n${citations.join("\n")}`);
    sections.push(metadata.join("\n"));

    return sections.join("\n\n");
  }

  function matchesQuery(row: ReplayLogRow, needle: string): boolean {
    if (!needle) return true;
    const haystack = [
      row.label,
      row.body,
      row.detail,
      row.status,
      row.toolName,
      row.output,
      inputText(row)
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  }

  const filtering = $derived(filter !== "all" || query.trim().length > 0);

  const visibleGroups = $derived(
    groups
      .map((group) => {
        const needle = query.trim().toLowerCase();
        const rows = group.rows.filter(
          (row) =>
            row.ordinal === activeOrdinal ||
            (matchesFilter(row, filter) && matchesQuery(row, needle))
        );
        return { ...group, rows };
      })
      .filter((group) => group.rows.length > 0)
  );

  const totalRows = $derived(
    groups.reduce((sum, group) => sum + group.rows.length, 0)
  );
  const shownRows = $derived(
    visibleGroups.reduce((sum, group) => sum + group.rows.length, 0)
  );

  $effect(() => {
    const rowId = activeRowId;
    if (rowId == null) {
      expandedRows = {};
      return;
    }
    expandedRows = { [rowId]: true };
    tick().then(() => {
      document
        .getElementById(`log-row-${rowId}`)
        ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  });

  function time(value: number): string {
    return new Date(value * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
  }

  function isRowExpanded(rowId: string): boolean {
    return expandedRows[rowId] ?? false;
  }

  function toggleRow(rowId: string): void {
    expandedRows = {
      ...expandedRows,
      [rowId]: !isRowExpanded(rowId)
    };
  }

  function actorLabel(row: ReplayLogRow): string {
    const prefix = row.parentTurnNumber != null ? "Subagent " : "";
    if (row.parentTurnNumber != null && row.actor === "user") return "Subagent input";
    if (row.parentTurnNumber != null && row.actor === "agent") return "Subagent output";
    if (row.actor === "user") return "User";
    if (row.actor === "agent") return "Agent";
    if (row.actor === "model") return `${prefix}Model`;
    if (row.actor === "tool") return `${prefix}${row.toolName ?? "Tool"}`;
    if (row.actor === "approval") return `${prefix}Approval`;
    if (row.actor === "operator") return `${prefix}Operator`;
    if (row.actor === "subagent") return "Subagent";
    if (row.actor === "queue") return `${prefix}Queue`;
    if (row.actor === "reasoning") return `${prefix}Reasoning`;
    if (row.actor === "error") return `${prefix}Error`;
    return `${prefix}System`;
  }

  function statusKind(row: ReplayLogRow): StatusKind {
    const status = row.status?.toLowerCase() ?? "";
    if (row.tone === "error" || row.actor === "error" || status.includes("fail")) {
      return "error";
    }
    if (row.actor === "approval" || status.includes("approval") || status.includes("await")) {
      return "approval";
    }
    if (row.actor === "operator") return "queued";
    if (row.actor === "tool" || status.includes("tool")) return "tool";
    if (row.actor === "model") return "model";
    if (row.actor === "reasoning") return "reasoning";
    if (row.actor === "subagent") return "delegating";
    if (row.actor === "queue" || status.includes("queue")) return "queued";
    if (status.includes("done") || status.includes("complete") || status.includes("approved")) {
      return "complete";
    }
    if (status.includes("running") || status.includes("streaming")) return "thinking";
    return "idle";
  }
</script>

<section class="transcript" aria-label="Replay logs">
  <div class="transcript-head">
    <h2>Logs</h2>
    <Badge
      label={filtering ? `${shownRows}/${totalRows} events` : `${totalRows} events`}
      tone="neutral"
    />
  </div>

  <div class="transcript-controls">
    <div class="filter-chips" role="group" aria-label="Filter logs">
      {#each filters as item}
        <button
          class:active={filter === item.key}
          type="button"
          aria-pressed={filter === item.key}
          onclick={() => onFilterChange?.(item.key)}
        >
          {item.label}
        </button>
      {/each}
    </div>
    <div class="search">
      <Search size={14} />
      <input
        type="search"
        placeholder="Search events"
        bind:value={query}
        aria-label="Search log events"
      />
      {#if query}
        <button class="clear" type="button" aria-label="Clear search" onclick={() => (query = "")}>
          <X size={13} />
        </button>
      {/if}
    </div>
  </div>

  <div class="items">
    {#if groups.length === 0}
      <p class="empty">Step through the stream to build the logs.</p>
    {:else if visibleGroups.length === 0}
      <p class="empty">No events match this filter.</p>
    {:else}
      {#each visibleGroups as group}
        <section
          class={`turn-group ${activeTurnNumber === group.turnNumber ? "active-turn" : ""}`}
          aria-label={`Turn ${group.turnNumber}`}
        >
          <header class="turn-head">
            <div class="turn-summary">
              <span class="turn-main">
                <span class="turn-title">Turn {group.turnNumber}</span>
                <span class="turn-preview">{group.summary.preview}</span>
              </span>
              <span class="turn-meta">
                <time>{time(group.startedAt)}</time>
              </span>
            </div>

            <div class="turn-stats" aria-label={`Turn ${group.turnNumber} summary`}>
              <span>{formatDuration(group.summary.durationSeconds)}</span>
              <span>{group.summary.eventCount} events</span>
              {#if group.summary.modelCalls}
                <span>{group.summary.modelCalls} model</span>
              {/if}
              {#if group.summary.toolCalls}
                <span>{group.summary.toolCalls} tools</span>
              {/if}
              {#if group.summary.approvals}
                <span>{group.summary.approvals} approvals</span>
              {/if}
              {#if group.summary.errors}
                <span class="error-stat">{group.summary.errors} errors</span>
              {/if}
              {#if group.summary.tokens}
                <span>{formatTokens(group.summary.tokens)} tok</span>
              {/if}
              {#if group.summary.tokens && group.summary.estimatedCostUsd != null}
                <span>{formatCost(group.summary.estimatedCostUsd)}</span>
              {/if}
            </div>
          </header>

          <div class="log-lines" id={`turn-${group.turnNumber}-logs`}>
            {#each group.rows as row}
              {@const expanded = isRowExpanded(row.id)}
              {@const active = activeRowId === row.id || (activeRowId == null && activeOrdinal === row.ordinal)}
              <article
                id={`log-row-${row.id}`}
                class={`log-line ${row.tone} ${row.parentTurnNumber != null ? "nested-subagent" : ""} ${expanded ? "expanded" : ""} ${active ? "active-row" : ""}`}
              >
                <div class="actor-icon" aria-hidden="true">
                  {#if row.actor === "user"}
                    <UserRound size={15} />
                  {:else if row.actor === "agent" || row.actor === "subagent"}
                    <Bot size={15} />
                  {:else if row.actor === "model"}
                    <Cpu size={15} />
                  {:else if row.actor === "reasoning"}
                    <BrainCircuit size={15} />
                  {:else if row.actor === "approval"}
                    {#if row.tone === "done"}
                      <CheckCircle2 size={15} />
                    {:else if row.tone === "error"}
                      <XCircle size={15} />
                    {:else}
                      <Clock3 size={15} />
                    {/if}
                  {:else if row.actor === "queue"}
                    <MessageSquarePlus size={15} />
                  {:else if row.actor === "operator"}
                    <Radio size={15} />
                  {:else if row.actor === "error"}
                    <AlertTriangle size={15} />
                  {:else if row.tone === "done"}
                    <CheckCircle2 size={15} />
                  {:else if row.tone === "error"}
                    <XCircle size={15} />
                  {:else if row.actor === "system"}
                    <Radio size={15} />
                  {:else}
                    <Wrench size={15} />
                  {/if}
                </div>

                <div class="line-content">
                  <button
                    class="line-toggle"
                    type="button"
                    aria-expanded={expanded}
                    aria-controls={`log-row-${row.id}-details`}
                    onclick={() => toggleRow(row.id)}
                  >
                    <span class="line-toggle-main">
                      <span class="line-meta">
                        <span class="actor-name">{actorLabel(row)}</span>
                        <time>{time(row.timestamp)}</time>
                        <Badge label={row.label} tone={row.tone} />
                        {#if row.status}
                          <StatusChip
                            label={row.status}
                            kind={statusKind(row)}
                            compact
                            active={row.ordinal === activeOrdinal}
                          />
                        {/if}
                        {#if row.parentTurnNumber != null}
                          <span class="source-turn" title={row.sourceLabel ?? undefined}>
                            subagent turn {row.sourceTurnNumber}
                          </span>
                        {/if}
                      </span>
                    </span>
                    <span class="row-toggle-icon" aria-hidden="true">
                      {#if expanded}
                        <ChevronDown size={15} />
                      {:else}
                        <ChevronRight size={15} />
                      {/if}
                    </span>
                  </button>

                  {#if expanded}
                    {@const primary = primaryPayload(row)}
                    {@const fullDetail = fullLogDetail(row)}
                    <div class="line-details" id={`log-row-${row.id}-details`}>
                      {#if primary}
                        <section class="primary-payload" aria-label={`${primary.label} preview`}>
                          <span class="payload-label">{primary.label}</span>
                          {#if primary.kind === "text"}
                            <p>{primary.text}</p>
                          {:else}
                            <pre class="primary-pre">{primary.text}</pre>
                          {/if}
                        </section>
                      {/if}
                      {#if row.citations.length}
                        <div class="citations">
                          {#each row.citations as citation}
                            <a
                              href={citation.custom_metadata?.deep_url ?? citation.document_uri ?? "#"}
                              target="_blank"
                              rel="noreferrer"
                            >
                              {citation.custom_metadata?.heading ?? citation.file_name ?? "Source"}
                            </a>
                          {/each}
                        </div>
                      {/if}
                      <section class="full-details" aria-label="Full details">
                        <span class="payload-label">Full details</span>
                        <pre>{fullDetail}</pre>
                      </section>
                    </div>
                  {/if}
                </div>
              </article>
            {/each}
          </div>
        </section>
      {/each}
    {/if}
  </div>
</section>

<style>
  .transcript {
    min-width: 330px;
    max-width: 430px;
    border-left: 1px solid var(--border);
    background: var(--surface-1);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .transcript-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 14px 10px;
    border-bottom: 1px solid var(--border);
  }

  h2 {
    margin: 0;
    font-size: 13px;
  }

  .transcript-controls {
    display: grid;
    gap: 8px;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
  }

  .filter-chips {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .filter-chips button {
    padding: 4px 9px;
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--text-3);
    background: var(--surface-0);
    cursor: pointer;
    font: inherit;
    font-size: 11px;
  }

  .filter-chips button:hover {
    color: var(--text-1);
    border-color: var(--border-strong);
  }

  .filter-chips button.active {
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
    background: color-mix(in srgb, var(--accent) 13%, var(--surface-2));
  }

  .search {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 0 8px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-0);
    color: var(--text-3);
  }

  .search input {
    flex: 1;
    min-width: 0;
    height: 30px;
    border: 0;
    background: transparent;
    color: var(--text-1);
    font-size: 12px;
    outline: none;
  }

  .search input::placeholder {
    color: var(--text-3);
  }

  .search .clear {
    display: inline-flex;
    padding: 2px;
    border: 0;
    border-radius: 4px;
    color: var(--text-3);
    background: transparent;
    cursor: pointer;
  }

  .search .clear:hover {
    color: var(--text-1);
  }

  .items {
    min-height: 0;
    overflow-y: auto;
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .turn-group {
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    min-height: 58px;
    border: 1px solid var(--border);
    border-radius: 7px;
    overflow: hidden;
    background: color-mix(in srgb, var(--surface-1) 75%, var(--surface-0));
    transition:
      border-color 140ms ease,
      transform 140ms ease,
      background 140ms ease;
  }

  .turn-group:hover,
  .turn-group.active-turn {
    border-color: var(--border-strong);
    transform: translateY(-1px);
    background: color-mix(in srgb, var(--surface-2) 38%, var(--surface-0));
  }

  .turn-group.active-turn {
    box-shadow: inset 3px 0 0 color-mix(in srgb, var(--accent) 70%, transparent);
  }

  .turn-head {
    z-index: 1;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-2);
    font-size: 11px;
    font-weight: 650;
  }

  .turn-summary {
    width: 100%;
    min-height: 42px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 7px 9px 5px;
    color: inherit;
  }

  .turn-main {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .turn-title {
    color: var(--text-1);
    font-size: 12px;
  }

  .turn-preview {
    max-width: 275px;
    overflow: hidden;
    color: var(--text-3);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .turn-meta {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .turn-meta time {
    color: var(--text-3);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }

  .turn-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    padding: 0 9px 8px;
    color: var(--text-3);
    font-size: 10px;
    font-variant-numeric: tabular-nums;
  }

  .turn-stats span {
    padding: 2px 6px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface-0);
  }

  .turn-stats .error-stat {
    color: var(--error);
  }

  .log-lines {
    display: grid;
    overflow: visible;
  }

  .log-line {
    display: grid;
    grid-template-columns: 24px minmax(0, 1fr);
    gap: 8px;
    padding: 8px 9px;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
    background: transparent;
  }

  .log-line.nested-subagent {
    position: relative;
    margin-left: 18px;
    padding-left: 12px;
    border-left: 1px solid color-mix(in srgb, var(--accent) 36%, transparent);
    background: color-mix(in srgb, var(--accent) 5%, transparent);
  }

  .log-line.nested-subagent::before {
    content: "";
    position: absolute;
    left: -1px;
    top: 19px;
    width: 10px;
    border-top: 1px solid color-mix(in srgb, var(--accent) 36%, transparent);
  }

  .log-line:last-child {
    border-bottom: 0;
  }

  .log-line.active-row {
    background: color-mix(in srgb, var(--accent) 12%, transparent);
  }

  .actor-icon {
    width: 24px;
    height: 24px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-2);
    background: var(--surface-0);
  }

  .log-line.model .actor-icon { color: var(--model); }
  .log-line.tool .actor-icon { color: var(--warning); }
  .log-line.approval .actor-icon,
  .log-line.queue .actor-icon { color: var(--queue); }
  .log-line.operator .actor-icon { color: var(--accent); }
  .log-line.done .actor-icon { color: var(--success); }
  .log-line.error .actor-icon { color: var(--error); }

  .line-content {
    min-width: 0;
  }

  .line-toggle {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px;
    align-items: start;
    padding: 0;
    border: 0;
    color: inherit;
    background: transparent;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .line-toggle:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 55%, transparent);
    outline-offset: 3px;
    border-radius: 4px;
  }

  .line-toggle-main {
    min-width: 0;
    display: grid;
  }

  .row-toggle-icon {
    width: 20px;
    height: 20px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-top: 1px;
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--text-3);
    background: var(--surface-0);
  }

  .log-line:hover .row-toggle-icon,
  .log-line.expanded .row-toggle-icon,
  .line-toggle:focus-visible .row-toggle-icon {
    color: var(--text-1);
    border-color: var(--border-strong);
  }

  .line-meta {
    min-width: 0;
    display: flex;
    align-items: center;
    flex-wrap: nowrap;
    gap: 6px;
    overflow: hidden;
    color: var(--text-3);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }

  .log-line.expanded .line-meta {
    flex-wrap: wrap;
  }

  .actor-name {
    flex: 0 0 auto;
    color: var(--text-2);
    font-weight: 650;
  }

  .source-turn {
    flex: 0 0 auto;
    padding: 1px 5px;
    border: 1px solid color-mix(in srgb, var(--accent) 34%, transparent);
    border-radius: 999px;
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 9%, transparent);
    font-size: 10px;
  }

  .line-meta time {
    flex: 0 0 auto;
  }

  .line-meta :global(.badge) {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .line-details {
    display: grid;
    gap: 8px;
    margin-top: 8px;
  }

  p {
    margin: 0;
    color: var(--text-1);
    font-size: 12px;
    line-height: 1.42;
    word-break: break-word;
  }

  .primary-payload {
    min-width: 0;
    display: grid;
    gap: 5px;
  }

  .payload-label {
    color: var(--text-3);
    font-size: 11px;
    font-weight: 650;
  }

  pre {
    margin: 0;
    padding: 8px;
    border-radius: 6px;
    overflow-x: auto;
    background: var(--surface-0);
    color: var(--text-2);
    font-size: 11px;
  }

  .primary-pre {
    color: var(--text-1);
  }

  .full-details pre {
    margin-top: 6px;
    color: var(--text-2);
  }

  .citations {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .citations a {
    color: var(--accent);
    font-size: 11px;
    text-decoration: none;
    border-bottom: 1px solid color-mix(in srgb, var(--accent) 50%, transparent);
  }

  .empty {
    color: var(--text-3);
  }
</style>
