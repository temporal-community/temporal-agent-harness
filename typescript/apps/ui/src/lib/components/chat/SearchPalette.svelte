<script lang="ts" module>
  export type PaletteScope = "all" | "sessions" | "agents" | "panes";
</script>

<script lang="ts">
  /* The console's one search, the way an editor's command palette is. Every search icon opens it,
     already scoped to where it was clicked, and Cmd/Ctrl+K opens it on everything. It lists only
     what the server sent and the panes this console has; picking a row does what clicking it in
     place would. */
  import { ArrowLeft, ArrowRight, Bot, LayoutPanelLeft, MessagesSquare, Search, X } from "@lucide/svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import SessionGlyph from "$lib/components/primitives/SessionGlyph.svelte";
  import { tick } from "svelte";
  import type { AgentDescriptor, Session } from "$lib/api/types";
  import { PANE_META, type PaneKind } from "$lib/panes/registry";
  import { agentName, openSessionStatus, sessionAge, sessionGone, sessionRowStatus, sessionTitle, type RowStatus } from "$lib/state/sessionHistory";

  interface Props {
    open: boolean;
    scope: PaletteScope;
    sessions: Session[];
    agents: AgentDescriptor[];
    closedWorkflowIds?: string[];
    paneKinds: PaneKind[];
    onSession: (workflowId: string) => void;
    onAgent: (workflowType: string) => void;
    onPane: (kind: PaneKind) => void;
  }

  let { open = $bindable(), scope = $bindable(), sessions, agents, closedWorkflowIds = [], paneKinds, onSession, onAgent, onPane }: Props = $props();

  const SCOPES: { key: PaletteScope; label: string }[] = [
    { key: "all", label: "All" },
    { key: "sessions", label: "Sessions" },
    { key: "agents", label: "Agents" },
    { key: "panes", label: "Panes" }
  ];
  const PLACEHOLDER: Record<PaletteScope, string> = {
    all: "Search sessions, agents, and panes…",
    sessions: "Search sessions by message, agent, or ID…",
    agents: "Search agents by name, description, or task queue…",
    panes: "Search panes…"
  };
  /* Everything shows a few of each; one scope shows all of its own. */
  const PER_GROUP = { all: 5, one: 200 };

  type Hit = { key: string; group: string; title: string; detail: string; meta: string; icon: typeof Bot; status?: RowStatus & { unavailable: boolean }; pick: () => void };

  let dialog = $state<HTMLDialogElement | null>(null);
  let input = $state<HTMLInputElement | null>(null);
  let results = $state<HTMLElement | null>(null);
  let query = $state("");
  let active = $state(0);
  const id = $props.id();

  const matches = (text: string) => !query.trim() || text.toLowerCase().includes(query.trim().toLowerCase());

  const groups = $derived.by(() => {
    const now = Date.now();
    const workerless = new Set(agents.filter((agent) => agent.worker?.status === "no_worker").map((agent) => agent.workflow_type));
    const closedIds = new Set(closedWorkflowIds);
    const limit = scope === "all" ? PER_GROUP.all : PER_GROUP.one;
    const out: { label: string; hits: Hit[] }[] = [];
    if (scope === "all" || scope === "sessions") {
      const hits = [...sessions]
        .sort((a, b) => b.created_at - a.created_at)
        .filter((session) => matches(`${sessionTitle(session)} ${agentName(agents, session.agent_workflow_type)} ${session.workflow_id}`))
        .slice(0, limit)
        .map((session) => ({
          key: `s:${session.workflow_id}`,
          group: "Sessions",
          title: sessionTitle(session),
          detail: agentName(agents, session.agent_workflow_type),
          meta: sessionAge(session.created_at, now).short,
          icon: MessagesSquare,
          status: { ...sessionRowStatus(session, workerless, closedIds, openSessionStatus.get(session.workflow_id)), unavailable: sessionGone(session) },
          pick: () => onSession(session.workflow_id)
        }));
      if (hits.length) out.push({ label: "Sessions", hits });
    }
    if (scope === "all" || scope === "agents") {
      const hits = agents
        .filter((agent) => matches(`${agent.label} ${agent.description} ${agent.task_queue} ${agent.workflow_type}`))
        .slice(0, limit)
        .map((agent) => ({
          key: `a:${agent.key}`,
          group: "Agents",
          title: agent.label || agent.workflow_type,
          detail: agent.task_queue,
          meta: agent.worker?.status === "no_worker" ? "No worker" : "",
          icon: Bot,
          pick: () => onAgent(agent.workflow_type)
        }));
      if (hits.length) out.push({ label: "Agents", hits });
    }
    if (scope === "all" || scope === "panes") {
      const hits = paneKinds
        .filter((kind) => matches(PANE_META[kind].kindLabel))
        .slice(0, limit)
        .map((kind) => ({
          key: `p:${kind}`,
          group: "Panes",
          title: PANE_META[kind].kindLabel,
          detail: "Open or bring into view",
          meta: "",
          icon: LayoutPanelLeft,
          pick: () => onPane(kind)
        }));
      if (hits.length) out.push({ label: "Panes", hits });
    }
    return out;
  });
  const flat = $derived(groups.flatMap((group) => group.hits));

  /* The native dialog follows `open`; closing it, however it closes, leaves the next search fresh. */
  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      void tick().then(() => input?.focus());
    } else if (!open && dialog.open) dialog.close();
  });

  function closed(): void {
    open = false;
    query = "";
    active = 0;
  }

  /* Scrolls the results alone, never the page or the pane rail behind the dialog. */
  function keepInView(row: HTMLElement | null): void {
    if (!row || !results) return;
    const box = row.getBoundingClientRect();
    const view = results.getBoundingClientRect();
    if (box.top < view.top) results.scrollTop -= view.top - box.top;
    else if (box.bottom > view.bottom) results.scrollTop += box.bottom - view.bottom;
  }

  function pick(hit: Hit | undefined): void {
    if (!hit) return;
    open = false;
    hit.pick();
  }

  function setScope(next: PaletteScope): void {
    scope = next;
    active = 0;
  }

  function moveScope(step: number): void {
    const at = SCOPES.findIndex((item) => item.key === scope);
    setScope(SCOPES[(at + step + SCOPES.length) % SCOPES.length].key);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (flat.length) active = (active + (event.key === "ArrowDown" ? 1 : -1) + flat.length) % flat.length;
      keepInView(document.getElementById(`${id}-hit-${active}`));
    } else if (event.key === "Enter") {
      event.preventDefault();
      pick(flat[active]);
    } else if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && !query) {
      event.preventDefault();
      moveScope(event.key === "ArrowRight" ? 1 : -1);
    } else if (event.key === "Tab") {
      event.preventDefault();
      moveScope(event.shiftKey ? -1 : 1);
    }
  }
</script>

<dialog
  bind:this={dialog}
  class="palette"
  aria-label="Search"
  onclose={closed}
  onclick={(event) => event.target === dialog && (open = false)}
  onkeydown={(event) => event.stopPropagation()}
>
  <div class="palette-search">
    <label class="field">
      <Search size={14} aria-hidden="true" />
      <input
        bind:this={input}
        bind:value={query}
        placeholder={PLACEHOLDER[scope]}
        aria-label="Search"
        role="combobox"
        aria-expanded="true"
        aria-controls={`${id}-list`}
        aria-activedescendant={flat.length ? `${id}-hit-${active}` : undefined}
        oninput={() => (active = 0)}
        onkeydown={onKeydown}
      />
    </label>
    <button
      type="button"
      class="head-button"
      aria-label="Close"
      data-tip="Close&#10;Esc"
      data-tip-below
      data-tip-align="end"
      onclick={() => (open = false)}
    ><X size={14} aria-hidden="true" /></button>
  </div>
  <div class="palette-scopes" role="tablist" aria-label="Search in">
    {#each SCOPES as item (item.key)}
      <Chip size="xs" fill="bare" role="tab" active={scope === item.key} aria-selected={scope === item.key} onclick={() => (setScope(item.key), input?.focus())}>{item.label}</Chip>
    {/each}
    <span class="kicker scope-hint">Tab or <ArrowLeft size={11} aria-hidden="true" /><ArrowRight size={11} aria-hidden="true" /><span class="visually-hidden">the arrow keys</span> to switch</span>
  </div>
  <div class="palette-results" id={`${id}-list`} role="listbox" aria-label="Results" bind:this={results}>
    {#each groups as group (group.label)}
      <div role="group" aria-label={group.label}>
        <h3 class="kicker group-label">{group.label}</h3>
        {#each group.hits as hit (hit.key)}
          {@const index = flat.indexOf(hit)}
          {@const Icon = hit.icon}
          <div
            id={`${id}-hit-${index}`}
            class={["hit", index === active && "active"]}
            role="option"
            tabindex="-1"
            aria-selected={index === active}
            onpointermove={() => (active = index)}
            onclick={() => pick(hit)}
            onkeydown={(event) => event.key === "Enter" && pick(hit)}
          >
            {#if hit.status}<SessionGlyph kind={hit.status.kind} unavailable={hit.status.unavailable} />{:else}<Icon class="hit-icon" size={14} aria-hidden="true" />{/if}
            <span class="hit-title fade-end" dir="auto">{hit.title}</span>
            {#if hit.status}<span class="visually-hidden">, {hit.status.label},</span>{/if}
            <span class="hit-detail fade-end" dir="auto">{hit.detail}</span>
            {#if hit.meta}<span class="kicker hit-meta">{hit.meta}</span>{/if}
          </div>
        {/each}
      </div>
    {:else}
      <p class="no-hits" role="status">{query ? `Nothing matches “${query}”.` : "Nothing here yet."}</p>
    {/each}
  </div>
</dialog>

<style>
  .palette {
    width: min(640px, calc(100% - var(--gutter) * 2));
    max-height: min(70dvh, 560px);
    margin-top: 12dvh;
    padding: 0;
    border: 1px solid var(--border-strong);
    border-radius: 0;
    background: var(--surface-1);
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
    overflow: hidden;
  }

  .palette[open] {
    display: flex;
    flex-direction: column;
  }

  .palette::backdrop {
    background: var(--overlay-scrim);
  }

  .palette-search {
    flex: none;
    display: flex;
    align-items: center;
    gap: var(--gap-md);
    padding: var(--gap-md) var(--pane-inset);
    border-bottom: 1px solid var(--border);
    color: var(--text-3);
  }

  .palette-search .field {
    flex: 1;
    min-width: 0;
  }

  .palette-scopes {
    flex: none;
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
    padding: var(--gap-sm) var(--pane-inset);
    border-bottom: 1px solid var(--border);
  }

  /* The chosen scope outlined, as Setup's SDK switch is. */
  .palette-scopes :global(.chip.bare.active) {
    border-color: var(--border-strong);
    background: transparent;
    color: var(--text-1);
  }

  .hit:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  .scope-hint {
    margin-inline-start: auto;
    display: inline-flex;
    align-items: center;
    gap: 0.5ch;
    color: var(--text-4);
  }

  .palette-results {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    scrollbar-gutter: stable;
    padding: var(--gap-xs) var(--gap-xs) var(--gap-sm);
  }

  .group-label {
    margin: 0;
    padding: var(--gap-md) var(--gap-md) var(--gap-xs);
  }

  .hit {
    display: grid;
    grid-template-columns: 14px minmax(0, max-content) minmax(0, 1fr) auto;
    align-items: baseline;
    gap: var(--gap-md);
    min-height: var(--control-height);
    padding: var(--gap-xs) var(--gap-md);
    cursor: pointer;
  }

  .hit.active {
    background: var(--selected-bg);
  }

  .hit :global(.hit-icon),
  .hit :global(.session-glyph) {
    align-self: center;
    justify-self: center;
    color: var(--text-3);
  }

  .hit-title {
    color: var(--text-1);
    font-size: var(--font-md);
  }

  .hit-detail {
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .hit-meta {
    color: var(--text-4);
  }

  .no-hits {
    margin: 0;
    padding: var(--gutter) var(--gap-md);
    color: var(--text-3);
    font-size: var(--font-md);
    overflow-wrap: anywhere;
  }

  /* No room beside the four scopes on a phone, where nobody has the keys anyway. */
  @media (max-width: 520px) {
    .scope-hint {
      display: none;
    }
  }
</style>
