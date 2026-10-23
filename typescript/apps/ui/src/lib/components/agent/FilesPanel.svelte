<script lang="ts">
  import { FilePen, FilePlus, FileX } from "@lucide/svelte";
  import FileViewerDialog from "./FileViewerDialog.svelte";
  import MountTrees from "./MountTrees.svelte";
  import OKFGraphDialog from "./OKFGraphDialog.svelte";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import type { Metric } from "$lib/components/primitives/metrics";
  import Reading from "$lib/components/primitives/Reading.svelte";
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

  /* What the tree is a reading of, said once for the pane rather than once per mount. */
  const allIndexed = $derived(mounts.every((m) => m.kind === "index"));
  const allInMemory = $derived(mounts.every((m) => m.kind === "tree"));
  const nothingYet = $derived(mounts.every((m) => m.root.children.length === 0));
  const fileTotal = $derived(mounts.reduce((sum, m) => sum + m.fileCount, 0));
  const readingLabel = $derived(
    allIndexed ? "Touched this session · at cursor" : allInMemory ? "In memory · at cursor" : "At cursor"
  );
  const INDEXED_SCOPE =
    "An indexed mount lists only the paths this session's scripts looked up, listed, read or wrote, not everything in the store.";
  const MEMORY_SCOPE = "An in-memory mount lists every file it holds, as of the cursor.";
  const CHANGE_METRICS = [
    { change: "added", label: "Added", icon: FilePlus, hue: "--success" },
    { change: "modified", label: "Changed", icon: FilePen, hue: "--warning" },
    { change: "removed", label: "Removed", icon: FileX, hue: "--error" }
  ] as const;
  const headline = $derived<Metric[]>([
    {
      label: "Files",
      value: fileTotal.toLocaleString("en-US"),
      note: allIndexed ? INDEXED_SCOPE : allInMemory ? MEMORY_SCOPE : `${INDEXED_SCOPE} ${MEMORY_SCOPE}`
    },
    ...CHANGE_METRICS.map(({ change, label, icon, hue }) => ({
      label,
      icon,
      hue,
      value: mounts.reduce((sum, m) => sum + m.changes[change], 0)
    }))
      .filter((metric) => metric.value > 0)
      .map((metric) => ({ ...metric, value: metric.value.toLocaleString("en-US") }))
  ]);

  const MOUNT_EXAMPLE = `workspace = agent.vfs_mount("/workspace", agent.InMemoryFileSystem, description="...")
memory = agent.vfs_mount("/memory", LocalDisk, description="...")

mounts=[self.workspace.bind(seed=None), self.memory.bind(LocalDiskConfig(...))]`;
</script>

<section class="files" class:empty={mounts.length === 0} aria-label="VFS File Mounts">
  {#if mounts.length === 0}
    <EmptyState
      title="No agent in this run tracks a filesystem mount."
      language="python"
      code={MOUNT_EXAMPLE}
    >
      <p>
        Track a Code Mode mount by declaring it on the agent class, then bind it in
        <code>@agent.init</code>. An in-memory filesystem keeps its files, contents included;
        an activity-backed one keeps an index of its tree:
      </p>
    </EmptyState>
  {:else}
    {#if nothingYet}
      <EmptyState
        title={allIndexed ? "This session hasn't touched a file in any mount yet." : "No mount holds a file yet."}
      />
    {:else}
      <Reading label={readingLabel} {headline} />
    {/if}
    <div class="trees">
      <MountTrees
        {mounts}
        {selected}
        graphKey={graphMountKey}
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
    gap: var(--gutter);
    min-width: 0;
    height: 100%;
    padding: var(--pane-inset);
    overflow: hidden;
  }

  /* Into the pane's end inset by the tree's track, which then is that inset. */
  .trees {
    flex: 1;
    min-height: 0;
    margin-inline-end: calc(-1 * var(--scrollbar-size));
    overflow: auto;
  }

  .files.empty {
    overflow: auto;
  }
</style>
