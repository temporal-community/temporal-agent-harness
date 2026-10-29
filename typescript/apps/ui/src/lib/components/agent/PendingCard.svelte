<script lang="ts">
  /**
   * One request the agent is blocked on, as the attention queue draws it: an approval and a
   * callback share this anatomy, so paging from one to the other moves nothing but the content.
   * The name and when it was asked, its arguments, then whatever the request asks (a callback's
   * questions), and a sticky footer holding the decisions, with what is said about them over
   * them. No kind glyph: the banner over the queue draws the waiting-on-you glyph and names the
   * kinds, and the decisions say which this is.
   */
  import type { Snippet } from "svelte";
  import { formatTimestamp, type ReplayLogRow } from "$lib/state/replayLog";
  import CallInput from "./CallInput.svelte";

  interface Props {
    row: ReplayLogRow;
    title: string;
    /** Names the card for assistive tech, kind first, since no glyph draws it: "Approval: send_email". */
    label: string;
    /** Requested past the replay cursor: live state the agent is blocked on now. */
    ahead?: boolean;
    /** The header's muted end; the row's turn and time unless the card was not asked in one. */
    when?: string;
    /** Drawn in the title's place, such as a control that names and switches what is asked. */
    heading?: Snippet;
    /** A quiet extra at the header's end, shown on hover or focus (always on touch). */
    action?: Snippet;
    /** Between the arguments and the footer. */
    children?: Snippet;
    /** The decisions, primary first and destructive last. */
    foot: Snippet;
    /** In the footer, over the decisions: a reason being typed, sent, decided elsewhere, or what went wrong. */
    status?: Snippet;
  }

  let { row, title, label, ahead = false, when, heading, action, children, foot, status }: Props = $props();
</script>

<article class="pending-card" aria-label={label}>
  <header class="head">
    {#if heading}
      <span class="heading">{@render heading()}</span>
    {:else}
      <strong class="fade-end" dir="auto" {title}>{title}</strong>
    {/if}
    <span class="when">{when ?? `Turn ${row.turnNumber} · ${formatTimestamp(row.timestamp)}`}</span>
    {#if action}<span class="action">{@render action()}</span>{/if}
  </header>
  <CallInput input={row.input} />
  {@render children?.()}
  {#if ahead}
    <!-- Live state, not history: the agent is blocked on this now, so it stays answerable
         however far back the cursor is. -->
    <p class="note">Requested ahead of the replay cursor · waiting on you now</p>
  {/if}
  <!-- The status rides the sticky footer, so a reason field opened on a card taller than the
       queue is in view without scrolling to it, and so is what went wrong with a decision. -->
  <div class="queue-foot">
    {#if status}<div class="status">{@render status()}</div>{/if}
    {@render foot()}
  </div>
</article>

<style>
  .pending-card {
    container: pending-card / inline-size;
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
    padding: 12px 12px 0;
  }

  /* The trace row's head: monospaced name, the time muted at the end. One height with
     or without the action, so approvals and callbacks line up when paged. */
  .head {
    min-width: 0;
    min-height: var(--control-height);
    display: flex;
    gap: 8px;
    align-items: center;
  }

  /* No basis, so a long name fades out before the time gives up a character. */
  .head strong {
    flex: 1 1 0;
    min-width: 0;
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--font-md);
    font-weight: 600;
  }

  .heading {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
  }

  /* Gives way with the name, so a narrow pane never pushes the action past the card. */
  .when {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
  }

  .action {
    display: inline-flex;
    margin-inline-end: -4px;
    opacity: 0;
    transition: opacity var(--duration-fast) var(--ease-out);
  }

  .action :global(.icon-button) {
    border-color: transparent;
    background: none;
  }

  .pending-card:hover .action,
  .pending-card:focus-within .action,
  .action:has(:global([aria-pressed="true"])) {
    opacity: 1;
  }

  @media (hover: none) {
    .action {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .action {
      transition: none;
    }
  }

  /* Edge to edge of the card's padding, the decisions at the end on one line; the wrap is for
     the status, which takes a line of its own over them. Sticky, so when a tall card scrolls
     the queue they stay in reach. */
  .queue-foot {
    position: sticky;
    bottom: 0;
    z-index: 1;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    justify-content: flex-end;
    margin-inline: -12px;
    padding: 8px 12px 12px;
    background: var(--surface-0);
  }

  /* Labelled, Approve, Always allow and Reject need about 290px inside the card. Narrower,
     each decision is its icon in the same hairline box, its word in its name and tip, so the
     row never wraps. Wide, the word is on the button and a tip would only repeat it. */
  @container pending-card (width < 320px) {
    .queue-foot :global(.chip.interactive) {
      width: var(--control-height);
      justify-content: center;
      padding: 0;
    }

    .queue-foot :global(.chip-label) {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
    }

    @media (pointer: coarse) {
      .queue-foot :global(.chip.interactive) {
        width: 32px;
        height: 32px;
      }
    }
  }

  @container pending-card (width >= 320px) {
    .queue-foot :global(.chip[data-tip]::before),
    .queue-foot :global(.chip[data-tip]::after) {
      content: none;
    }
  }

  .status {
    flex: 1 0 100%;
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 6px;
  }

  /* By element rather than `:empty`: the snippet leaves a space between its blocks. */
  .status:not(:has(*)) {
    display: none;
  }

  .note {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }
</style>
