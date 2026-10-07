<script lang="ts">
  import { untrack } from "svelte";
  import { RefreshCw, X } from "@lucide/svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import MarkdownMessage from "$lib/components/chat/MarkdownMessage.svelte";
  import OKFGraphView from "./OKFGraphView.svelte";
  import type { FileChunk, FileViewRequest, OKFGraph, OKFGraphRequest } from "$lib/api/types";
  import { contentBytes, decodeText, formatSize, type MountView } from "$lib/state/fileMounts";
  import {
    colorOf,
    linksOf,
    stripFrontmatter,
    typeColors,
    type WalkedGraph
  } from "$lib/state/okfGraph";

  /**
   * An OKF bundle mount's whole graph, beside the selected concept. Built like the file
   * viewer: a modal dialog over a scrim.
   *
   * The graph is the bundle as it is in its store NOW, walked by the filesystem's
   * `okf_graph` activity, not the files this session touched (the pane's tree) and not the
   * bundle as of the cursor. The dialog says so, and warns when the cursor is behind the
   * live edge.
   *
   * A walk reads the whole bundle, so it runs only when needed: on opening, unless the last
   * walk (`cached`) was taken since the mount's state last changed, and on Reload.
   */
  interface Props {
    mount: MountView;
    live: boolean;
    onOkfGraph: (request: OKFGraphRequest) => Promise<OKFGraph>;
    onViewFile: (request: FileViewRequest) => Promise<FileChunk>;
    onJumpToLive?: () => void;
    onClose: () => void;
    /** The mount's last walk, shown without walking again while it is current. */
    cached?: WalkedGraph | null;
    /** Called with each walk, for the caller to pass back as `cached` next time. */
    onWalked?: (walked: WalkedGraph) => void;
    selectedId?: string | null;
  }

  let {
    mount,
    live,
    onOkfGraph,
    onViewFile,
    onJumpToLive,
    onClose,
    cached = null,
    onWalked,
    selectedId = $bindable(null)
  }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  $effect(() => {
    if (dialog && !dialog.open) dialog.showModal();
  });

  // ---------------------------------------------------------------- the graph

  let graph = $state<OKFGraph | null>(untrack(() => cached?.graph ?? null));
  let loading = $state(false);
  let graphError = $state<string | null>(null);
  let walks = 0;

  async function walk(): Promise<void> {
    const source = mount.source;
    if (!source || loading) return;
    const stateVersion = mount.stateVersion;
    const request = ++walks;
    loading = true;
    graphError = null;
    try {
      const walked = await onOkfGraph({ source });
      if (request === walks) {
        graph = walked;
        onWalked?.({ graph: walked, stateVersion });
      }
    } catch (error) {
      if (request === walks) graphError = error instanceof Error ? error.message : String(error);
    } finally {
      if (request === walks) loading = false;
    }
  }

  // On opening, or once the mount first records its source while the dialog is open.
  const walkable = $derived(mount.source !== null);
  $effect(() => {
    if (!walkable) return;
    untrack(() => {
      if (cached?.stateVersion !== mount.stateVersion) void walk();
    });
  });
  // A walk that finishes after the dialog closes is dropped.
  $effect(() => () => {
    walks += 1;
  });

  const colors = $derived(typeColors(graph?.concepts ?? []));
  const types = $derived(
    [...colors.keys()].map((type) => ({
      type,
      count: graph?.concepts.filter((c) => c.type === type).length ?? 0
    }))
  );

  let query = $state("");
  let hiddenTypes = $state<string[]>([]);

  function toggleType(type: string) {
    hiddenTypes = hiddenTypes.includes(type)
      ? hiddenTypes.filter((t) => t !== type)
      : [...hiddenTypes, type];
  }

  // ---------------------------------------------------------------- the selected concept

  const concept = $derived(graph?.concepts.find((c) => c.id === selectedId) ?? null);
  const related = $derived(graph && selectedId ? linksOf(graph, selectedId) : null);
  const titleOf = (id: string) => graph?.concepts.find((c) => c.id === id)?.title ?? id;

  let body = $state<{ path: string; text: string | null; size: number } | null>(null);
  let bodyError = $state<string | null>(null);
  let reads = 0;

  const bodyToken = $derived(
    concept && mount.source ? `${concept.path}\u0000${graph?.walked_at ?? 0}` : null
  );

  $effect(() => {
    const token = bodyToken;
    const path = untrack(() => concept?.path);
    const source = untrack(() => mount.source);
    const request = ++reads;
    body = null;
    bodyError = null;
    if (token === null || !path || !source) return;
    onViewFile({ source, path })
      .then((chunk) => {
        if (request !== reads) return;
        const text = decodeText(contentBytes(chunk.content, chunk.encoding));
        body = { path, text: text === null ? null : stripFrontmatter(text), size: chunk.size };
      })
      .catch((error) => {
        if (request === reads) bodyError = error instanceof Error ? error.message : String(error);
      });
  });

  const tierLabel = {
    unverified: "Unverified",
    "machine-confirmed": "Machine-confirmed",
    "human-reviewed": "Confirmed by a person"
  } as const;
</script>

<dialog
  bind:this={dialog}
  class="okf-graph"
  aria-label={`${mount.mount} knowledge graph`}
  onclose={onClose}
  onclick={(event) => {
    if (event.target === dialog) dialog?.close();
  }}
>
  <div class="graph-body">
    <header class="graph-header">
      <div class="heading">
        <h2>{mount.mount} knowledge graph</h2>
        <p>
          {#if graph}
            <span>{graph.concepts.length} concepts · {graph.links.length} links</span>
            {#if graph.truncated}<span class="truncated">Only part of the bundle: it is larger than the graph reads.</span>{/if}
          {:else if loading}
            <span>Reading the bundle…</span>
          {/if}
        </p>
      </div>
      <div class="actions">
        <Chip label="Live" tone="live" size="sm" pip ring />
        <IconButton
          label="Reload graph"
          tip="Walk the bundle again"
          disabled={!mount.source || loading}
          onclick={() => void walk()}
        >
          <RefreshCw size={16} />
        </IconButton>
        <IconButton label="Close graph" onclick={() => dialog?.close()}>
          <X size={16} />
        </IconButton>
      </div>
    </header>

    <p class="provenance">
      The whole bundle as it is in its store <strong>now</strong>{graph
        ? `, read at ${new Date(graph.walked_at * 1000).toLocaleTimeString()}`
        : ""}: every concept, not only the ones this session touched, and not as of this point
      in the session.
    </p>
    {#if !live}
      <div class="scrubbed" role="note">
        <p>
          You're looking at an earlier point in the session. The graph is current, so it may
          hold memories the agent hadn't written yet at this point.
        </p>
        {#if onJumpToLive}
          <button type="button" class="jump" onclick={onJumpToLive}>Jump to latest step</button>
        {/if}
      </div>
    {/if}

    <div class="columns">
      <section class="canvas" aria-label="Graph">
        <div class="filters">
          <input
            class="search"
            type="search"
            placeholder="Search titles, ids, tags"
            aria-label="Search concepts"
            bind:value={query}
          />
          <div class="types" role="group" aria-label="Concept types">
            {#each types as { type, count } (type)}
              <button
                type="button"
                class="type"
                class:off={hiddenTypes.includes(type)}
                aria-pressed={!hiddenTypes.includes(type)}
                onclick={() => toggleType(type)}
              >
                <span class="swatch" style={`--node: ${colorOf(colors, type)}`}></span>
                {type}
                <span class="count">{count}</span>
              </button>
            {/each}
          </div>
        </div>
        <div class="stage">
          {#if graphError}
            <p class="problem">{graphError}</p>
          {:else if !mount.source}
            <p class="hint">
              The graph is available once this session has used the mount: that is when the
              console learns where the bundle is stored.
            </p>
          {:else if graph && graph.concepts.length === 0}
            <p class="hint">The bundle has no concepts yet.</p>
          {:else if graph}
            <OKFGraphView {graph} {colors} {query} {hiddenTypes} bind:selectedId />
          {/if}
        </div>
      </section>

      <aside class="concept" aria-live="polite">
        {#if !selectedId}
          <p class="hint">Select a concept to read it.</p>
        {:else if !concept}
          <h3>{selectedId}</h3>
          <p class="hint">
            Not written yet: other concepts link here, but the bundle has no
            <code>{selectedId}.md</code>.
          </p>
        {:else}
          <h3>{concept.title}</h3>
          <p class="path">{mount.mount}/{concept.path} <span class="size">{formatSize(concept.size)}</span></p>
          <div class="facts">
            <Chip label={concept.type} size="sm" fill="quiet" />
            {#if concept.status !== "stable"}<Chip label={concept.status} size="sm" fill="quiet" />{/if}
            {#if concept.stale}<Chip label="Stale" size="sm" fill="quiet" />{/if}
            <Chip label={tierLabel[concept.trust_tier]} size="sm" fill="quiet" />
          </div>
          {#if concept.description}<p class="description">{concept.description}</p>{/if}
          {#if concept.tags.length > 0}
            <p class="tags">{#each concept.tags as tag, i (i)}<span class="tag">{tag}</span>{/each}</p>
          {/if}
          {#if concept.problem}
            <p class="problem">{concept.problem}</p>
          {/if}

          {#if related && (related.linksTo.length > 0 || related.citedBy.length > 0)}
            <div class="links">
              {#if related.linksTo.length > 0}
                <h4>Links to</h4>
                <ul>
                  {#each related.linksTo as link (link.target)}
                    <li>
                      <button type="button" class="link" onclick={() => (selectedId = link.target)}>
                        {titleOf(link.target)}{#if link.dangling}<span class="ghost-note"> (not written yet)</span>{/if}
                      </button>
                    </li>
                  {/each}
                </ul>
              {/if}
              {#if related.citedBy.length > 0}
                <h4>Cited by</h4>
                <ul>
                  {#each related.citedBy as link (link.source)}
                    <li>
                      <button type="button" class="link" onclick={() => (selectedId = link.source)}>
                        {titleOf(link.source)}
                      </button>
                    </li>
                  {/each}
                </ul>
              {/if}
            </div>
          {/if}

          <div class="contents">
            {#if bodyError}
              <p class="problem">{bodyError}</p>
            {:else if !body || body.path !== concept.path}
              <p class="hint">Reading…</p>
            {:else if body.text === null}
              <p class="hint">Binary file; no preview.</p>
            {:else}
              <MarkdownMessage text={body.text} />
            {/if}
          </div>
        {/if}
        {#if related && !concept && related.citedBy.length > 0}
          <div class="links">
            <h4>Cited by</h4>
            <ul>
              {#each related.citedBy as link (link.source)}
                <li>
                  <button type="button" class="link" onclick={() => (selectedId = link.source)}>
                    {titleOf(link.source)}
                  </button>
                </li>
              {/each}
            </ul>
          </div>
        {/if}
      </aside>
    </div>
  </div>
</dialog>

<style>
  .okf-graph {
    display: flex;
    width: min(1320px, calc(100% - var(--gutter) * 4));
    height: min(880px, calc(100% - var(--gutter) * 4));
    padding: 0;
    overflow: hidden;
    border: 1px solid color-mix(in srgb, var(--accent) 72%, var(--text-1) 6%);
    background: color-mix(in srgb, var(--accent) 9%, var(--surface-2));
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
  }

  .okf-graph::backdrop {
    background: var(--overlay-scrim);
  }

  .graph-body {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gap-md);
    padding: var(--gutter);
  }

  .graph-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--gutter);
  }

  .heading h2 {
    margin: 0;
    font-size: var(--font-title);
    line-height: 1.2;
    letter-spacing: 0;
    overflow-wrap: anywhere;
  }

  .heading p {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    margin: var(--gap-xs) 0 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .truncated {
    color: var(--warning);
  }

  .actions {
    display: flex;
    align-items: center;
    gap: var(--gap-md);
  }

  .provenance {
    margin: 0;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.45;
  }

  .scrubbed {
    display: grid;
    gap: var(--gap-sm);
    justify-items: start;
    padding: var(--gutter-tight);
    border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border));
    background: color-mix(in srgb, var(--warning) 8%, var(--surface-2));
    color: var(--text-2);
    font-size: var(--font-sm);
  }

  .scrubbed p {
    margin: 0;
  }

  .columns {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(280px, 380px);
    gap: var(--gutter);
  }

  .canvas {
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  .search {
    flex: 0 1 240px;
    min-width: 140px;
    height: var(--control-height);
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--control-bg);
    color: var(--text-1);
    font-size: var(--font-sm);
  }

  .types {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-xs);
  }

  .type {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: var(--control-height-xs);
    padding: 0 8px;
    border: 1px solid var(--border);
    border-radius: var(--radius-chip);
    background: var(--surface-1);
    color: var(--text-2);
    font-size: var(--font-xs);
    cursor: pointer;
  }

  .type.off {
    opacity: 0.45;
  }

  .swatch {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--node);
  }

  .count {
    color: var(--text-4);
  }

  .stage {
    flex: 1;
    min-height: 0;
    display: flex;
    border: 1px solid var(--border);
    background: var(--surface-0);
    overflow: hidden;
  }

  .stage > :global(svg) {
    flex: 1;
  }

  .stage > .hint,
  .stage > .problem {
    margin: var(--gutter-tight);
  }

  .concept {
    min-height: 0;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding-left: var(--gutter-tight);
    border-left: 1px solid var(--border);
  }

  .concept h3 {
    margin: 0;
    font-size: var(--font-lg);
    overflow-wrap: anywhere;
  }

  .concept h4 {
    margin: var(--gap-xs) 0 0;
    color: var(--text-3);
    font-size: var(--font-xs);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
  }

  .path {
    margin: 0;
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }

  .size {
    color: var(--text-4);
    font-family: var(--font-sans);
    font-size: var(--font-2xs);
  }

  .facts,
  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-xs);
    margin: 0;
  }

  .tag {
    padding: 1px 6px;
    border-radius: var(--radius-chip);
    background: var(--surface-1);
    color: var(--text-3);
    font-size: var(--font-xs);
  }

  .description {
    margin: 0;
    color: var(--text-1);
    font-size: var(--font-sm);
    line-height: 1.45;
  }

  .links ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .link {
    padding: 2px 0;
    border: 0;
    background: none;
    color: var(--accent);
    font-size: var(--font-sm);
    text-align: left;
    cursor: pointer;
  }

  .ghost-note {
    color: var(--text-4);
  }

  .contents {
    flex: 1 0 auto;
    padding: var(--gutter-tight);
    border: 1px solid var(--border);
    background: var(--surface-0);
  }

  .hint {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .problem {
    margin: 0;
    padding: var(--gutter-tight);
    border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border));
    background: color-mix(in srgb, var(--warning) 8%, var(--surface-2));
    color: var(--text-2);
    font-size: var(--font-sm);
  }

  .jump {
    padding: 6px 12px;
    border: 1px solid color-mix(in srgb, var(--accent) 38%, var(--border));
    border-radius: var(--radius-chip);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    color: var(--text-1);
    font-size: var(--font-sm);
    font-weight: 650;
    cursor: pointer;
  }
</style>
