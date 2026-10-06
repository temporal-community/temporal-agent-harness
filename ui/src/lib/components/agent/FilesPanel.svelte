<script lang="ts">
  import FileViewerDialog from "./FileViewerDialog.svelte";
  import MountTrees from "./MountTrees.svelte";
  import type { FileChunk, FileViewRequest } from "$lib/api/types";
  import type { MountView } from "$lib/state/fileMounts";

  /**
   * Every Code Mode mount an agent in this run tracks, as a tree as of the cursor. Opening a
   * file shows it in the file viewer, large, beside the same trees.
   */
  interface Props {
    mounts: MountView[];
    /** The cursor is at the live edge of the run. */
    live: boolean;
    onViewFile: (request: FileViewRequest) => Promise<FileChunk>;
    onJumpToLive?: () => void;
    /** The open file: which mount, and its path in the mount. */
    selected?: { mountKey: string; path: string } | null;
  }

  let { mounts, live, onViewFile, onJumpToLive, selected = $bindable(null) }: Props = $props();
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
      />
    </div>
  {/if}
</section>

<FileViewerDialog {mounts} {live} bind:selected {onViewFile} {onJumpToLive} />

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
