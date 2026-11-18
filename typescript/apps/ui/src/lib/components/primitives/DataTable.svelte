<script lang="ts" module>
  import type { Component, Snippet } from "svelte";
  import type { Atom } from "@tanstack/svelte-store";
  import {
    columnFacetingFeature,
    columnFilteringFeature,
    columnOrderingFeature,
    columnResizingFeature,
    columnSizingFeature,
    columnVisibilityFeature,
    createFacetedRowModel,
    createFacetedUniqueValues,
    createFilteredRowModel,
    createSortedRowModel,
    rowSortingFeature,
    sortFn_alphanumeric,
    sortFn_text,
    tableFeatures,
    type ColumnSizingState,
    type RowData
  } from "@tanstack/svelte-table";

  export interface DataColumn<TRow extends RowData> {
    id: string;
    header: string;
    /** What the column sorts and filters by: text sorts naturally ("agent 2" before "agent 10"),
     *  numbers start descending. */
    value: (row: TRow) => string | number;
    cell: Snippet<[TRow]>;
    align?: "end";
    /** On the column's header and cells, so the caller's styles can reach them. */
    class?: string;
    sortable?: boolean;
    /** What the column means, on hover over its header. */
    tip?: string;
    /** A filter on the column's own values, chosen from a checklist with their counts. */
    filter?: boolean;
    /** The column's width in pixels, its content's and not the gap before it. A sized column can
     *  be resized between `minSize` and `maxSize`; one without a size takes the width the others
     *  leave. */
    size?: number;
    minSize?: number;
    maxSize?: number;
    /** Hidden while the table is narrower than this many pixels. */
    hideBelow?: number;
    /** The cell that names its row: a row header holding the row's button. */
    rowHeader?: boolean;
  }

  /** One thing a row's menu can do. */
  export interface RowAction {
    label: string;
    icon?: Component<{ size?: number; "aria-hidden"?: "true" }>;
    run: () => void;
  }

  /* The table's whole feature set, made once: sorting, a faceted filter per column, and the
     columns' order, visibility and sizes. Only the sorts "auto" resolves to for text are
     registered; numbers use the core's own. */
  const features = tableFeatures({
    rowSortingFeature,
    sortedRowModel: createSortedRowModel(),
    sortFns: { alphanumeric: sortFn_alphanumeric, text: sortFn_text },
    columnFilteringFeature,
    filteredRowModel: createFilteredRowModel(),
    columnFacetingFeature,
    facetedRowModel: createFacetedRowModel(),
    facetedUniqueValues: createFacetedUniqueValues(),
    columnOrderingFeature,
    columnVisibilityFeature,
    columnSizingFeature,
    columnResizingFeature
  });
</script>

<script lang="ts" generics="TRow extends RowData">
  /**
   * A table of rows a reader opens, drawn as a semantic table. TanStack Table owns the row model
   * and the sort and filter state in its own atoms; the caller owns each cell, and may own the
   * columns' order as an atom it keeps.
   */
  import { ArrowDown, ArrowUp, Ellipsis, ListFilter } from "@lucide/svelte";
  import { createTable } from "@tanstack/svelte-table";
  import { untrack } from "svelte";
  import { placeMenu } from "./placeMenu";

  interface Props {
    rows: TRow[];
    columns: DataColumn<TRow>[];
    rowKey: (row: TRow) => string;
    onRowClick: (row: TRow) => void;
    /** Anything else a row's button says about itself: its title, what it opens. */
    rowProps?: (row: TRow) => Record<string, unknown>;
    /** Names the table for assistive tech. */
    label: string;
    class?: string;
    /** A menu beside each row. */
    rowActions?: (row: TRow) => RowAction[];
    /** The columns' order, owned by the caller: reordering writes through it. Without one the
     *  columns keep the order they are given and can't be moved. */
    columnOrder?: Atom<string[]>;
    /** The columns' sizes, owned by the caller: resizing writes through it. Without one sized
     *  columns keep their size. */
    columnSizing?: Atom<ColumnSizingState>;
  }

  let {
    rows,
    columns,
    rowKey,
    onRowClick,
    rowProps,
    label,
    class: extraClass = "",
    rowActions,
    columnOrder,
    columnSizing
  }: Props = $props();

  const id = $props.id();
  let tableWidth = $state(0);

  const table = createTable({
    features,
    get columns() {
      return columns.map((column) => ({
        id: column.id,
        header: column.header,
        accessorFn: column.value,
        enableSorting: column.sortable ?? true,
        enableColumnFilter: column.filter ?? false,
        filterFn: (row: { getValue: (id: string) => unknown }, columnId: string, chosen: string[]) =>
          !chosen?.length || chosen.includes(String(row.getValue(columnId))),
        sortUndefined: "last" as const,
        ...(column.size == null ? { enableResizing: false } : { size: column.size, minSize: column.minSize ?? column.size, maxSize: column.maxSize ?? column.size * 3 })
      }));
    },
    get data() {
      return rows;
    },
    getRowId: (row: TRow) => rowKey(row),
    /* The slices the app owns. Read once: an atom is the slice's home, not a value to swap. */
    atoms: untrack(() => ({ ...(columnOrder && { columnOrder }), ...(columnSizing && { columnSizing }) })),
    /* Derived from the table's own width, not chosen: a narrow table drops what it can't fit. */
    state: {
      get columnVisibility() {
        return Object.fromEntries(columns.map((column) => [column.id, !(column.hideBelow && tableWidth > 0 && tableWidth < column.hideBelow)]));
      }
    },
    /* Shift-click a second header to sort by it too. */
    enableMultiSort: true,
    enableColumnResizing: untrack(() => Boolean(columnSizing)),
    columnResizeMode: "onChange"
  });

  const byId = $derived(new Map(columns.map((column) => [column.id, column])));
  const visibleColumns = $derived(table.getVisibleLeafColumns().map((column) => byId.get(column.id)!).filter(Boolean));
  const sortCount = $derived(table.atoms.sorting.get().length);
  const shown = $derived(table.getRowModel().rows);
  /* A sized column's width in the colgroup: its own, plus the gap before it after the first. */
  function colWidth(columnId: string, first: boolean): string | undefined {
    if (byId.get(columnId)?.size == null) return undefined;
    const px = `${table.getColumn(columnId)!.getSize()}px`;
    return first ? px : `calc(${px} + var(--gutter))`;
  }
  /* A resize handle moves from the keyboard too: a step at a time, or four with Shift. */
  function nudgeSize(event: KeyboardEvent, columnId: string): void {
    const step = event.key === "ArrowRight" ? 8 : event.key === "ArrowLeft" ? -8 : 0;
    if (!step) return;
    event.preventDefault();
    const column = table.getColumn(columnId)!;
    const def = byId.get(columnId)!;
    const size = Math.min(def.maxSize ?? def.size! * 3, Math.max(def.minSize ?? def.size!, column.getSize() + step * (event.shiftKey ? 4 : 1)));
    table.setColumnSizing((sizes) => ({ ...sizes, [columnId]: size }));
  }

  /* Dragging a header onto another moves its column there; Alt+arrows do the same. */
  const movable = Boolean(untrack(() => columnOrder));
  let dragging = $state<string | null>(null);
  let dropOn = $state<string | null>(null);
  /* Every column in its current place: the kept order, then any column it doesn't name yet. */
  function ordered(): string[] {
    const ids = columns.map((column) => column.id);
    const kept = table.atoms.columnOrder.get().filter((columnId) => ids.includes(columnId));
    return [...kept, ...ids.filter((columnId) => !kept.includes(columnId))];
  }
  function moveColumn(from: string, to: string): void {
    const order = ordered();
    if (from === to) return;
    const next = order.filter((item) => item !== from);
    next.splice(next.indexOf(to) + (order.indexOf(from) < order.indexOf(to) ? 1 : 0), 0, from);
    table.setColumnOrder(next);
  }
  function nudgeColumn(event: KeyboardEvent, columnId: string): void {
    if (!movable || !event.altKey || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) return;
    const ids = visibleColumns.map((column) => column.id);
    const neighbour = ids[ids.indexOf(columnId) + (event.key === "ArrowRight" ? 1 : -1)];
    if (!neighbour) return;
    event.preventDefault();
    moveColumn(columnId, neighbour);
  }
  /* A column lands after the header it is dropped on when it came from before it, else before. */
  function dropSide(columnId: string): "before" | "after" | null {
    if (dropOn !== columnId || !dragging || dragging === columnId) return null;
    const order = ordered();
    return order.indexOf(dragging) < order.indexOf(columnId) ? "after" : "before";
  }

  const ARIA_SORT = { asc: "ascending", desc: "descending" } as const;

  function filterValues(columnId: string): string[] {
    return (table.getColumn(columnId)?.getFilterValue() as string[] | undefined) ?? [];
  }

  function toggleFilter(columnId: string, value: string): void {
    const chosen = filterValues(columnId);
    const next = chosen.includes(value) ? chosen.filter((item) => item !== value) : [...chosen, value];
    table.getColumn(columnId)?.setFilterValue(next.length ? next : undefined);
  }

  let openMenu = $state<string | null>(null);

  /* A click anywhere on a row opens it, as its button does; one on a control inside it is that
     control's. */
  function rowClick(event: MouseEvent, row: TRow): void {
    if ((event.target as Element).closest("button, a, input, [popover]")) return;
    onRowClick(row);
  }
</script>

<!-- A row's menu, over the end of its last cell rather than in a column of its own, so the
     table's end keeps the pane's inset. An empty title: the row's, its description, would
     otherwise show over the menu's own tip. -->
{#snippet rowMenu(row: (typeof shown)[number])}
  {@const menuId = `${id}-menu-${row.id}`}
  <span class="row-actions" title="">
    <button type="button" class="head-button row-more" aria-label="More actions" data-tip="More actions" data-tip-align="end" aria-expanded={openMenu === row.id} popovertarget={menuId}>
      <Ellipsis size={14} aria-hidden="true" />
    </button>
    <div
      id={menuId}
      class="table-menu"
      popover="auto"
      ontoggle={(event) => {
        openMenu = event.newState === "open" ? row.id : openMenu === row.id ? null : openMenu;
        placeMenu(event, (event.currentTarget as HTMLElement).previousElementSibling as HTMLElement);
      }}
    >
      {#each rowActions?.(row.original) ?? [] as action (action.label)}
        {@const Icon = action.icon}
        <button
          type="button"
          onclick={(event) => {
            ((event.currentTarget as HTMLElement).closest("[popover]") as HTMLElement | null)?.hidePopover();
            action.run();
          }}
        >
          {#if Icon}<Icon size={12} aria-hidden="true" />{/if}<span>{action.label}</span>
        </button>
      {/each}
    </div>
  </span>
{/snippet}

{#snippet rowLine(row: (typeof shown)[number])}
  <tr class="trow" title={(rowProps?.(row.original).title as string | undefined) ?? undefined} onclick={(event) => rowClick(event, row.original)}>
    {#each visibleColumns as column, i (column.id)}
      {@const last = rowActions && i === visibleColumns.length - 1}
      {#if column.rowHeader}
        <th scope="row" class={["td", column.class, column.align === "end" && "end"]}>
          <button type="button" class="row-button" {...rowProps?.(row.original)} title={undefined} onclick={() => onRowClick(row.original)}>{@render column.cell(row.original)}</button>
          {#if last}{@render rowMenu(row)}{/if}
        </th>
      {:else}
        <td class={["td", column.class, column.align === "end" && "end"]}>{@render column.cell(row.original)}{#if last}{@render rowMenu(row)}{/if}</td>
      {/if}
    {/each}
  </tr>
{/snippet}

<div class={["data-table", extraClass]} bind:clientWidth={tableWidth}>
  <!-- Read when a header's button is focused; the header's own name stays its column's, as it is
       repeated with every cell under it. aria-sort says how it is sorted now. -->
  <span id={`${id}-sort-help`} class="visually-hidden">Sorts by this column{sortCount > 0 ? "; Shift-click to sort by it as well" : ""}{movable ? ". Alt and the arrow keys move the column" : ""}.</span>
  <div class="scroll">
    <table aria-label={label}>
      <colgroup>
        {#each visibleColumns as column, i (column.id)}
          <col style:width={colWidth(column.id, i === 0)} />
        {/each}
      </colgroup>
      <thead>
        <tr>
          {#each table.getHeaderGroups()[0]?.headers ?? [] as header (header.id)}
            {@const column = byId.get(header.id)!}
            {@const sorted = header.column.getIsSorted()}
            {@const chosen = column.filter ? filterValues(column.id) : []}
            {@const side = dropSide(column.id)}
            <th
              scope="col"
              aria-label={column.header}
              class={["th-cell", column.class, column.align === "end" && "end", dragging === column.id && "dragging", side && `drop-${side}`]}
              aria-sort={header.column.getCanSort() ? (sorted ? ARIA_SORT[sorted] : "none") : undefined}
              draggable={movable ? "true" : undefined}
              ondragstart={(event) => {
                dragging = column.id;
                event.dataTransfer?.setData("text/plain", column.id);
                if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
              }}
              ondragover={(event) => {
                if (!dragging) return;
                event.preventDefault();
                dropOn = column.id;
              }}
              ondragleave={() => dropOn === column.id && (dropOn = null)}
              ondrop={(event) => {
                event.preventDefault();
                if (dragging) moveColumn(dragging, column.id);
                dragging = dropOn = null;
              }}
              ondragend={() => (dragging = dropOn = null)}
            >
              <span class="th-inner">
                {#if header.column.getCanSort()}
                  <button
                    type="button"
                    class={["th", sorted && "sorted"]}
                    aria-describedby={`${id}-sort-help`}
                    data-tip={column.tip}
                    data-tip-below={column.tip ? true : undefined}
                    onclick={header.column.getToggleSortingHandler()}
                    onkeydown={(event) => nudgeColumn(event, column.id)}
                  >
                    {column.header}
                    {#if sorted && sortCount > 1}<span class="sort-index">{header.column.getSortIndex() + 1}</span>{/if}{#if sorted === "asc"}<ArrowUp size={10} aria-hidden="true" />{:else if sorted === "desc"}<ArrowDown size={10} aria-hidden="true" />{/if}
                  </button>
                {:else}
                  <span class="th">{column.header}</span>
                {/if}
                {#if column.filter}
                  {@const filterId = `${id}-filter-${column.id}`}
                  <button
                    type="button"
                    class={["filter-button", chosen.length && "on"]}
                    aria-label={chosen.length ? `Filter ${column.header}: ${chosen.join(", ")}` : `Filter ${column.header}`}
                    data-tip={chosen.length ? `Showing ${chosen.join(", ")}` : `Filter ${column.header}`}
                    data-tip-below
                    popovertarget={filterId}
                  >
                    <ListFilter size={11} aria-hidden="true" />
                  </button>
                  <div id={filterId} class="table-menu" popover="auto" ontoggle={(event) => placeMenu(event, (event.currentTarget as HTMLElement).previousElementSibling as HTMLElement)}>
                    {#each [...(header.column.getFacetedUniqueValues() as Map<string, number>)].sort(([a], [b]) => String(a).localeCompare(String(b))) as [value, count] (value)}
                      <label class="menu-check">
                        <input type="checkbox" checked={chosen.includes(String(value))} onchange={() => toggleFilter(column.id, String(value))} />
                        <span>{value}</span>
                        <span class="kicker menu-count">{count.toLocaleString()}</span>
                      </label>
                    {/each}
                    {#if chosen.length}
                      <button type="button" class="menu-clear" onclick={() => header.column.setFilterValue(undefined)}>Show all</button>
                    {/if}
                  </div>
                {/if}
              </span>
              {#if header.column.getCanResize()}
                {@const def = column}
                <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
                <div
                  class={["resize", header.column.getIsResizing() && "resizing"]}
                  role="separator"
                  aria-orientation="vertical"
                  aria-label={`Resize ${column.header}`}
                  aria-valuenow={header.column.getSize()}
                  aria-valuemin={def.minSize ?? def.size}
                  aria-valuemax={def.maxSize ?? (def.size ?? 0) * 3}
                  tabindex="0"
                  draggable="true"
                  data-tip={`Drag to resize ${column.header}\nDouble-click to reset`}
                  data-tip-below
                  onmousedown={header.getResizeHandler()}
                  ontouchstart={header.getResizeHandler()}
                  ondragstart={(event) => {
                    /* The nearest draggable to a press here, so the header's own drag never starts. */
                    event.preventDefault();
                    event.stopPropagation();
                  }}
                  ondblclick={() => header.column.resetSize()}
                  onkeydown={(event) => nudgeSize(event, column.id)}
                ><span class="resize-line"></span></div>
              {/if}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each shown as row (row.id)}
          {@render rowLine(row)}
        {/each}
      </tbody>
    </table>
    {#if shown.length === 0 && rows.length > 0}
      <p class="no-match" role="status">No {label.toLowerCase()} match.</p>
    {/if}
  </div>
</div>

<style>
  .data-table {
    min-height: 0;
    display: flex;
    flex: 1;
    flex-direction: column;
  }

  /* One scroller for the header and the rows, inset as every pane scroller is: the scrollbar's
     gutter is the end inset, so the table sits as far from either edge. */
  .scroll {
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
    scrollbar-gutter: stable;
    padding: 0 var(--pane-inset);
    padding-inline-end: calc(var(--pane-inset) - var(--scrollbar-size));
  }

  /* Fixed layout: the columns are the colgroup's, so a header and its cells can't drift apart.
     Separate borders, because collapsed ones don't travel with a sticky header. */
  table {
    width: 100%;
    table-layout: fixed;
    border-collapse: separate;
    border-spacing: 0;
    font-size: var(--font-md);
  }

  /* Each cell after the first draws the gap before it, so the colgroup's widths add it. */
  th,
  td {
    min-width: 0;
    padding: var(--gap-xs) 0;
    padding-inline-start: var(--gutter);
    overflow: hidden;
    white-space: nowrap;
    text-align: start;
    font-weight: inherit;
  }

  :is(th, td):first-child {
    padding-inline-start: 0;
  }

  .end {
    text-align: end;
  }

  thead th {
    position: sticky;
    top: 0;
    z-index: 1;
    padding-block: var(--gap-sm);
    overflow: visible;
    border-bottom: 1px solid var(--border);
    background: var(--surface-1);
    vertical-align: middle;
  }

  /* Cells of different faces and sizes meet on their text's baseline. */
  tbody td,
  tbody th {
    border-bottom: 1px solid var(--border);
    vertical-align: baseline;
  }

  .trow {
    cursor: pointer;
  }

  .trow:has(.row-button:focus-visible) {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  /* The row header's button is the row's keyboard stop: it looks like the rest of the cell. */
  .row-button {
    display: block;
    width: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: inherit;
    cursor: pointer;
  }

  .row-button:focus-visible {
    outline: none;
  }

  .th-inner {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: var(--gap-xs);
    max-width: 100%;
    vertical-align: middle;
  }

  /* The header is its own handle: a grab cursor over all of it, sorting included, and a tint
     behind its label on hover, inside the pane's inset when the column is first. */
  .th-cell[draggable="true"],
  .th-cell[draggable="true"] button.th {
    cursor: grab;
  }

  .th-cell[draggable="true"] .th-inner {
    isolation: isolate;
  }

  .th-cell[draggable="true"] .th-inner::before {
    content: "";
    position: absolute;
    z-index: -1;
    inset: calc(-1 * var(--gap-2xs)) calc(-1 * var(--gap-xs));
    background: transparent;
    transition: background-color var(--duration-fast) var(--ease-out);
  }

  .th-cell.dragging .th-inner {
    opacity: 0.4;
  }

  .th-cell.dragging .th-inner::before {
    background: var(--control-hover);
  }

  @media (prefers-reduced-motion: reduce) {
    .th-inner::before {
      transition: none;
    }
  }

  /* Where a dragged column will land: a line in the gap on that side. */
  .th-cell.drop-before::after,
  .th-cell.drop-after::after {
    content: "";
    position: absolute;
    inset-block: 0;
    width: 2px;
    border-radius: 1px;
    background: var(--accent);
  }

  .th-cell.drop-before::after {
    inset-inline-start: calc(var(--gutter) / 2 - 1px);
  }

  .th-cell.drop-before:first-child::after {
    inset-inline-start: -2px;
  }

  .th-cell.drop-after::after {
    inset-inline-end: calc(-1 * var(--gutter) / 2 - 1px);
  }

  /* A column's edge, grabbed to resize it, shown as a line when pointed at, reached, or held.
     Inside its own header: each sticky header paints over the one before it, so a handle that
     reached into the next would lose half its target. The line is an element, not a pseudo:
     a tip claims both of its owner's. */
  .resize {
    position: absolute;
    inset-block: 0;
    inset-inline-end: 0;
    width: 8px;
    cursor: col-resize;
    touch-action: none;
  }

  .resize-line {
    position: absolute;
    inset-block: var(--gap-xs);
    inset-inline-end: 0;
    width: 2px;
    background: transparent;
  }

  /* A header whose edge is in use draws over the next, whose label may hang across it. */
  thead th:has(.resize:is(:hover, :focus-visible, .resizing)) {
    z-index: 2;
  }

  .resize:focus-visible {
    outline: none;
  }

  .resize:focus-visible .resize-line,
  .resize.resizing .resize-line {
    background: var(--accent);
  }

  .sort-index {
    font-variant-numeric: tabular-nums;
    color: var(--accent-soft);
  }

  .th {
    min-width: 0;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--label-size);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
  }

  button.th {
    display: inline-flex;
    align-items: center;
    gap: var(--gap-2xs);
    padding: 0;
    border: 0;
    background: transparent;
    cursor: pointer;
  }

  .th.sorted {
    color: var(--text-1);
  }

  button.th:focus-visible,
  .filter-button:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .filter-button {
    display: inline-grid;
    place-items: center;
    padding: 1px;
    border: 1px solid transparent;
    background: transparent;
    color: var(--text-4);
    cursor: pointer;
  }

  .filter-button.on {
    border-color: var(--border-strong);
    color: var(--text-1);
  }

  /* A row's menu sits over the end of the row, as the Sessions sidebar's actions do, the text
     under it fading out. Placed against the row, not its cell, so the cell's clip can't cut it or
     its tip; its top is the cell's, so it keeps to the row's first line. */
  .trow {
    position: relative;
  }

  .row-actions {
    --actions-bg: var(--surface-1);
    --fade-width: 24px;
    position: absolute;
    inset-block: 0;
    inset-inline-end: 0;
    display: flex;
    align-items: flex-start;
    padding-block-start: inherit;
    padding-inline-start: var(--fade-width);
    pointer-events: none;
  }

  .trow:is(:hover, :focus-within) .row-actions,
  .row-actions:has([aria-expanded="true"]) {
    background: linear-gradient(to right, transparent, var(--actions-bg) var(--fade-width));
  }

  .trow:is(:hover, :focus-within) .row-actions:dir(rtl) {
    background: linear-gradient(to left, transparent, var(--actions-bg) var(--fade-width));
  }

  /* A row's menu waits until the row is pointed at or reached, then stays while it is open. */
  .row-more {
    opacity: 0;
    pointer-events: auto;
  }

  .trow:is(:hover, :focus-within) .row-more,
  .row-more[aria-expanded="true"] {
    opacity: 1;
  }

  .no-match {
    margin: 0;
    padding: var(--gutter) 0;
    color: var(--text-3);
    font-size: var(--font-md);
  }

  @media (hover: hover) and (pointer: fine) {
    button.th:hover {
      color: var(--text-1);
    }

    .th-cell[draggable="true"]:hover .th-inner::before {
      background: var(--control-hover);
    }

    .resize:hover .resize-line {
      background: var(--border-strong);
    }

    .filter-button:hover {
      color: var(--text-1);
    }

    .trow:hover > * {
      background: var(--control-hover);
    }

    .trow:hover .row-actions {
      --actions-bg: var(--control-hover);
    }
  }

  /* No hover to reveal it, so it is always shown. */
  @media (hover: none) {
    .row-actions {
      background: linear-gradient(to right, transparent, var(--actions-bg) var(--fade-width));
    }

    .row-more {
      opacity: 1;
    }
  }
</style>
