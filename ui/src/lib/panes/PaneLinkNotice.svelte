<script lang="ts">
  /**
   * What the desk says about a link that asked for a pane it does not have.
   *
   * One line under the status line, in the chrome rather than in a pane: the pane
   * a bad token was meant to be is by definition not on screen, so there is
   * nowhere inside the rail this could reliably be read. A strip that only
   * exists while there is something wrong with the link costs the desk no
   * permanent chrome.
   *
   * It states the token as written and, when the token is a typo away from
   * exactly one kind, what it was probably reaching for. The guess is never
   * acted on — the reader compares it against what they typed, which is the one
   * thing that tells "you asked for a pane that does not exist" apart from "a
   * pane broke".
   */
  import Chip from "$lib/components/primitives/Chip.svelte";
  import type { UnknownPaneReport } from "$lib/state/paneStack.svelte";

  interface Props {
    report: UnknownPaneReport;
    onDismiss: () => void;
  }

  let { report, onDismiss }: Props = $props();

  const outcome = $derived(
    report.fellBack
      ? "Nothing the link named exists, so this is the default desk."
      : "The rest of the link opened."
  );
</script>

<div class="link-notice" role="status">
  <span class="kicker">Link</span>

  <p class="said">
    <!-- The spaces are written as expressions because Svelte trims whitespace at
         the edge of a block, and "pane— did you mean" is a line a reader would
         take for a bug of its own. -->
    {#each report.tokens as token, index (token.written)}
      {#if index > 0}{" "}{/if}<code>{token.written}</code> is not a pane{#if token.meant}{" — did you mean "}<code
          >{token.meant}</code
        >?{:else}.{/if}
    {/each}
    <span class="outcome">{outcome}</span>
  </p>

  <Chip label="Got it" size="xs" fill="bare" onclick={onDismiss} />
</div>

<style>
  /* The status line's row, one step quieter: same inset, same hairline under it,
     so the two read as one frame rather than as a band that arrived. */
  .link-notice {
    flex: none;
    display: flex;
    align-items: center;
    gap: var(--gutter-tight);
    padding: var(--gap-sm) var(--gutter);
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
    opacity: 1;
    transform: none;
    transition:
      opacity var(--duration-fast) var(--ease-out),
      transform var(--duration-fast) var(--ease-out);
  }

  /* It arrives with the page and pushes the rail down, so it fades and settles
     rather than snapping in under a desk the reader is already reading. */
  @starting-style {
    .link-notice {
      opacity: 0;
      transform: translateY(-3px);
    }
  }

  .said {
    flex: 1;
    min-width: 0;
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-md);
    line-height: 1.5;
    text-wrap: pretty;
  }

  /* The tokens are the part to be compared against a link, so they are set the
     way every identifier in the console is. */
  code {
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--figure-size);
  }

  .outcome {
    color: var(--text-4);
  }

  @media (prefers-reduced-motion: reduce) {
    /* Still fades — it just stops travelling. */
    @starting-style {
      .link-notice {
        opacity: 0;
        transform: none;
      }
    }
  }
</style>
