<script lang="ts">
  import { X } from "@lucide/svelte";
  import IconButton from "./IconButton.svelte";
  import { formatImageBytes, imagePolicy } from "$lib/state/imagePolicy";

  interface Props {
    /** Any URL: only one imagePolicy allows is loaded, anything else draws as its alt text. */
    src: string;
    alt?: string;
  }

  let { src, alt = "" }: Props = $props();

  const policy = $derived(imagePolicy(src));
  const label = $derived(alt.trim() || "Generated image");
  let failed = $state(false);
  let viewer = $state<HTMLDialogElement | null>(null);
  /* The full-size copy is drawn only while the viewer is open, so a closed one decodes nothing. */
  let viewing = $state(false);

  function openViewer(): void {
    viewing = true;
    viewer?.showModal();
  }
</script>

<!-- `nodrag nopan nowheel`: inside a graph card, a press or a scroll here is the image's, not the canvas's. -->
{#if policy?.status === "ok" && !failed}
  <span class="safe-image">
    <button
      type="button"
      class="frame nodrag nopan"
      aria-haspopup="dialog"
      aria-label={`Open full size: ${label}`}
      onclick={openViewer}
    >
      <img src={policy.src} alt={label} loading="lazy" decoding="async" onerror={() => (failed = true)} />
    </button>
    <!-- The body fills the dialog, so a click on the dialog itself came from the backdrop. -->
    <dialog
      bind:this={viewer}
      class="viewer nodrag nopan nowheel"
      aria-label={label}
      onclose={() => (viewing = false)}
      onclick={(event) => {
        if (event.target === viewer) viewer?.close();
      }}
    >
      <div class="viewer-head">
        <span class="viewer-title" dir="auto">{label}</span>
        <IconButton label="Close image" onclick={() => viewer?.close()}><X size={14} /></IconButton>
      </div>
      {#if viewing}
        <img class="full" src={policy.src} alt={label} decoding="async" />
      {/if}
    </dialog>
  </span>
{:else if policy}
  <span class="image-note">
    <strong>{policy.status === "too-large" ? `Image too large to preview (${formatImageBytes(policy.bytes)})` : "Image unavailable"}</strong>
    <span dir="auto">{label}</span>
  </span>
{:else}
  <span dir="auto">{label}</span>
{/if}

<style>
  .safe-image {
    display: block;
    max-width: 100%;
  }

  /* Sized by the image, capped by the token, so nothing below moves more than the cap. */
  .frame {
    display: inline-flex;
    max-width: 100%;
    padding: 0;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface-0);
    cursor: zoom-in;
  }

  .frame:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .frame img {
    display: block;
    max-width: 100%;
    max-height: var(--image-max-height);
    width: auto;
    height: auto;
    object-fit: contain;
  }

  .image-note {
    display: inline-flex;
    flex-wrap: wrap;
    gap: var(--gap-xs) var(--gap-sm);
    max-width: 100%;
    padding: var(--gap-xs) var(--gap-sm);
    border: 1px dashed var(--border-strong);
    border-radius: var(--radius-sm);
    color: var(--text-2);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }

  .image-note strong {
    color: var(--text-1);
    font-weight: 650;
  }

  .viewer {
    max-width: calc(100vw - var(--gutter) * 4);
    max-height: calc(100vh - var(--gutter) * 4);
    padding: 0;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-lg);
    background: var(--surface-2);
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
    overflow: auto;
  }

  .viewer::backdrop {
    background: var(--overlay-scrim);
  }

  .viewer-head {
    position: sticky;
    top: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-md);
    padding: var(--gap-xs) var(--gap-xs) var(--gap-xs) var(--gutter);
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }

  .viewer-title {
    min-width: 0;
    overflow: hidden;
    font-size: var(--font-md);
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .full {
    display: block;
    max-width: calc(100vw - var(--gutter) * 4 - 2px);
    max-height: calc(100vh - var(--gutter) * 4 - var(--head-height));
    margin: auto;
    object-fit: contain;
  }
</style>
