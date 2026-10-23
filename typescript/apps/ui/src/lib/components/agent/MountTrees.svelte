<script lang="ts" module>
  import type { FileNode, MountView } from "$lib/state/fileMounts";

  /**
   * Where a row sits in the tree, said explicitly: rows outside the window aren't in the DOM,
   * so the browser can't count them. `parent` is the parent row's index, -1 for a mount.
   */
  interface Place {
    key: string;
    open: boolean;
    expandable: boolean;
    level: number;
    setsize: number;
    posinset: number;
    parent: number;
  }

  type Row = Place & ({ kind: "mount"; mount: MountView } | { kind: "node"; mount: MountView; node: FileNode; depth: number });

  export const nodeKey = (mountKey: string, path: string) => `${mountKey}\u0000${path}`;

  /** Every mount's row, then the rows of its tree that are not inside a collapsed folder or mount. */
  export function treeRows(mounts: MountView[], collapsed: ReadonlySet<string>): Row[] {
    const out: Row[] = [];
    mounts.forEach((mount, m) => {
      const mountIndex = out.length;
      const mountOpen = !collapsed.has(mount.key);
      const expandable = mount.root.children.length > 0;
      out.push({
        kind: "mount",
        key: mount.key,
        mount,
        open: mountOpen,
        expandable,
        level: 1,
        setsize: mounts.length,
        posinset: m + 1,
        parent: -1
      });
      if (!mountOpen) return;
      const walk = (nodes: FileNode[], depth: number, parent: number) => {
        nodes.forEach((node, n) => {
          const key = nodeKey(mount.key, node.path);
          const open = !collapsed.has(key);
          const index = out.length;
          const isDir = node.isDir === true && node.children.length > 0;
          out.push({
            kind: "node",
            key,
            mount,
            node,
            depth,
            open,
            expandable: isDir,
            level: depth + 2,
            setsize: nodes.length,
            posinset: n + 1,
            parent
          });
          if (isDir && open) walk(node.children, depth + 1, index);
        });
      };
      walk(mount.root.children, 0, mountIndex);
    });
    return out;
  }

  /**
   * Where a key moves focus in the tree from row `from`, as windowedFocus takes it: down and up
   * a row, Home and End, Right into an open row's first child, Left out to the parent. Opening
   * and closing a row with Right and Left is the row's own handler, before this runs.
   */
  export function treeKey(rows: readonly Row[], from: number, key: string): number | null | undefined {
    const row = rows[from];
    switch (key) {
      case "ArrowDown":
      case "PageDown":
        return from + 1;
      case "ArrowUp":
      case "PageUp":
        return from - 1;
      case "Home":
        return 0;
      case "End":
        return rows.length - 1;
      case "ArrowRight":
        return row?.expandable && row.open ? from + 1 : null;
      case "ArrowLeft":
        return row && row.parent >= 0 ? row.parent : null;
      default:
        return undefined;
    }
  }

  /**
   * A name split where it gives way: the stem ellipsizes and the tail (a file's extension, a
   * path's last segment) stays, so a long name keeps both of the ends that tell it apart.
   */
  export function nameParts(name: string, separator: "." | "/"): [string, string] {
    const at = name.lastIndexOf(separator);
    if (at <= 0 || name.length - at > 24) return [name, ""];
    return [name.slice(0, at), name.slice(at)];
  }
</script>

<script lang="ts">
  import {
    ChevronRight,
    File as FileIcon,
    FilePen,
    FilePlus,
    FileX,
    Folder,
    FolderDot,
    FolderPen,
    FolderPlus,
    FolderX,
    HardDrive,
    Lock,
    MemoryStick,
    Waypoints,
    type IconProps
  } from "@lucide/svelte";
  import { untrack, type Component } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import { SvelteSet } from "svelte/reactivity";
  import { markdownToText } from "$lib/components/chat/MarkdownMessage.svelte";
  import { NEST_CAP } from "$lib/components/primitives/nest";
  import TipCard from "$lib/components/primitives/TipCard.svelte";
  import WindowedRows from "$lib/components/primitives/WindowedRows.svelte";
  import { windowedFocus } from "$lib/components/primitives/windowedFocus";
  import { formatSize, type FileChange } from "$lib/state/fileMounts";

  /**
   * Every tracked mount as a collapsible tree, with the open file marked. Used by the files
   * pane and by the file viewer beside the file it shows, so a click in either opens a file.
   * A mount is the top row of its own tree; what it is and who owns it is in that row's tip.
   */
  interface Props {
    mounts: MountView[];
    selected: { mountKey: string; path: string } | null;
    onOpen: (mountKey: string, path: string) => void;
    /** Opens an OKF bundle mount's graph. Without it, no Graph button is shown. */
    onGraph?: (mountKey: string) => void;
    /** The mount whose graph is open, so its Graph button reads as pressed. */
    graphKey?: string | null;
  }

  let { mounts, selected, onOpen, onGraph, graphKey = null }: Props = $props();

  const uid = $props.id();

  let collapsed = $state<Set<string>>(new Set());
  let scroller = $state<HTMLElement | null>(null);
  let rowList = $state<{ scrollToIndex(index: number): void } | null>(null);

  /* A mount of thousands of files drew every one of them; only the rows in view are drawn. */
  const rows = $derived(treeRows(mounts, collapsed));

  const treeFocus = windowedFocus(
    (index) => rowList?.scrollToIndex(index),
    () => rows.length,
    (key, from) => treeKey(rows, from, key)
  );

  /* One Tab stop: the row last focused, else the open file's, else the first. The rest are
     reached with the arrows. */
  let activeKey = $state<string | null>(null);
  const selectedKey = $derived(selected ? nodeKey(selected.mountKey, selected.path) : null);
  const activeIndex = $derived(Math.max(0, rows.findIndex((row) => row.key === (activeKey ?? selectedKey))));
  /* Opens scrolled to the open file, so the file viewer's tree starts where the file is. A
     frame after the first rows are drawn: in a dialog that is still opening, the window
     won't scroll sooner. */
  let scrolledToOpen = false;
  $effect(() => {
    if (scrolledToOpen || drawn.size === 0) return;
    scrolledToOpen = true;
    const index = untrack(() => (selectedKey ? activeIndex : null));
    if (index !== null) requestAnimationFrame(() => rowList?.scrollToIndex(index));
  });
  /* Which rows the window has drawn. When the Tab stop isn't one of them, the tree itself is,
     and hands focus on to that row. */
  const drawn = new SvelteSet<number>();
  const trackDrawn =
    (index: number): Attachment =>
    () => {
      drawn.add(index);
      return () => drawn.delete(index);
    };

  function focusRow(index: number, tries = 10): void {
    const row = scroller?.querySelector<HTMLElement>(`[data-index="${index}"]`);
    if (row) row.focus();
    else if (tries > 0) requestAnimationFrame(() => focusRow(index, tries - 1));
  }

  /* Tabbed into from outside, not handed focus back by windowedFocus when a row scrolled away. */
  function enterTree(event: FocusEvent): void {
    const from = event.relatedTarget as Node | null;
    if (event.target !== scroller || !from || scroller?.contains(from)) return;
    rowList?.scrollToIndex(activeIndex);
    focusRow(activeIndex);
  }

  function toggle(key: string): void {
    const next = new Set(collapsed);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    collapsed = next;
  }

  function pick(row: Row): void {
    if (row.kind === "node" && row.node.isDir !== true) onOpen(row.mount.key, row.node.path);
    else if (row.expandable) toggle(row.key);
  }

  /** What every row is to the tree: its place, its Tab stop, and its keys. */
  function treeItem(row: Row, index: number) {
    return {
      type: "button" as const,
      "aria-level": row.level,
      "aria-setsize": row.setsize,
      "aria-posinset": row.posinset,
      "aria-expanded": row.expandable ? row.open : undefined,
      tabindex: index === activeIndex ? 0 : -1,
      "data-index": index,
      onfocus: () => (activeKey = row.key),
      /* Not `onkeydown`: Svelte delegates that to the root, after windowedFocus has moved focus. */
      onkeydowncapture: (event: KeyboardEvent) => rowKey(event, row),
      onclick: () => pick(row)
    };
  }

  /* Right opens a closed row and Left closes an open one; otherwise the keys move focus. */
  function rowKey(event: KeyboardEvent, row: Row): void {
    const opens = event.key === "ArrowRight" && !row.open;
    const closes = event.key === "ArrowLeft" && row.open;
    if (!row.expandable || !(opens || closes)) return;
    event.preventDefault();
    event.stopPropagation();
    toggle(row.key);
  }

  const CHANGE_WORD: Record<FileChange, string> = { added: "Added", modified: "Changed", removed: "Removed" };

  /** The shape says how the commit at the cursor touched a path, so status isn't color alone. */
  function glyphFor(node: FileNode, isDir: boolean, open: boolean): Component<IconProps> {
    switch (node.change) {
      case "added":
        return isDir ? FolderPlus : FilePlus;
      case "modified":
        return isDir ? FolderPen : FilePen;
      case "removed":
        return isDir ? FolderX : FileX;
      case null:
        /* One cue at the highest closed folder, not a mark on every ancestor. */
        if (isDir) return node.changedBelow && !open ? FolderDot : Folder;
        return FileIcon;
      default: {
        const unhandled: never = node.change;
        return unhandled;
      }
    }
  }

  const kindName = (mount: MountView) => (mount.kind === "index" ? "indexed" : "in memory");

  function mountLabel(mount: MountView): string {
    const files = `${mount.fileCount.toLocaleString("en-US")} ${mount.fileCount === 1 ? "file" : "files"}`;
    return [mount.mount, kindName(mount), mount.readOnly && "read-only", files].filter(Boolean).join(", ");
  }
</script>

<div
  class="mounts"
  role="tree"
  aria-label="Mounts"
  tabindex={drawn.has(activeIndex) ? -1 : 0}
  bind:this={scroller}
  {@attach treeFocus}
  onfocus={enterTree}
>
  <WindowedRows items={rows} getKey={(row) => row.key} {scroller} start={0} estimateSize={20} bind:this={rowList}>
    {#snippet children(row, index)}
      {#if row.kind === "mount"}
        {@const mount = row.mount}
        {@const hasTree = mount.root.children.length > 0}
        {@const [head, tail] = nameParts(mount.mount, "/")}
        <div class={["mount", index > 0 && "later"]} data-mount={mount.mount}>
          <button
            role="treeitem"
            {...treeItem(row, index)}
            {@attach trackDrawn(index)}
            class={["row mount-row", hasTree && "disclosure"]}
            aria-label={mountLabel(mount)}
            aria-describedby={`${uid}-tip-${index}`}
            data-tip=""
            data-tip-align="start"
          >
            <span class="trace-glyph">
              {#if mount.kind === "index"}<HardDrive size={13} aria-hidden="true" />{:else}<MemoryStick
                  size={13}
                  aria-hidden="true"
                />{/if}
              {#if hasTree}<ChevronRight class="disclosure-chevron" size={13} aria-hidden="true" />{/if}
            </span>
            <span class="name"><span class="stem" dir="auto">{head}</span><span class="tail">{tail}</span></span>
            {#if mount.readOnly}<Lock class="lock" size={12} aria-hidden="true" />{/if}
            <span class="size">{mount.fileCount.toLocaleString("en-US")}</span>
            <TipCard
              id={`${uid}-tip-${index}`}
              heading={mount.mount}
              fields={[
                {
                  key: "Kind",
                  value:
                    mount.kind === "index"
                      ? "Indexed: only the paths this session's scripts looked up, listed, read or " +
                        "wrote, not everything in the store. Opened files are read from the store as they are now"
                      : "In memory: contents are session state, shown as of the cursor"
                },
                { key: "Access", value: mount.readOnly ? "Read-only" : "Read and write" },
                { key: "Owner", value: mount.role === "subagent" ? mount.label : null },
                {
                  key: "Files",
                  value: hasTree
                    ? null
                    : mount.kind === "index"
                      ? "None touched yet"
                      : "None yet"
                }
              ]}
              body={markdownToText(mount.description)}
            />
          </button>
          {#if mount.okfVersion && onGraph}
            <button
              type="button"
              class={["head-button", graphKey === mount.key && "on"]}
              aria-label={`Open ${mount.mount} graph`}
              data-tip="The whole OKF bundle as a graph, read from its store now"
              data-tip-align="end"
              onclick={() => onGraph(mount.key)}
            >
              <Waypoints size={14} aria-hidden="true" />
            </button>
          {/if}
          {#if mount.problem}<p class="callout-warning">{mount.problem}</p>{/if}
        </div>
      {:else}
        {@const { mount, node, depth, open } = row}
        {@const isSelected = selected?.mountKey === mount.key && selected.path === node.path}
        {@const isDir = node.isDir === true}
        {@const [stem, tail] = nameParts(node.name, isDir ? "/" : ".")}
        {@const Glyph = glyphFor(node, isDir, open)}
        <button
          role="treeitem"
          {...treeItem(row, index)}
          {@attach trackDrawn(index)}
          class={["row node", row.expandable && "disclosure", node.change, isSelected && "selected"]}
          style={`--depth: ${Math.min(depth, NEST_CAP)}`}
          aria-selected={isSelected ? "true" : undefined}
          aria-current={isSelected ? "true" : undefined}
          data-path={node.path}
          data-tip={node.change ? `${CHANGE_WORD[node.change]} by this mount's latest commit at the cursor` : undefined}
          data-tip-align={node.change ? "start" : undefined}
        >
          {#if depth > NEST_CAP}<span class="nest-depth">{depth}<span class="visually-hidden"> levels deep</span></span>{/if}
          <span class="trace-glyph">
            <Glyph size={13} aria-hidden="true" />
            {#if row.expandable}<ChevronRight class="disclosure-chevron" size={13} aria-hidden="true" />{/if}
          </span>
          <span class="name"><span class="stem" dir="auto">{stem}</span><span class="tail">{tail}</span></span>
          {#if node.change}<span class="visually-hidden">, {node.change}</span>{/if}
          {#if !isDir}<span class="size">{formatSize(node.size)}</span>{/if}
        </button>
      {/if}
    {/snippet}
  </WindowedRows>
</div>

<style>
  /* One minmax(0, 1fr) track, or the implicit auto track grows to the longest
     nowrap file name and the whole tree scrolls sideways. */
  .mounts {
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-content: start;
    min-width: 0;
    height: 100%;
    overflow: auto;
    scrollbar-gutter: stable;
  }

  /* The mount's row, then its Graph action, then a problem under both. */
  .mount {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    column-gap: var(--gap-2xs);
    min-width: 0;
  }

  .mount.later {
    padding-block-start: var(--gap-md);
  }

  .mount > .callout-warning {
    grid-column: 1 / -1;
    margin-block: var(--gap-2xs);
  }

  /* One height for every row, so a tall glyph (an emoji) can't break the rhythm and the
     window's estimate is exact. */
  .row {
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
    width: 100%;
    min-width: 0;
    height: var(--icon-target);
    padding-block: 0;
    padding-inline: var(--gap-xs) var(--gap-sm);
    border: 0;
    background: transparent;
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    text-align: start;
    cursor: pointer;
  }

  /* A level in under the mount's row, then one per folder. */
  .row.node {
    padding-inline-start: calc(var(--gap-xs) + (var(--depth) + 1) * 14px);
  }

  .mount-row {
    color: var(--text-1);
    font-size: var(--figure-size);
    font-weight: 600;
  }

  .mount-row:not(.disclosure) {
    cursor: default;
  }

  /* A long name never squeezes a glyph out of its cell. */
  .row :global(svg) {
    flex: none;
  }

  .row :global(.lock) {
    color: var(--text-3);
  }

  /* Inside the ring's own box: the tree is a scroller, and a ring drawn outward
     loses its sides to it. */
  .row:focus-visible {
    outline-offset: -2px;
  }

  .row.selected {
    background: var(--selected-bg);
    color: var(--text-1);
  }

  /* The tokens the replay position takes for its status. The glyph's shape and the row's
     accessible name say the same, so status is not color alone. */
  .row.added {
    color: var(--success);
  }

  .row.modified {
    color: var(--warning);
  }

  .row.removed {
    color: var(--error);
  }

  .row.removed .name {
    text-decoration: line-through;
  }

  @media (hover: hover) and (pointer: fine) {
    .row:hover {
      background: var(--control-hover);
    }
  }

  .name {
    flex: 1 1 auto;
    display: flex;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    line-height: var(--icon-target);
  }

  .stem {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tail {
    flex: none;
  }

  .size {
    flex: none;
    color: var(--text-4);
    font-size: var(--font-2xs);
    font-weight: 400;
    font-variant-numeric: tabular-nums;
  }
</style>
