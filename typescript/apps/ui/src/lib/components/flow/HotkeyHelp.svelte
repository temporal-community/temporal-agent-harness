<script lang="ts">
  import { REPLAY_BINDINGS, type ReplayScope } from "$lib/state/replayHotkeys";

  interface Props {
    open: boolean;
    onClose: () => void;
  }

  let { open, onClose }: Props = $props();

  /* Two sections, because the table now holds two kinds of key and twenty
     undifferentiated rows is a list nobody reads to the end of. Derived from the
     bindings rather than listed here, so a new scope cannot go unrendered. */
  const SCOPE_TITLE: Record<ReplayScope, string> = {
    replay: "Moving through the run",
    rail: "Moving around the desk"
  };
  const sections = Object.entries(SCOPE_TITLE).map(([scope, title]) => ({
    title,
    bindings: REPLAY_BINDINGS.filter((binding) => binding.scope === scope)
  }));

  let sheetElement = $state<HTMLDialogElement | null>(null);

  /* showModal(), the way the node inspector does it. This sheet used to be a div
     wearing `role="dialog" aria-modal="true"` inside a hand-rolled scrim, which
     told a screen reader to ignore everything behind it while Tab walked out into
     the app anyway — the claim without the containment. The top layer is what
     grants the trap, and it brings Escape and a real ::backdrop with it.
     Escape stays bound globally too, because the resolver is what decides whether
     this sheet or a full-screen pane owns the press.
     Both directions run through here, and the dialog stays mounted, so every way
     out ends at the native `close` event: the resolver's Escape, the `?` toggle,
     the backdrop, and the browser's own Escape. Rendering this under `{#if open}`
     instead would unmount an *open* dialog, which is the one exit that skips
     `close` — and with it the focus restore, stranding the keyboard on <body>. */
  $effect(() => {
    const element = sheetElement;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  });
</script>

<!-- The scrim is the dialog's own ::backdrop, so nothing sits between the app and
     the sheet to catch the click. A click landing on the dialog itself can only
     have come from the backdrop, because the body fills it edge to edge. -->
<dialog
  bind:this={sheetElement}
  class="sheet"
  aria-labelledby="hotkey-help-title"
  onclose={onClose}
  onclick={(event) => {
    if (event.target === sheetElement) sheetElement?.close();
  }}
>
  <h2 id="hotkey-help-title">Keyboard shortcuts</h2>
  {#each sections as section (section.title)}
    <h3>{section.title}</h3>
    <dl>
      {#each section.bindings as binding (binding.action)}
        <div class="row">
          <dt><kbd>{binding.chord}</kbd></dt>
          <dd>{binding.label}</dd>
        </div>
      {/each}
    </dl>
  {/each}
  <p class="note">
    Every key here stays quiet while you are typing in a message or on a form field, so Option and
    the arrows keep meaning what they mean in a text field.
  </p>
</dialog>

<style>
  /* Written on main, where neither the token layer nor `data-tip` existed, so
     every value below was a raw pixel. Retokenized onto the console's scale:
     the square corners are not a simplification, the whole app sets its radii
     to 0. */
  /* No .scrim rule and no z-index: showModal() puts the sheet in the top layer,
     which is above every stacking context by definition, and paints the scrim as
     ::backdrop. The gutter the scrim used to hold as padding is subtracted here
     instead, because a modal dialog centres itself on `margin: auto`. */
  .sheet {
    width: min(420px, calc(100% - var(--gutter) * 4));
    max-height: calc(100% - var(--gutter) * 4);
    overflow: auto;
    padding: var(--gutter);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-lg);
    background: var(--surface-2);
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
    animation: rise var(--duration-fast) var(--ease-out);
  }

  .sheet::backdrop {
    background: var(--overlay-scrim);
  }

  h2 {
    margin: 0 0 var(--gutter);
    color: var(--text-1);
    font-size: var(--font-lg);
    font-weight: 650;
  }

  h3 {
    margin: var(--gutter) 0 var(--gap-xs);
    color: var(--text-3);
    font-size: var(--font-sm);
    font-weight: 650;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  h3:first-of-type {
    margin-top: 0;
  }

  dl {
    margin: 0;
    display: grid;
    gap: var(--gap-2xs);
  }

  .row {
    display: grid;
    /* Wide enough for the longest chord in the table, "Cmd Shift ←". */
    grid-template-columns: 116px minmax(0, 1fr);
    gap: var(--gap-lg);
    align-items: center;
    padding: var(--gap-xs) var(--gap-sm);
    border-radius: var(--radius-sm);
  }

  .row:nth-child(odd) {
    background: color-mix(in srgb, var(--text-1) 2%, transparent);
  }

  dt {
    margin: 0;
  }

  /* No inset highlight: the elevation layer here has no blooms in it, every
     --shadow-inset-* token is none. */
  kbd {
    display: inline-block;
    padding: var(--gap-2xs) var(--gap-sm);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-xs);
    background: var(--control-bg);
    color: var(--text-1);
    font-family: inherit;
    font-size: var(--font-sm);
    font-weight: 650;
    white-space: nowrap;
  }

  dd {
    margin: 0;
    min-width: 0;
    color: var(--text-2);
    font-size: var(--font-md);
  }

  .note {
    margin: var(--gutter) 0 0;
    color: var(--text-3);
    font-size: var(--font-sm);
    line-height: 1.45;
  }

  @keyframes rise {
    from {
      opacity: 0;
      transform: translateY(4px) scale(0.98);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .sheet {
      animation: none;
    }
  }
</style>
