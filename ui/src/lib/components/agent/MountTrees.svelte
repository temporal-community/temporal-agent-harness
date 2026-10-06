<script lang="ts">
  import { ChevronDown, ChevronRight, File as FileIcon, Folder } from "@lucide/svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import { formatSize, type FileNode, type MountView } from "$lib/state/fileMounts";

  /**
   * Every tracked mount as a collapsible tree, with the open file marked. Used by the files
   * pane and by the file viewer beside the file it shows, so a click in either opens a file.
   */
  interface Props {
    mounts: MountView[];
    selected: { mountKey: string; path: string } | null;
    onOpen: (mountKey: string, path: string) => void;
  }

  let { mounts, selected, onOpen }: Props = $props();

  let collapsed = $state<Set<string>>(new Set());

  function toggle(mountKey: string, path: string): void {
    const key = `${mountKey}\u0000${path}`;
    const next = new Set(collapsed);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    collapsed = next;
  }

  function pick(mountKey: string, node: FileNode): void {
    if (node.isDir === true) toggle(mountKey, node.path);
    else onOpen(mountKey, node.path);
  }
</script>

{#snippet treeNode(mount: MountView, node: FileNode, depth: number)}
  {@const open = !collapsed.has(`${mount.key}\u0000${node.path}`)}
  {@const isSelected = selected?.mountKey === mount.key && selected.path === node.path}
  <li>
    <button
      type="button"
      class="row"
      class:selected={isSelected}
      class:changed={node.changed}
      style={`--depth: ${depth}`}
      aria-expanded={node.isDir === true ? open : undefined}
      aria-current={isSelected ? "true" : undefined}
      data-path={node.path}
      title={node.changed ? "Changed by this mount's latest commit at the cursor" : undefined}
      onclick={() => pick(mount.key, node)}
    >
      {#if node.isDir === true}
        {#if open}<ChevronDown size={12} aria-hidden="true" />{:else}<ChevronRight
            size={12}
            aria-hidden="true"
          />{/if}
        <Folder size={13} aria-hidden="true" />
      {:else}
        <span class="spacer"></span>
        <FileIcon size={13} aria-hidden="true" />
      {/if}
      <span class="name">{node.name}</span>
      {#if node.isDir !== true}<span class="size">{formatSize(node.size)}</span>{/if}
    </button>
    {#if node.isDir === true && open && node.children.length > 0}
      <ul>
        {#each node.children as child (child.path)}
          {@render treeNode(mount, child, depth + 1)}
        {/each}
      </ul>
    {/if}
  </li>
{/snippet}

<div class="mounts">
  {#each mounts as mount (mount.key)}
    <div class="mount" data-mount={mount.mount}>
      <div class="mount-head">
        <span class="mount-path">{mount.mount}</span>
        <Chip
          label={mount.kind === "tree" ? "in memory" : "touched this session"}
          tone={mount.kind === "tree" ? "accent" : "live"}
          size="xs"
          fill="quiet"
          data-tip={mount.kind === "tree"
            ? "Contents are session state, shown as of the cursor"
            : "Only paths this session's scripts have looked up, listed, read or written, not " +
              "everything in the store. Contents are read from the store when opened, as they " +
              "are now"}
        />
        {#if mount.readOnly}<Chip label="read-only" size="xs" fill="quiet" />{/if}
        {#if mount.role === "subagent"}<span class="kicker owner">{mount.label}</span>{/if}
      </div>
      {#if mount.kind === "index"}
        <p class="mount-scope">Showing only what this session has touched, not the whole store.</p>
      {/if}
      {#if mount.description}<p class="mount-about">{mount.description}</p>{/if}
      {#if mount.problem}<p class="problem">{mount.problem}</p>{/if}
      {#if mount.root.children.length === 0}
        <p class="mount-about">
          {mount.kind === "index"
            ? "This session hasn't touched anything in this mount yet."
            : "Nothing in this mount yet."}
        </p>
      {:else}
        <ul class="tree" aria-label={`Files in ${mount.mount}`}>
          {#each mount.root.children as child (child.path)}
            {@render treeNode(mount, child, 0)}
          {/each}
        </ul>
      {/if}
    </div>
  {/each}
</div>

<style>
  .mounts {
    display: grid;
    align-content: start;
    gap: var(--gap-md);
    min-width: 0;
  }

  .mount {
    display: grid;
    gap: var(--gap-2xs);
    min-width: 0;
  }

  .mount-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .mount-path {
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    color: var(--text-1);
    overflow-wrap: anywhere;
  }

  .mount-scope {
    margin: 0;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.45;
  }

  .mount-about {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
    line-height: 1.45;
  }

  .owner {
    margin: 0;
  }

  .problem {
    margin: 0;
    color: var(--text-2);
    font-size: var(--font-sm);
  }

  .tree,
  .tree ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
    width: 100%;
    min-width: 0;
    padding: 2px var(--gap-sm) 2px calc(var(--gap-sm) + var(--depth) * 14px);
    border: 0;
    border-left: 2px solid transparent;
    background: transparent;
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    text-align: left;
    cursor: pointer;
  }

  .row.selected {
    background: var(--surface-2);
    color: var(--text-1);
  }

  /* The mount's latest commit at the cursor touched this path or something under it. */
  .row.changed {
    border-left-color: var(--success);
  }

  @media (hover: hover) and (pointer: fine) {
    .row:hover {
      background: var(--control-hover);
    }
  }

  .spacer {
    width: 12px;
    flex: 0 0 12px;
  }

  .name {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .size {
    flex: 0 0 auto;
    color: var(--text-4);
    font-size: var(--font-2xs);
  }
</style>
