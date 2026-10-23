<script lang="ts" module>
  export interface EndTarget {
    workflowId: string;
    /** What the reader called it where they asked: the agent and the session's title. */
    name: string;
    /** Where focus goes when the dialog closes, whichever way it closes. */
    trigger: HTMLElement | null;
  }
</script>

<script lang="ts">
  import Chip from "$lib/components/primitives/Chip.svelte";

  interface Props {
    /** The session to confirm ending; null keeps the dialog shut. */
    target: EndTarget | null;
    /** Rejects with the reason the session could not be ended, which is shown here. */
    onConfirm: (workflowId: string) => Promise<void>;
    onClose: () => void;
  }

  let { target, onConfirm, onClose }: Props = $props();

  const id = $props.id();
  let dialogElement = $state<HTMLDialogElement | null>(null);
  let ending = $state(false);
  let failure = $state<string | null>(null);

  /* showModal() for the top layer's focus trap and Escape. Cancel takes focus first: the safe
     answer is the one Enter gives. */
  $effect(() => {
    const element = dialogElement;
    if (!element) return;
    if (target && !element.open) {
      element.showModal();
      element.querySelector<HTMLElement>(".end-cancel")?.focus();
    }
    if (!target && element.open) element.close();
  });

  async function confirm(): Promise<void> {
    if (!target || ending) return;
    ending = true;
    failure = null;
    try {
      await onConfirm(target.workflowId);
      dialogElement?.close();
    } catch (error) {
      failure = error instanceof Error ? error.message : "Could not end the session.";
    } finally {
      ending = false;
    }
  }

  /* The trigger is named rather than left to the browser: a menu item that asked is gone by now. */
  function handleClose(): void {
    const trigger = target?.trigger;
    failure = null;
    onClose();
    if (trigger?.isConnected) trigger.focus();
  }
</script>

<dialog
  bind:this={dialogElement}
  class="end-dialog"
  aria-labelledby={`${id}-title`}
  aria-describedby={`${id}-body`}
  onclose={handleClose}
  oncancel={(event) => {
    if (ending) event.preventDefault();
  }}
>
  {#if target}
    <h2 id={`${id}-title`}>End this session?</h2>
    <p class="end-name">{target.name}</p>
    <p id={`${id}-body`}>The agent stops and you can't send to it again. Its history stays viewable.</p>
    {#if failure}
      <p class="end-failure" role="alert">{failure}</p>
    {/if}
    <div class="end-actions">
      <Chip class="end-cancel" size="sm" fill="quiet" onclick={() => dialogElement?.close()}>Cancel</Chip>
      <Chip class="end-confirm" size="sm" tone="error" toned disabled={ending} onclick={() => void confirm()}>
        {ending ? "Ending…" : "End session"}
      </Chip>
    </div>
  {/if}
</dialog>

<style>
  .end-dialog {
    width: min(420px, calc(100% - var(--gutter) * 4));
    max-height: calc(100% - var(--gutter) * 4);
    overflow: auto;
    padding: var(--gutter);
    border: 1px solid color-mix(in srgb, var(--error) 45%, var(--border-strong));
    border-radius: var(--radius-lg);
    background: var(--surface-2);
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
  }

  .end-dialog::backdrop {
    background: var(--overlay-scrim);
  }

  h2 {
    margin: 0 0 var(--gap-sm);
    font-size: var(--font-lg);
    font-weight: 650;
  }

  p {
    margin: 0 0 var(--gutter-tight);
    color: var(--text-2);
    font-size: var(--font-md);
    overflow-wrap: anywhere;
  }

  /* A pasted transcript as a first message would otherwise make the dialog a page. */
  .end-name {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    overflow: hidden;
    color: var(--text-1);
    font-weight: 600;
  }

  .end-failure {
    color: var(--error);
  }

  .end-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--gap-sm);
  }
</style>
