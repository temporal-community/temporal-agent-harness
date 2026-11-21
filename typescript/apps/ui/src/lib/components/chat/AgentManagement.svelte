<script lang="ts">
  import type { AgentDescriptor } from "$lib/api/types";
  import Chip from "$lib/components/primitives/Chip.svelte";

  interface Props {
    agent: AgentDescriptor;
    busy?: boolean;
    /** Delete was asked once: the button now asks for the second, consequential press. */
    confirmingDelete?: boolean;
    failure?: string | null;
    onEdit: () => void;
    onRetire: () => void;
    onRestore: () => void;
    onDelete: () => void;
  }

  let { agent, busy = false, confirmingDelete = false, failure = null, onEdit, onRetire, onRestore, onDelete }: Props = $props();

  const uid = $props.id();
</script>

<section class="manage" aria-labelledby={`${uid}-manage`}>
  <h4 class="kicker" id={`${uid}-manage`}>Manage</h4>
  <div class="manage-actions">
    {#if agent.retired}
      <Chip size="xs" fill="quiet" disabled={busy} onclick={onRestore}>Restore</Chip>
    {:else}
      <Chip size="xs" fill="quiet" disabled={busy} onclick={onEdit}>Edit</Chip>
      <Chip size="xs" fill="quiet" disabled={busy} onclick={onRetire}>Retire</Chip>
    {/if}
    <Chip size="xs" fill={confirmingDelete ? "solid" : "quiet"} tone="error" disabled={busy} onclick={onDelete}>
      {confirmingDelete ? "Delete, and drop its sessions from the list" : "Delete"}
    </Chip>
  </div>
  {#if agent.retired}
    <p class="note">Retired: its sessions are still listed and usable, but it starts no new ones until it is restored.</p>
  {/if}
  {#if failure}<p class="form-error" role="alert">{failure}</p>{/if}
</section>

<style>
  .manage {
    display: grid;
    gap: var(--gap-xs);
    margin-top: var(--gap-sm);
  }

  h4, p {
    margin: 0;
  }

  .manage-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-xs);
  }

  .note {
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.5;
  }

  .form-error {
    color: var(--error);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }
</style>
