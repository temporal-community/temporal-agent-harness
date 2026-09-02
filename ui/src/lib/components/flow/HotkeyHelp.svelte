<script lang="ts">
  import { REPLAY_BINDINGS } from "$lib/state/replayHotkeys";

  interface Props {
    open: boolean;
    onClose: () => void;
  }

  let { open, onClose }: Props = $props();

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
      <h2 id="hotkey-help-title">Replay shortcuts</h2>
      <dl>
        {#each REPLAY_BINDINGS as binding (binding.action)}
          <div class="row">
            <dt><kbd>{binding.chord}</kbd></dt>
            <dd>{binding.label}</dd>
          </div>
        {/each}
      </dl>
      <p class="note">Keys stay quiet while you are typing in a message or on a form field.</p>
    </div>
  </div>
{/if}

<style>
  .scrim {
    position: fixed;
    inset: 0;
    z-index: 60;
    display: grid;
    place-items: center;
    padding: 24px;
    background: rgb(0 0 0 / 0.5);
  }

  .sheet {
    width: min(420px, 100%);
    max-height: 100%;
    overflow: auto;
    padding: 18px 20px;
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    background: var(--surface-2);
    box-shadow: var(--shadow-popover);
    animation: rise 140ms ease;
  }

  h2 {
    margin: 0 0 14px;
    color: var(--text-1);
    font-size: 13px;
    font-weight: 650;
  }

  dl {
    margin: 0;
    display: grid;
    gap: 2px;
  }

  .row {
    display: grid;
    grid-template-columns: 92px minmax(0, 1fr);
    gap: 12px;
    align-items: center;
    padding: 5px 6px;
    border-radius: 6px;
  }

  .row:nth-child(odd) {
    background: rgb(255 255 255 / 0.02);
  }

  dt {
    margin: 0;
  }

  kbd {
    display: inline-block;
    padding: 2px 7px;
    border: 1px solid var(--border-strong);
    border-radius: 5px;
    background: var(--control-bg);
    box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.05);
    color: var(--text-1);
    font-family: inherit;
    font-size: 11px;
    font-weight: 650;
    white-space: nowrap;
  }

  dd {
    margin: 0;
    min-width: 0;
    color: var(--text-2);
    font-size: 12px;
  }

  .note {
    margin: 14px 0 0;
    color: var(--text-3);
    font-size: 11px;
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
