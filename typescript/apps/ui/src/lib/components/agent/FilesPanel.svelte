<script lang="ts">
  import FileViewerDialog from "./FileViewerDialog.svelte";
  import MountTrees from "./MountTrees.svelte";
  import OKFGraphDialog from "./OKFGraphDialog.svelte";
  import type { FileChunk, FileViewRequest, OKFGraph, OKFGraphRequest } from "$lib/api/types";
  import type { MountView } from "$lib/state/fileMounts";
  import type { WalkedGraph } from "$lib/state/okfGraph";

  /**
   * Every Code Mode mount an agent in this run tracks, as a tree as of the cursor. Opening a
   * file shows it in the file viewer, large, beside the same trees.
   */
  interface Props {
    mounts: MountView[];
    /** The cursor is at the live edge of the run. */
    live: boolean;
    onViewFile: (request: FileViewRequest) => Promise<FileChunk>;
    /** Walks an OKF bundle mount for its graph. Without it, OKF mounts offer no graph. */
    onOkfGraph?: (request: OKFGraphRequest) => Promise<OKFGraph>;
    onJumpToLive?: () => void;
    /** The open file: which mount, and its path in the mount. */
    selected?: { mountKey: string; path: string } | null;
    /** The mount whose OKF graph is open. */
    graphMountKey?: string | null;
  }

  let {
    mounts,
    live,
    onViewFile,
    onOkfGraph,
    onJumpToLive,
    selected = $bindable(null),
    graphMountKey = $bindable(null)
  }: Props = $props();

  const graphMount = $derived(
    graphMountKey ? (mounts.find((m) => m.key === graphMountKey) ?? null) : null
  );

  // Each OKF mount's last walk, so reopening its graph doesn't walk the bundle again unless
  // the mount has changed since.
  let walkedGraphs = $state<Record<string, WalkedGraph>>({});
</script>

<section class="files" aria-label="VFS File Mounts">
  {#if mounts.length === 0}
    <div class="empty">
      <p>No agent in this run tracks a filesystem mount.</p>
      <p class="empty-note">
        Track a Code Mode mount by declaring it on the agent class, then bind it in
        <code>@agent.init</code>. An in-memory filesystem keeps its files, contents included;
        an activity-backed one keeps an index of its tree:
      </p>
      <pre data-language="python">workspace = agent.vfs_mount("/workspace", agent.InMemoryFileSystem, description="...")
memory = agent.vfs_mount("/memory", LocalDisk, description="...")

mounts=[self.workspace.bind(seed=None), self.memory.bind(LocalDiskConfig(...))]</pre>
    </div>
  {:else}
    <div class="trees">
      <MountTrees
        {mounts}
        {selected}
        onOpen={(mountKey, path) => (selected = { mountKey, path })}
        onGraph={onOkfGraph ? (mountKey) => (graphMountKey = mountKey) : undefined}
      />
    </div>
  {/if}
</section>

<FileViewerDialog {mounts} {live} bind:selected {onViewFile} {onJumpToLive} />
{#if graphMount && onOkfGraph}
  {@const key = graphMount.key}
  {#key key}
    <OKFGraphDialog
      mount={graphMount}
      {live}
      {onOkfGraph}
      {onViewFile}
      {onJumpToLive}
      cached={walkedGraphs[key] ?? null}
      onWalked={(walked) => (walkedGraphs = { ...walkedGraphs, [key]: walked })}
      onClose={() => (graphMountKey = null)}
    />
  {/key}
{/if}

<style>
  .files {
    display: flex;
    flex-direction: column;
    min-width: 0;
    height: 100%;
    overflow: hidden;
  }

  .trees {
    flex: 1;
    min-height: 0;
    overflow: auto;
  }

  .empty {
    display: grid;
    gap: var(--gap-sm);
    padding: var(--gutter-tight);
    background: var(--surface-2);
  }

  .empty p {
    margin: 0;
  }

  .empty-note {
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .empty-note code {
    font-family: var(--font-mono);
  }

  .empty pre {
    margin: 0;
    font-family: var(--font-mono);
    font-size: var(--font-code);
    white-space: pre-wrap;
  }
</style>
