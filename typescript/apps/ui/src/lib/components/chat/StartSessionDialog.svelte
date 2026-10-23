<script lang="ts" module>
  import type { AgentDescriptor } from "$lib/api/types";
  import { markdownToText } from "$lib/components/chat/MarkdownMessage.svelte";
  import { agentName } from "$lib/state/sessionHistory";

  /**
   * What an agent can be told apart by, read only from fields the registry serves: worker
   * readiness, init data, and the handlers and states of its schema document. Nothing here is
   * about one agent in particular, so an agent the registry says nothing about gets no tags.
   * Capabilities in one group are alternatives (a filter takes any of them), groups narrow.
   */
  interface Capability {
    id: string;
    group: "availability" | "setup" | "interface";
    label: string;
    has: (agent: AgentDescriptor) => boolean;
    /** Shown on the card; availability is not, since a card only speaks up when not ready. */
    tag?: (agent: AgentDescriptor) => string;
  }

  export const CAPABILITIES: readonly Capability[] = [
    { id: "ready", group: "availability", label: "Ready now", has: (agent) => agent.worker?.status === "ready" },
    { id: "no-setup", group: "setup", label: "No setup", has: (agent) => !agent.init_data },
    {
      id: "setup-required",
      group: "setup",
      label: "Setup required",
      has: (agent) => agent.init_data?.required === true,
      tag: () => "Setup required"
    },
    {
      id: "setup-optional",
      group: "setup",
      label: "Optional setup",
      has: (agent) => agent.init_data?.required === false,
      tag: () => "Optional setup"
    },
    {
      id: "live-state",
      group: "interface",
      label: "Live state",
      has: (agent) => (agent.states?.length ?? 0) > 0,
      tag: () => "Live state"
    },
    {
      id: "handlers",
      group: "interface",
      label: "Several handlers",
      has: (agent) => (agent.handlers?.length ?? 0) > 1,
      tag: (agent) => `${agent.handlers?.length} handlers`
    }
  ];

  /* Searched as plain text, so a description's markdown neither matches nor hides a word. */
  export function agentEntries(agents: AgentDescriptor[]) {
    return agents.map((agent, index) => {
      const name = agentName(agents, agent.workflow_type);
      const summary = markdownToText(agent.description).replace(/\s+/g, " ").trim();
      const capabilities = CAPABILITIES.filter((capability) => capability.has(agent));
      const caps = new Set(capabilities.map((capability) => capability.id));
      const tags = capabilities.flatMap((capability) => (capability.tag ? [capability.tag(agent)] : []));
      return { agent, index, name, summary, caps, tags, haystack: `${name}\n${summary}\n${agent.workflow_type}`.toLowerCase() };
    });
  }

  /** The filters worth offering: capabilities some agents have and some do not. */
  export function filterChips<T extends { caps: ReadonlySet<string> }>(entries: T[]) {
    return CAPABILITIES.map((capability) => ({
      ...capability,
      count: entries.filter((entry) => entry.caps.has(capability.id)).length
    })).filter((chip) => chip.count > 0 && chip.count < entries.length);
  }

  export function filterAgents<T extends { haystack: string; caps: ReadonlySet<string> }>(
    entries: T[],
    query: string,
    active: readonly string[] = []
  ): T[] {
    const term = query.trim().toLowerCase();
    const chosen = CAPABILITIES.filter((capability) => active.includes(capability.id));
    const groups = [...new Set(chosen.map((capability) => capability.group))].map((group) => chosen.filter((capability) => capability.group === group));
    return entries.filter(
      (entry) => (!term || entry.haystack.includes(term)) && groups.every((group) => group.some((capability) => entry.caps.has(capability.id)))
    );
  }

  export const MIN_CARD_WIDTH = 190;
  export const CARD_GAP = 8;

  /** One to three columns, as many as fit cards at least MIN_CARD_WIDTH wide. */
  export function columnsFor(width: number): number {
    return Math.min(3, Math.max(1, Math.floor((width + CARD_GAP) / (MIN_CARD_WIDTH + CARD_GAP))));
  }

  /**
   * Where an arrow, Home or End takes the selection in a grid of `count` cards, `columns` to a
   * row, or null for any other key. Down on the row above a short last row lands on its last
   * card; Down on the last row and Up on the first stay put. Left and Right follow the reading
   * direction, so on an RTL page Left moves forward.
   */
  export function gridMove(key: string, index: number, count: number, columns: number, rtl = false): number | null {
    if (count === 0) return null;
    if (index < 0) return key === "End" ? count - 1 : ["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home"].includes(key) ? 0 : null;
    const forward = rtl ? "ArrowLeft" : "ArrowRight";
    const back = rtl ? "ArrowRight" : "ArrowLeft";
    const lastRow = Math.floor((count - 1) / columns);
    if (key === forward) return Math.min(index + 1, count - 1);
    if (key === back) return Math.max(index - 1, 0);
    if (key === "ArrowDown") return index + columns < count ? index + columns : Math.floor(index / columns) < lastRow ? count - 1 : index;
    if (key === "ArrowUp") return index - columns >= 0 ? index - columns : index;
    if (key === "Home") return 0;
    if (key === "End") return count - 1;
    return null;
  }
</script>

<script lang="ts">
  import { ChevronRight, RefreshCw, Search, X } from "@lucide/svelte";
  import { createVirtualizer } from "@tanstack/svelte-virtual";
  import { untrack } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import type { JsonRecord } from "$lib/api/types";
  import MarkdownMessage from "$lib/components/chat/MarkdownMessage.svelte";
  import SchemaForm from "$lib/components/chat/SchemaForm.svelte";
  import { focusFirstProblem, problemSummary } from "$lib/components/chat/schemaForm";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import StatusChip, { type StatusKind } from "$lib/components/primitives/StatusChip.svelte";
  import { windowedFocus } from "$lib/components/primitives/windowedFocus";
  import { initDataForm, initDataPayload, type InitDataForm } from "$lib/state/initData";
  import { keptHighlight } from "$lib/state/quickSwitch";
  import { dissolveClear, inputClearDissolve } from "$lib/motion/inputClearDissolve";

  interface Props {
    agents: AgentDescriptor[];
    preferredAgentType?: string;
    starting?: boolean;
    refreshing?: boolean;
    error?: string | null;
    onRefresh?: () => void | Promise<void>;
    onStart: (workflowType: string, data?: JsonRecord) => void | Promise<void>;
    onClose: () => void;
  }

  let { agents, preferredAgentType, starting = false, refreshing = false, error = null, onRefresh, onStart, onClose }: Props = $props();

  const uid = $props.id();
  const listId = `${uid}-agents`;

  const entries = $derived(agentEntries(agents));
  type Entry = (typeof entries)[number];
  const chips = $derived(filterChips(entries));

  let query = $state("");
  let active = $state<string[]>([]);
  const filtered = $derived(filterAgents(entries, query, active));
  const filteredKeys = $derived(filtered.map((entry) => entry.agent.key));

  function toggle(id: string): void {
    active = active.includes(id) ? active.filter((value) => value !== id) : [...active, id];
  }

  /* What the reader picked, else the agent of the session they are in. The selection shown is
     derived from it, so a search that hides it lands on the first match and clearing the search
     brings it back, and a registry that arrives after the dialog opened still honours it. */
  let chosenKey = $state<string | null>(null);
  const preferredKey = $derived(agents.find((agent) => agent.workflow_type === preferredAgentType)?.key ?? null);
  const selectedKey = $derived(keptHighlight(filteredKeys, chosenKey ?? preferredKey));
  const selectedIndex = $derived(selectedKey == null ? -1 : filteredKeys.indexOf(selectedKey));
  const selected = $derived(selectedIndex >= 0 ? filtered[selectedIndex] : null);
  const agent = $derived(selected?.agent ?? null);

  let dialogElement = $state<HTMLDialogElement | null>(null);
  let searchInput = $state<HTMLInputElement | null>(null);
  let listElement = $state<HTMLElement | null>(null);
  let submitting = $state(false);
  const busy = $derived(starting || submitting);

  /* One form per agent, kept while the dialog is open: a search that moves the selection away
     and back must not throw away what the reader typed. */
  const forms: Record<string, InitDataForm | null> = {};
  function formFor(agent: AgentDescriptor): InitDataForm | null {
    if (!(agent.key in forms)) {
      const created = $state(initDataForm(agent));
      forms[agent.key] = created;
    }
    return forms[agent.key];
  }
  const form = $derived(agent ? formFor(agent) : null);

  /* Kept with the agent they belong to, so choosing another one shows none of them. */
  let failure = $state<{ key: string; problems: Record<string, string>; error: string | null } | null>(null);
  const problems = $derived(failure && failure.key === agent?.key ? failure.problems : {});
  const alert = $derived(failure && failure.key === agent?.key ? (problemSummary(failure.problems) ?? failure.error) : null);
  let expandedKey = $state<string | null>(null);
  const expanded = $derived(expandedKey !== null && expandedKey === agent?.key);
  let clamped = $state(false);

  /* showModal() for the top layer's focus trap and Escape; the search takes focus from the
     header's first button, which showModal() would otherwise give it to. */
  $effect(() => {
    if (dialogElement && !dialogElement.open) {
      dialogElement.showModal();
      searchInput?.focus();
    }
  });

  /* Every card is one height, so a row of them is too, and the window is exact without
     measuring anything. */
  const CARD_HEIGHT = 116;
  const ROW_STRIDE = CARD_HEIGHT + CARD_GAP;
  let columns = $state(2);
  const rowCount = $derived(Math.ceil(filtered.length / columns));
  /* Only the rows in view are in the DOM; each card carries aria-setsize/aria-posinset, so a
     screen reader still hears the full count. */
  const cardRows = createVirtualizer<HTMLElement, HTMLElement>({
    /* Seeded here as well as in the effect below, which SSR never runs. */
    count: untrack(() => rowCount),
    estimateSize: () => ROW_STRIDE,
    getScrollElement: () => listElement,
    initialRect: { width: 0, height: 400 },
    overscan: 3
  });
  /* Untracked: setOptions publishes the store, which would re-run the effect calling it. */
  const rows = () => untrack(() => $cardRows);

  /* Pre, so a shrunk grid's count lands before the rows render and index past its end. */
  $effect.pre(() => {
    const count = rowCount;
    listElement;
    rows().setOptions({ count });
  });

  $effect(() => {
    if (selectedIndex >= 0) rows().scrollToIndex(Math.floor(selectedIndex / columns), { align: "auto" });
  });

  const watchWidth: Attachment<HTMLElement> = (node) => {
    const observer = new ResizeObserver(([entry]) => (columns = columnsFor(entry.contentRect.width)));
    observer.observe(node);
    return () => observer.disconnect();
  };

  const measureClamp: Attachment<HTMLElement> = (node) => {
    const read = () => (clamped = node.scrollHeight > node.clientHeight + 1);
    read();
    const observer = new ResizeObserver(read);
    observer.observe(node);
    return () => observer.disconnect();
  };

  function optionId(entry: Entry): string {
    return `${uid}-option-${entry.index}`;
  }

  /* Ready says nothing: a card only speaks up when starting it would not just work. */
  function statusOf(option: AgentDescriptor): { label: string; kind: StatusKind } | null {
    if (option.worker?.status === "ready") return null;
    if (refreshing) return { label: "Checking…", kind: "idle" };
    if (option.worker?.status === "no_worker") return { label: "No worker", kind: "stuck" };
    if (option.worker?.status === "unknown") return { label: "Unknown", kind: "idle" };
    return null;
  }

  function agentBlocked(agent: AgentDescriptor): boolean {
    return agent.worker?.status === "no_worker";
  }

  function choose(entry: Entry, focusCard = false): void {
    if (busy) return;
    chosenKey = entry.agent.key;
    if (focusCard) focusWhenDrawn(optionId(entry));
  }

  /* The card may be outside the window until the scroll to it lands. */
  function focusWhenDrawn(id: string, tries = 10): void {
    requestAnimationFrame(() => {
      const card = document.getElementById(id);
      if (card) card.focus({ preventScroll: true });
      else if (tries > 0) focusWhenDrawn(id, tries - 1);
    });
  }

  /* The search and the grid share one keyboard. In the search, Left, Right, Home and End stay
     its caret keys and Up and Down move a row; on the grid every arrow moves, and a character
     typed there goes to the search, so typing always filters. Enter starts from either. */
  function handleKey(event: KeyboardEvent, inSearch: boolean): void {
    if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "Enter") {
      event.preventDefault();
      void start();
      return;
    }
    if (inSearch && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const rtl = listElement ? getComputedStyle(listElement).direction === "rtl" : false;
    const next = event.shiftKey ? null : gridMove(event.key, selectedIndex, filtered.length, columns, rtl);
    if (next !== null) {
      event.preventDefault();
      choose(filtered[next], !inSearch);
    } else if (!inSearch && event.key.length === 1 && event.key !== " ") {
      searchInput?.focus();
    }
  }

  function close(): void {
    if (!busy) dialogElement?.close();
  }

  function clearSearch(): void {
    dissolveClear(searchInput, () => (query = ""));
    searchInput?.focus({ preventScroll: true });
  }

  async function start(withData = true): Promise<void> {
    if (!agent || busy || agentBlocked(agent)) return;
    if (!withData && form?.required) return;
    let data: JsonRecord | undefined;
    const key = agent.key;
    if (withData && form) {
      const result = initDataPayload(form);
      failure = { key, problems: "problems" in result ? result.problems : {}, error: "error" in result ? result.error : null };
      if (!("data" in result)) return focusFirstProblem(dialogElement);
      data = result.data;
    }
    submitting = true;
    failure = null;
    try {
      await onStart(agent.workflow_type, data);
      dialogElement?.close();
    } catch (error) {
      failure = { key, problems: {}, error: error instanceof Error ? error.message : "Could not start this session. Try again." };
    } finally {
      submitting = false;
    }
  }
</script>

<dialog
  bind:this={dialogElement}
  class="start-sheet"
  aria-labelledby={`${uid}-title`}
  aria-describedby={`${uid}-subtitle`}
  onkeydown={(event) => {
    event.stopPropagation();
    /* Escape empties the search before it closes the dialog. */
    if (event.key === "Escape" && query && !event.isComposing) {
      event.preventDefault();
      dissolveClear(searchInput, () => (query = ""));
      searchInput?.focus({ preventScroll: true });
    }
  }}
  onclose={onClose}
  oncancel={(event) => { if (busy) event.preventDefault(); }}
  onclick={(event) => { if (event.target === dialogElement) close(); }}
>
  <form onsubmit={(event) => { event.preventDefault(); void start(); }}>
    <header class="surface-head">
      <div class="head-name">
        <h2 class="head-title" id={`${uid}-title`}>New session</h2>
        <p class="head-subtitle" id={`${uid}-subtitle`}>Browse the agents you can run and start one.</p>
      </div>
      {#if onRefresh}
        <button
          type="button"
          class="head-button"
          aria-label="Re-check availability"
          data-tip="Re-check availability"
          data-tip-below
          data-tip-align="end"
          disabled={refreshing || busy}
          onclick={() => void onRefresh?.()}
        ><RefreshCw size={13} aria-hidden="true" /></button>
      {/if}
      <button
        type="button"
        class="head-button"
        aria-label="Close"
        data-tip="Close"
        data-tip-below
        data-tip-align="end"
        disabled={busy}
        onclick={close}
      ><X size={14} aria-hidden="true" /></button>
    </header>

    <div class="toolbar">
      <div class="search-row">
        <div class="field">
          <Search size={14} aria-hidden="true" />
          <input
            bind:this={searchInput}
            bind:value={query}
            placeholder="Search agents"
            aria-label="Search agents"
            autocomplete="off"
            spellcheck="false"
            role="combobox"
            aria-autocomplete="list"
            aria-controls={listId}
            aria-expanded="true"
            aria-activedescendant={selected ? optionId(selected) : undefined}
            use:inputClearDissolve
            onkeydown={(event) => handleKey(event, true)}
          />
          {#if query}
            <IconButton class="clear-search" label="Clear search" onclick={clearSearch}>
              <X size={13} aria-hidden="true" />
            </IconButton>
          {/if}
        </div>
        {#if agents.length > 0}
          <span class="kicker count" aria-live="polite">
            {refreshing ? "Checking…" : filtered.length < agents.length ? `${filtered.length} of ${agents.length}` : `${agents.length} ${agents.length === 1 ? "agent" : "agents"}`}
          </span>
        {/if}
      </div>
      {#if chips.length > 0}
        <div class="filters" role="group" aria-label="Filter agents">
          {#each chips as chip (chip.id)}
            <Chip size="xs" fill="quiet" active={active.includes(chip.id)} aria-pressed={active.includes(chip.id)} onclick={() => toggle(chip.id)}>
              {chip.label}<span class="chip-count">{chip.count}</span>
            </Chip>
          {/each}
          {#if active.length > 0}
            <Chip size="xs" fill="bare" onclick={() => (active = [])}>Clear</Chip>
          {/if}
        </div>
      {/if}
    </div>
    {#if error}<p class="form-error toolbar-error" role="alert">{error}</p>{/if}

    <div class="market">
      <div class="grid-area">
        {#if filtered.length === 0}
          <div class="grid-empty">
            {#if agents.length === 0}
              <EmptyState title={refreshing ? "Loading agents…" : "No agents are registered yet."} />
            {:else}
              <EmptyState title="No agents match">Try another word, or fewer filters.</EmptyState>
            {/if}
          </div>
        {/if}
        <!-- Mounted even when empty, so the virtualizer keeps one scroller, as the Session Manager's does.
             Each card is reached by the grid's own keydown (handleKey), so a click is all a card adds. -->
        <div
          class="agent-grid"
          id={listId}
          role="listbox"
          aria-label="Agents"
          tabindex="-1"
          hidden={filtered.length === 0}
          bind:this={listElement}
          {@attach windowedFocus()}
          onkeydown={(event) => handleKey(event, false)}
        >
          <div class="card-rows" style:height={`${$cardRows.getTotalSize()}px`} {@attach watchWidth}>
            {#each $cardRows.getVirtualItems() as row (row.key)}
              <div
                class="card-row"
                role="presentation"
                style:top={`${row.start}px`}
                style:height={`${CARD_HEIGHT}px`}
                style:grid-template-columns={`repeat(${columns}, minmax(0, 1fr))`}
                style:gap={`${CARD_GAP}px`}
              >
                {#each filtered.slice(row.index * columns, row.index * columns + columns) as entry, column (entry.agent.key)}
                  {@const position = row.index * columns + column}
                  {@const status = statusOf(entry.agent)}
                  {@const isSelected = entry.agent.key === selectedKey}
                  <!-- svelte-ignore a11y_click_events_have_key_events -->
                  <div
                    id={optionId(entry)}
                    class={["agent-card", isSelected && "selected"]}
                    role="option"
                    aria-selected={isSelected}
                    aria-setsize={filtered.length}
                    aria-posinset={position + 1}
                    aria-disabled={busy || undefined}
                    tabindex={isSelected ? 0 : -1}
                    data-index={position}
                    onclick={() => choose(entry)}
                    ondblclick={() => {
                      choose(entry);
                      void start();
                    }}
                  >
                    <span class="card-head">
                      <span class="agent-name fade-end" dir="auto">{entry.name}</span>
                      {#if status}<StatusChip plain label={status.label} kind={status.kind} />{/if}
                    </span>
                    <span class="agent-description" dir="auto">{entry.summary || entry.agent.workflow_type}</span>
                    {#if entry.tags.length > 0}
                      <span class="agent-tags">{#each entry.tags.slice(0, 3) as tag (tag)}<span class="tag">{tag}</span>{/each}</span>
                    {/if}
                  </div>
                {/each}
              </div>
            {/each}
          </div>
        </div>
      </div>

      <section class="detail" aria-labelledby={`${uid}-detail-name`}>
        <div class="detail-body">
          {#if agent && selected}
            {@const status = statusOf(agent)}
            <h3 class="detail-head">
              <span class="detail-name" id={`${uid}-detail-name`} dir="auto">{selected.name}</span>
              {#if status}<StatusChip plain label={status.label} kind={status.kind} />{/if}
            </h3>
            <code class="detail-type">{agent.workflow_type}</code>
            {#if selected.tags.length > 0}<p class="agent-tags">{#each selected.tags as tag (tag)}<span class="tag">{tag}</span>{/each}</p>{/if}
            {#if agent.description?.trim()}
              {#key agent.key}
                <div id={`${uid}-about`} class={["detail-description", expanded && "expanded"]} {@attach measureClamp}>
                  <MarkdownMessage text={agent.description} compact />
                </div>
              {/key}
              {#if clamped || expanded}
                <button type="button" class="disclosure more" aria-expanded={expanded} aria-controls={`${uid}-about`} onclick={() => (expandedKey = expanded ? null : agent.key)}>
                  {expanded ? "Less" : "More"}
                  <ChevronRight class="disclosure-chevron" size={12} aria-hidden="true" />
                </button>
              {/if}
            {/if}
            {#if agent.handlers?.length}
              <section class="detail-section" aria-labelledby={`${uid}-accepts`}>
                <h4 class="kicker" id={`${uid}-accepts`}>Accepts</h4>
                <ul class="handlers">
                  {#each agent.handlers as handler, i (i)}
                    <li>
                      <code class="handler-name" dir="auto">{handler.name}</code>
                      {#if handler.description}<span class="handler-description" dir="auto">{markdownToText(handler.description)}</span>{/if}
                    </li>
                  {/each}
                </ul>
              </section>
            {/if}
            {#if agent.states?.length}
              <section class="detail-section" aria-labelledby={`${uid}-states`}>
                <h4 class="kicker" id={`${uid}-states`}>Publishes state</h4>
                <p class="state-names">{#each agent.states as state, i (i)}<code dir="auto">{state}</code>{/each}</p>
              </section>
            {/if}
            <div aria-live="polite">
              {#if agentBlocked(agent)}
                <p class="callout-warning worker-hint">Start a worker on <Copyable value={agent.task_queue} label="Copy task queue name"><code>{agent.task_queue}</code></Copyable> to use this agent, then re-check availability.</p>
              {:else if agent.worker?.status !== "ready"}
                <p class="worker-hint">Worker availability could not be checked. You can still try starting this agent.</p>
              {/if}
            </div>
            {#if form}
              <section class="setup-fields" aria-labelledby={`${uid}-setup`}>
                <h4 id={`${uid}-setup`}>{form.title} <span>{form.required ? "Required" : "Optional"}</span></h4>
                {#if form.description}<div class="setup-description"><MarkdownMessage text={form.description} compact /></div>{/if}
                <SchemaForm fields={form.fields} bind:values={form.values} disabled={busy} idPrefix={`init-${agent.key}`} errors={problems} />
              </section>
            {/if}
          {:else}
            <p class="detail-none" id={`${uid}-detail-name`}>No agent selected.</p>
          {/if}
        </div>
        <footer class="detail-actions">
          {#if alert}<p class="form-error" role="alert">{alert}</p>{/if}
          <div class="actions">
            <Chip fill="quiet" disabled={busy} onclick={close}>Cancel</Chip>
            {#if form && !form.required}
              <Chip fill="quiet" disabled={busy || !agent || agentBlocked(agent)} onclick={() => void start(false)}>Start without data</Chip>
            {/if}
            <Chip
              type="submit"
              tone="accent"
              aria-label={selected ? `Start session with ${selected.name}` : undefined}
              disabled={busy || !agent || agentBlocked(agent)}
            >{busy ? "Starting…" : "Start session"}</Chip>
          </div>
        </footer>
      </section>
    </div>
  </form>
</dialog>

<style>
  /* A fixed size, so browsing, filtering and choosing never move the dialog. */
  .start-sheet {
    container: start-sheet / inline-size;
    width: min(1040px, calc(100% - var(--gutter) * 2));
    height: min(86dvh, 760px);
    box-sizing: border-box;
    max-height: none;
    padding: 0;
    border: 1px solid var(--border-strong);
    border-radius: 0;
    background: var(--surface-1);
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
    overflow: hidden;
  }

  /* Open only: a closed dialog given a display would show. */
  .start-sheet[open] {
    display: flex;
    flex-direction: column;
  }

  .start-sheet::backdrop {
    background: var(--overlay-scrim);
  }

  form {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }

  h2, h3, h4, p, ul {
    margin: 0;
  }

  .head-title {
    flex: none;
  }

  .toolbar {
    flex: none;
    display: grid;
    gap: var(--gap-sm);
    padding: var(--pane-inset);
    border-bottom: 1px solid var(--border);
  }

  .search-row {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
  }

  .search-row .field {
    flex: 1;
  }

  .count {
    flex: none;
    font-variant-numeric: tabular-nums;
    unicode-bidi: plaintext;
  }

  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-xs);
  }

  .chip-count {
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }

  .form-error {
    color: var(--error);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }

  .toolbar-error {
    flex: none;
    padding: var(--gap-sm) var(--pane-inset);
  }

  /* Narrow: the grid above the details, which take at most a bit over half. Wide: side by side. */
  .market {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .grid-area {
    flex: 1 1 0;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: min(9rem, 26dvh);
  }

  .grid-empty {
    padding: var(--pane-inset);
  }

  .agent-grid {
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .agent-grid:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  .card-rows {
    position: relative;
    margin: var(--pane-inset);
  }

  .card-row {
    position: absolute;
    inset-inline: 0;
    display: grid;
  }

  .agent-card {
    box-sizing: border-box;
    min-width: 0;
    display: grid;
    grid-template-rows: auto auto minmax(0, 1fr);
    gap: 4px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 0;
    background: var(--surface-2);
    cursor: pointer;
  }

  .agent-card.selected {
    background: var(--selected-bg);
  }

  .agent-card:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 1px;
  }

  .card-head {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    min-width: 0;
  }

  /* A plain status word draws no box, so it takes the name's line rather than a chip's height. */
  .card-head :global(.chip),
  .detail-head :global(.chip) {
    flex: none;
    height: auto;
  }

  .agent-name {
    flex: 1;
    min-width: 0;
    color: var(--text-1);
    font-size: var(--font-md);
    font-weight: 550;
    line-height: 1.4;
    unicode-bidi: plaintext;
  }

  .agent-description {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.4;
    overflow-wrap: anywhere;
    unicode-bidi: plaintext;
  }

  /* Labels, not controls: no box, the chrome label's type in a quiet tone, set apart by space
     rather than a separator that would start a wrapped line. A tag is never cut in half; on a
     narrow card the next one wraps, and a third line is cut whole. */
  .agent-tags {
    align-self: end;
    display: flex;
    flex-wrap: wrap;
    gap: 0 1.5ch;
    min-width: 0;
    max-height: 2.6em;
    overflow: hidden;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--label-size);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    line-height: 1.3;
    text-transform: uppercase;
  }

  .tag {
    white-space: nowrap;
    unicode-bidi: plaintext;
  }

  .detail {
    flex: 0 1 auto;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    max-height: 58%;
    border-top: 1px solid var(--border);
  }

  /* Rows at their content's height: the clamped description hides its overflow, which would
     otherwise let its row collapse to nothing when the panel is short, and the panel scrolls. */
  .detail-body {
    flex: 1 1 auto;
    display: grid;
    grid-auto-rows: max-content;
    align-content: start;
    gap: var(--gap-sm);
    min-height: 0;
    overflow-y: auto;
    padding: var(--pane-inset);
    overflow-wrap: anywhere;
  }

  .detail-head {
    display: flex;
    align-items: baseline;
    gap: var(--gap-sm);
    min-width: 0;
    font-size: var(--font-lg);
    font-weight: 600;
  }

  .detail-name {
    flex: 1;
    min-width: 0;
    unicode-bidi: plaintext;
  }

  .detail-type {
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-xs);
  }

  .detail-description {
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.5;
  }

  /* Four of compact markdown's lines; line-clamp does not reach through its nested blocks. */
  .detail-description:not(.expanded) {
    max-height: calc(4 * 1.45em);
    overflow: hidden;
  }

  .more {
    justify-self: start;
    display: inline-flex;
    align-items: center;
    gap: var(--gap-xs);
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-2);
    font: inherit;
    font-size: var(--font-sm);
  }

  .more:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .detail-section {
    display: grid;
    gap: var(--gap-xs);
    margin-top: var(--gap-sm);
  }

  .handlers {
    display: grid;
    gap: var(--gap-sm);
    padding: 0;
    list-style: none;
  }

  .handlers li {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .handler-name, .state-names code {
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--font-xs);
    overflow-wrap: anywhere;
  }

  .handler-description {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.4;
  }

  .state-names {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  .detail-none {
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .worker-hint {
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.5;
  }

  .worker-hint code {
    font-family: var(--font-mono);
    font-size: inherit;
  }

  .worker-hint :global(.copy) {
    opacity: 1;
  }

  .setup-fields {
    margin-top: var(--gap-sm);
    padding-top: var(--pane-inset);
    border-top: 1px solid var(--border);
  }

  .setup-fields h4 {
    font-size: var(--font-md);
    font-weight: 550;
  }

  .setup-fields h4, .setup-description {
    margin-bottom: var(--gutter-tight);
  }

  .setup-fields h4 span {
    margin-inline-start: var(--gap-sm);
    color: var(--text-3);
    font-size: var(--font-2xs);
    font-weight: 400;
  }

  .setup-description {
    color: var(--text-2);
    font-size: var(--font-sm);
  }

  .detail-actions {
    flex: none;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    padding: var(--gap-sm) var(--pane-inset);
    border-top: 1px solid var(--border);
    background: var(--surface-2);
  }

  .detail-actions .form-error {
    flex: 1 1 12rem;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--gap-sm);
    margin-inline-start: auto;
  }

  @container start-sheet (min-width: 760px) {
    .market {
      flex-direction: row;
    }

    .grid-area {
      min-height: 0;
    }

    .detail {
      flex: 0 0 clamp(280px, 34%, 360px);
      max-height: none;
      border-top: 0;
      border-inline-start: 1px solid var(--border);
    }
  }

  /* One scrolling row on a narrow dialog, so the filters do not take the grid's room. */
  @container start-sheet (max-width: 560px) {
    .filters {
      flex-wrap: nowrap;
      overflow-x: auto;
      overflow-y: hidden;
    }

    .filters :global(.chip) {
      flex: none;
    }
  }

  @media (hover: hover) and (pointer: fine) {
    .agent-card:hover {
      border-color: var(--border-strong);
    }

    .more:hover {
      color: var(--text-1);
    }
  }
</style>
