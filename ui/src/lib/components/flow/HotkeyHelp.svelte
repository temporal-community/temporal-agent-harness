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

  /* Escape is bound globally alongside every other replay key, so this surface
     does not repeat the handler — it only has to close on the scrim. */
  function handleScrimClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) onClose();
  }
</script>

{#if open}
  <!-- The scrim closes on click and nothing else; Escape reaches it through the
       same window handler that opened it. -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="scrim" onclick={handleScrimClick}>
    <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="hotkey-help-title">
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
        Every key here stays quiet while you are typing in a message or on a form field, so
        Option and the arrows keep meaning what they mean in a text field.
      </p>
    </div>
  </div>
{/if}

<style>
  /* Written on main, where neither the token layer nor `data-tip` existed, so
     every value below was a raw pixel. Retokenized onto the console's scale:
     the square corners are not a simplification, the whole app sets its radii
     to 0. */
  .scrim {
    position: fixed;
    inset: 0;
    z-index: 60;
    display: grid;
    place-items: center;
    padding: calc(var(--gutter) * 2);
    background: var(--overlay-scrim);
  }

  .sheet {
    width: min(420px, 100%);
    max-height: 100%;
    overflow: auto;
    padding: var(--gutter);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-lg);
    background: var(--surface-2);
    box-shadow: var(--shadow-modal);
    animation: rise var(--duration-fast) var(--ease-out);
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
    background: rgb(255 255 255 / 0.02);
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
