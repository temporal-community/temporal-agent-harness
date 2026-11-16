<script lang="ts" module>
  import type { MockListing } from "$lib/mock/catalog";

  /** One row: a fixture, or one of its variants (`data`, "" for the first). */
  export interface ScenarioRow {
    entry: MockListing;
    group: string;
    data?: string;
    label: string;
  }

  export const LIVE: MockListing = { name: "", label: "Live API", group: "Live" };
  export const RECENT = "Recent";

  /**
   * What the list shows. Narrowed by the search (a fixture's label, group, name or a variant's
   * label) and the group; with neither, the recent fixtures head it. A variant row shows under
   * its fixture while that fixture is loaded, or while the search matches the variant.
   */
  export function scenarioSections(
    catalog: MockListing[],
    { current, recent, query, group }: { current: string; recent: string[]; query: string; group: string }
  ): { group: string; rows: ScenarioRow[] }[] {
    const term = query.trim().toLowerCase();
    const has = (text: string) => text.toLowerCase().includes(term);
    const variantRows = (entry: MockListing, section: string): ScenarioRow[] =>
      Object.entries(entry.variants ?? {})
        .map(([key, label], i) => ({ entry, group: section, data: i === 0 ? "" : key, label }))
        .filter((row) => (term ? has(row.label) : entry.name === current));
    const rowsOf = (entry: MockListing, section: string): ScenarioRow[] => [{ entry, group: section, label: entry.label }, ...variantRows(entry, section)];
    const all = [LIVE, ...catalog];
    if (!term && !group) {
      const recentEntries = recent.map((name) => all.find((entry) => entry.name === name)).filter((entry) => entry !== undefined);
      const listed = [...new Set(all.map((entry) => entry.group))].map((name) => ({
        group: name,
        rows: all.filter((entry) => entry.group === name).map((entry) => ({ entry, group: name, label: entry.label }))
      }));
      return recentEntries.length ? [{ group: RECENT, rows: recentEntries.flatMap((entry) => rowsOf(entry, RECENT)) }, ...listed] : listed;
    }
    const hits = all.filter(
      (entry) => (!group || entry.group === group) && (!term || has(`${entry.label} ${entry.group} ${entry.name}`) || Object.values(entry.variants ?? {}).some(has))
    );
    return [...new Set(hits.map((entry) => entry.group))].map((name) => ({ group: name, rows: hits.filter((entry) => entry.group === name).flatMap((entry) => rowsOf(entry, name)) }));
  }
</script>

<script lang="ts">
  /**
   * Dev only: the dev tools' list of fixtures. Type to narrow, or choose a group beside the field;
   * arrow keys and Enter drive the list. With nothing typed and no group chosen, the last few
   * loaded head it as Recent. A fixture's variants are rows under it, so a variant is picked where
   * its fixture is.
   */
  import { Search } from "@lucide/svelte";
  import Select from "$lib/components/primitives/Select.svelte";

  interface Props {
    catalog: MockListing[];
    /** The fixture loaded, "" for the live API. */
    current: string;
    /** Its variant, "" for its first. */
    variant: string;
    recent: string[];
    onchoose: (name: string, data: string) => void;
  }

  let { catalog, current, variant, recent, onchoose }: Props = $props();

  let query = $state("");
  let group = $state("");
  let active = $state(0);

  const groups = $derived([...new Set(catalog.map((entry) => entry.group))]);
  const sections = $derived(scenarioSections(catalog, { current, recent, query, group }));
  const rows = $derived(sections.flatMap((section) => section.rows));

  const idOf = (row: ScenarioRow) => `dev-tools-row-${row.group}-${row.entry.name || "live"}${row.data === undefined ? "" : `-${row.data || "first"}`}`.replace(/\W+/g, "-");
  const isLoaded = (row: ScenarioRow) => row.entry.name === current && (row.data === undefined ? !row.entry.variants : row.data === variant);
  const choose = (row: ScenarioRow) => onchoose(row.entry.name, row.data ?? "");

  function keydown(event: KeyboardEvent): void {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!rows.length) return;
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
      document.getElementById(idOf(rows[active]!))?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && rows[active]) {
      event.preventDefault();
      choose(rows[active]);
    } else if (event.key === "Escape" && query) {
      /* The first Escape clears the search; the next one is the panel's, and closes it. */
      event.preventDefault();
      event.stopPropagation();
      query = "";
    }
  }
</script>

<div class="scenario-picker">
  <div class="scenario-bar">
    <label class="field">
      <Search size={14} aria-hidden="true" />
      <input
        placeholder={`Search ${catalog.length} scenarios`}
        aria-label="Search scenarios"
        role="combobox"
        aria-expanded="true"
        aria-controls="dev-tools-scenarios"
        aria-activedescendant={rows[active] ? idOf(rows[active]) : undefined}
        bind:value={query}
        oninput={() => (active = 0)}
        onkeydown={keydown}
      />
    </label>
    <Select
      class="scenario-group"
      aria-label="Group"
      bind:value={group}
      options={[{ value: "", label: "All groups" }, ...groups.map((name) => ({ value: name, label: name }))]}
      onchange={() => (active = 0)}
    />
  </div>
  <div class="scenario-list" id="dev-tools-scenarios" role="listbox" aria-label="Scenarios">
    {#each sections as section (section.group)}
      {#if !group || section.group === RECENT}<div class="kicker scenario-heading" role="presentation">{section.group}</div>{/if}
      {#each section.rows as row (idOf(row))}
        {@const index = rows.indexOf(row)}
        <!-- Reached from the search field, which owns the keys, as the console's other comboboxes are. -->
        <div
          id={idOf(row)}
          class={["scenario-row", row.data !== undefined && "variant", index === active && "active", isLoaded(row) && "loaded"]}
          role="option"
          aria-selected={isLoaded(row)}
          tabindex="-1"
          onclick={() => choose(row)}
          onkeydown={(event) => event.key === "Enter" && choose(row)}
          onmousemove={() => (active = index)}
        >
          <span>{row.label}</span>
          {#if row.data === undefined && row.entry.name}<code>{row.entry.name}</code>{:else if row.data}<code>data={row.data}</code>{/if}
        </div>
      {/each}
    {:else}
      <p class="scenario-none">No scenario matches{query.trim() ? ` “${query.trim()}”` : ""}{group ? ` in ${group}` : ""}.</p>
    {/each}
  </div>
</div>

<style>
  .scenario-picker {
    display: grid;
    gap: var(--gap-md);
  }

  .scenario-bar {
    display: flex;
    gap: var(--gap-sm);
  }

  .scenario-bar > .field {
    flex: 1;
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .scenario-bar > .field input {
    flex: 1;
    min-width: 0;
  }

  .scenario-bar :global(.scenario-group) {
    flex: 0 0 9.5rem;
  }

  .scenario-list {
    max-height: 320px;
    overflow: auto;
    border: 1px solid var(--border);
  }

  .scenario-heading {
    padding: var(--gap-sm) var(--gap-md) var(--gap-xs);
    color: var(--text-4);
  }

  .scenario-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--gap-md);
    min-height: var(--control-height);
    padding: var(--gap-xs) var(--gap-md);
    color: var(--text-2);
    font-size: var(--font-md);
    cursor: pointer;
  }

  /* A variant sits under its fixture on a rule, as a tree's children do, so it reads as part of it. */
  .scenario-row.variant {
    margin-inline-start: calc(var(--gap-md) + 3px);
    border-inline-start: 1px solid var(--border);
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .scenario-row code {
    flex: none;
    color: var(--text-4);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
  }

  .scenario-row span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .scenario-row.active {
    background: var(--control-hover);
    color: var(--text-1);
  }

  .scenario-row.loaded span::before {
    content: "● ";
    color: var(--dev-accent, var(--success));
  }

  .scenario-none {
    margin: 0;
    padding: var(--gap-md);
    color: var(--text-3);
    font-size: var(--font-md);
  }
</style>
