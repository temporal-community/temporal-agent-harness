<script lang="ts">
  import { untrack } from "svelte";
  import { formatTimestamp } from "$lib/state/replayLog";
  import { X } from "@lucide/svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import MarkdownMessage from "$lib/components/chat/MarkdownMessage.svelte";
  import MountTrees, { nameParts } from "./MountTrees.svelte";
  import type { FileChunk, FileViewRequest } from "$lib/api/types";
  import {
    contentBytes,
    decodeText,
    fileView,
    findNode,
    formatSize,
    splitFrontMatter,
    type MountView
  } from "$lib/state/fileMounts";

  /**
   * The open file, large, beside every mount's tree so exploring doesn't mean closing and
   * reopening it. Built like the flow's node inspector: a modal dialog over a scrim.
   *
   * Where the contents come from depends on the mount, and the dialog says which:
   *
   *   - an in-memory mount (`FileTree`) holds its contents in state, so a file shows as it
   *     was at the cursor, like the tree;
   *   - an indexed mount (`FileIndex`) holds no contents. A file is read from the mount's
   *     store when it is opened, so it shows as it is NOW, whatever point in the session
   *     the cursor is at.
   */
  interface Props {
    mounts: MountView[];
    /** The cursor is at the live edge of the run. */
    live: boolean;
    /** The open file. Closing the dialog sets it back to `null`. */
    selected: { mountKey: string; path: string } | null;
    onViewFile: (request: FileViewRequest) => Promise<FileChunk>;
    onJumpToLive?: () => void;
  }

  let { mounts, live, selected = $bindable(), onViewFile, onJumpToLive }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);

  /* showModal() for the top layer's focus trap, Escape and ::backdrop, as the node
     inspector does. Focus goes to the open file's row in the tree once the tree has drawn
     it, rather than to Close, the first button, where showModal() would put it. */
  $effect(() => {
    if (!dialog || dialog.open) return;
    dialog.showModal();
    const focusOpenFile = (node: HTMLDialogElement, tries = 10) => {
      const row = node.querySelector<HTMLElement>('.trees [aria-current="true"]');
      if (row) row.focus();
      else if (tries > 0) requestAnimationFrame(() => focusOpenFile(node, tries - 1));
    };
    focusOpenFile(dialog);
  });

  const mount = $derived(
    selected ? (mounts.find((m) => m.key === selected!.mountKey) ?? null) : null
  );
  const node = $derived(mount && selected ? findNode(mount.root, selected.path) : null);
  const name = $derived(selected ? (selected.path.split("/").pop() ?? selected.path) : "");
  const [titleStem, titleTail] = $derived(nameParts(name, "."));
  const view = $derived(selected ? fileView(selected.path) : { mode: "text" as const });

  interface Fetched {
    /** Which file and which version of it these bytes answer. */
    token: string;
    bytes: Uint8Array;
    size: number;
    readAt: Date;
  }

  let fetched = $state<Fetched | null>(null);
  let loading = $state(false);
  let fetchError = $state<string | null>(null);
  let requestCount = 0;

  /* A store read is asked for again when the file changes in the index, but only at the
     live edge: scrubbing moves the index through old versions, and every one of them would
     read the same current file again. */
  const fetchToken = $derived.by(() => {
    if (!selected || mount?.kind !== "index" || !mount.source) return null;
    if (node?.isDir === true) return null;
    const version = live ? (node?.version ?? "gone") : "";
    return `${mount.key}\u0000${selected.path}\u0000${version}`;
  });

  async function read(token: string, offset: number): Promise<void> {
    const source = mount?.source;
    if (!source || !selected) return;
    const request = ++requestCount;
    loading = true;
    fetchError = null;
    try {
      const chunk = await onViewFile({ source, path: selected.path, offset });
      if (request !== requestCount) return;
      const bytes = contentBytes(chunk.content, chunk.encoding);
      const before = offset > 0 && fetched?.token === token ? fetched.bytes : new Uint8Array();
      const joined = new Uint8Array(before.length + bytes.length);
      joined.set(before);
      joined.set(bytes, before.length);
      fetched = { token, bytes: joined, size: chunk.size, readAt: new Date() };
    } catch (error) {
      if (request !== requestCount) return;
      fetched = null;
      fetchError = error instanceof Error ? error.message : String(error);
    } finally {
      if (request === requestCount) loading = false;
    }
  }

  $effect(() => {
    const token = fetchToken;
    if (token === null) {
      requestCount += 1;
      fetched = null;
      fetchError = null;
      loading = false;
      return;
    }
    // `read` looks at the selection and at what was fetched before; the read is all this
    // effect depends on through `fetchToken`, so it must not also depend on what it writes.
    untrack(() => void read(token, 0));
  });

  /** What the viewer shows: bytes from state or from the store, as text when they are. */
  const shown = $derived.by(() => {
    if (!mount || !node || node.isDir === true) return null;
    if (mount.kind === "tree") {
      if (!node.content) return null;
      const bytes = contentBytes(node.content.text, node.content.encoding);
      return { text: decodeText(bytes), size: bytes.length, complete: true };
    }
    if (!fetched || fetched.token !== fetchToken) return null;
    return {
      text: decodeText(fetched.bytes),
      size: fetched.size,
      complete: fetched.bytes.length >= fetched.size
    };
  });
</script>

{#if selected && mount}
  <!-- A click that lands on the dialog itself can only have come from the backdrop,
       because the body fills it edge to edge. -->
  <dialog
    bind:this={dialog}
    class="file-viewer"
    aria-label={`${mount.mount}/${selected.path}`}
    onclose={() => (selected = null)}
    onclick={(event) => {
      if (event.target === dialog) dialog?.close();
    }}
  >
    <header class="surface-head">
      <div class="head-name">
        <h2 class="head-title" title={name}><span class="stem">{titleStem}</span><span class="tail">{titleTail}</span></h2>
        <p class="head-subtitle" title={`${mount.mount}/${selected.path}`}>
          <span class="path">{mount.mount}/{selected.path}</span>
          {#if shown}<span class="size">{formatSize(shown.size)}</span>{/if}
        </p>
      </div>
      <button
        type="button"
        class="head-button"
        aria-label="Close file"
        data-tip="Close file"
        data-tip-below
        data-tip-align="end"
        onclick={() => dialog?.close()}
      ><X size={14} aria-hidden="true" /></button>
    </header>

    <div class="viewer-body">

      <div class="columns">
        <nav class="trees" aria-label="Files">
          <MountTrees
            {mounts}
            {selected}
            onOpen={(mountKey, path) => (selected = { mountKey, path })}
          />
        </nav>

        <section class="file" aria-live="polite">
          {#if mount.kind === "tree"}
            <p class="provenance">
              From session state: the file as it was at the point in the session you're
              looking at.
            </p>
          {:else if mount.source}
            <p class="provenance">
              Read from the store {fetched?.readAt
                ? `at ${formatTimestamp(fetched.readAt.getTime() / 1000)}`
                : "when opened"}: the file as it is <strong>now</strong>, not as of this point
              in the session.
            </p>
            {#if !live}
              <div class="scrubbed callout-warning" role="note">
                <p>
                  You're looking at an earlier point in the session. The tree is as of the
                  cursor, but these contents are current, so they may differ from what the
                  agent saw then.
                </p>
                {#if onJumpToLive}
                  <Chip label="Jump to latest step" tone="accent" size="xs" onclick={onJumpToLive} />
                {/if}
              </div>
            {/if}
          {/if}

          <div class="contents">
            {#if !node}
              <p class="hint">This file isn't in the mount at this point in the session.</p>
            {:else if mount.kind === "index" && !mount.source}
              <p class="hint">
                Only this mount's tree is tracked; its contents can't be read from the console.
              </p>
            {:else if fetchError}
              <p class="callout-warning">{fetchError}</p>
            {:else if !shown}
              <p class="hint">{loading ? "Reading…" : "No contents."}</p>
            {:else if shown.text === null}
              <p class="hint">Binary file ({formatSize(shown.size)}); no preview.</p>
            {:else}
              {#if view.mode === "markdown"}
                {@const doc = splitFrontMatter(shown.text)}
                {#if doc.frontMatter !== null}
                  <MarkdownMessage text={doc.frontMatter} language="yaml" />
                {/if}
                <MarkdownMessage text={doc.body} />
              {:else if view.mode === "code"}
                <MarkdownMessage text={shown.text} language={view.language} />
              {:else}
                <pre class="plain">{shown.text}</pre>
              {/if}
              {#if !shown.complete && fetched && fetchToken}
                <Chip
                  label={loading
                    ? "Reading…"
                    : `Load more (${formatSize(fetched.bytes.length)} of ${formatSize(shown.size)})`}
                  tone="accent"
                  size="xs"
                  disabled={loading}
                  onclick={() => read(fetchToken!, fetched!.bytes.length)}
                />
              {/if}
            {/if}
          </div>
        </section>
      </div>
    </div>
  </dialog>
{/if}

<style>
  /* Centred by the UA's own :modal rule, like the node inspector, and larger: a file
     wants the room. */
  .file-viewer {
    container: file-viewer / inline-size;
    display: flex;
    flex-direction: column;
    width: min(1200px, calc(100% - var(--gutter) * 4));
    height: min(860px, calc(100% - var(--gutter) * 4));
    padding: 0;
    overflow: hidden;
    border: 1px solid color-mix(in srgb, var(--accent) 72%, var(--text-1) 6%);
    background: color-mix(in srgb, var(--accent) 9%, var(--surface-2));
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
  }

  .file-viewer::backdrop {
    background: var(--overlay-scrim);
  }

  .viewer-body {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gutter);
    padding: var(--gutter);
  }

  /* One line however long the name: it gives way in the middle, keeping its extension, and
     leaves the path beside it room. The whole name is the title's tip. */
  .head-title {
    display: flex;
    max-width: 60%;
    margin: 0;
  }

  .stem {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tail {
    flex: none;
  }

  .path {
    font-family: var(--font-mono);
  }

  .size {
    margin-inline-start: var(--gap-sm);
    color: var(--text-4);
    font-size: var(--font-2xs);
  }

  /* The trees and the file each scroll themselves; the dialog never does. */
  .columns {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(200px, 280px) minmax(0, 1fr);
    gap: var(--gutter);
  }

  .trees {
    min-height: 0;
    overflow: auto;
    padding-right: var(--gutter-tight);
    border-right: 1px solid var(--border);
  }

  /* A phone can't hold a 200px tree column beside the file: stack them. */
  @container file-viewer (max-width: 40rem) {
    .columns {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: minmax(0, 1fr) minmax(0, 2fr);
    }

    .trees {
      padding-right: 0;
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }
  }

  .file {
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .provenance {
    margin: 0;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.45;
  }

  /* The one case where what the dialog shows and what the cursor says can disagree. */
  .scrubbed {
    display: grid;
    gap: var(--gap-sm);
    justify-items: start;
  }

  .scrubbed p {
    margin: 0;
  }

  .contents {
    flex: 1 1 0;
    min-height: 0;
    overflow: auto;
    display: grid;
    align-content: start;
    gap: var(--gap-sm);
    padding: var(--gutter-tight);
    border: 1px solid var(--border);
    background: var(--surface-0);
  }

  .contents > :global(.chip) {
    justify-self: start;
  }

  .hint {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .plain {
    margin: 0;
    font-family: var(--font-mono);
    font-size: var(--font-code);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-1);
  }
</style>
