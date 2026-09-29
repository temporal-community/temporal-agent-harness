<script lang="ts" module>
  export type CallbackOutcome = { result: unknown } | { error: string };
</script>

<script lang="ts">
  import { XCircle } from "@lucide/svelte";
  import { untrack } from "svelte";
  import { callbackAlreadyResolved } from "$lib/api/httpClient";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import SchemaForm from "$lib/components/chat/SchemaForm.svelte";
  import {
    buildPayload,
    emptyValues,
    problemSummary,
    resultForm,
    validate
  } from "$lib/components/chat/schemaForm";
  import { formatTimestamp, type ReplayLogRow } from "$lib/state/replayLog";
  import CallInput from "./CallInput.svelte";

  interface Props {
    row: ReplayLogRow;
    /** Requested past the replay cursor: live state the agent is blocked on now. */
    ahead?: boolean;
    onSubmit?: (outcome: CallbackOutcome) => void | Promise<void>;
  }

  let { row, ahead = false, onSubmit }: Props = $props();

  /* The parent keys each card by its call, so the form is built once per call. */
  const form = untrack(() => resultForm(row.outputSchema ?? {}));
  let values = $state(emptyValues(form.fields));
  let submitting = $state(false);
  let sent = $state(false);
  let settledElsewhere = $state(false);
  let error = $state<string | null>(null);
  /* Marks follow the values once Send was pressed, so a fixed field clears as it is fixed. */
  let attempted = $state(false);
  const problems = $derived(attempted ? validate(form.fields, values) : {});
  const alert = $derived(problemSummary(problems) ?? error);

  const locked = $derived(!onSubmit || submitting || sent || settledElsewhere);

  async function send(outcome: CallbackOutcome): Promise<void> {
    if (locked) return;
    submitting = true;
    error = null;
    try {
      await onSubmit!(outcome);
      sent = true;
    } catch (failure) {
      if (callbackAlreadyResolved(failure)) settledElsewhere = true;
      else error = failure instanceof Error ? failure.message : "Could not send the response.";
    } finally {
      submitting = false;
    }
  }

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    attempted = true;
    error = null;
    if (Object.keys(problems).length > 0) return;
    let payload;
    try {
      payload = buildPayload(form.fields, values);
    } catch (failure) {
      error = failure instanceof Error ? failure.message : "Could not build the response.";
      return;
    }
    void send({ result: form.wrapped ? payload.result : payload });
  }
</script>

<article class="pending-callback-card" aria-label={`${row.toolName || "Callback"} is waiting for a response`}>
  <header class="head">
    <strong>{row.toolName || "Callback"}</strong>
    <span>Turn {row.turnNumber} · {formatTimestamp(row.timestamp)}</span>
  </header>
  <CallInput input={row.input} />
  {#if ahead}
    <p class="note">Requested ahead of the replay cursor · waiting on you now</p>
  {/if}
  <form onsubmit={handleSubmit}>
    <SchemaForm
      fields={form.fields}
      bind:values
      disabled={locked}
      idPrefix={`callback-${row.toolId ?? row.id}`}
      errors={problems}
    />
    <div class="actions">
      <Chip
        tone="error"
        fill="quiet"
        toned
        disabled={locked}
        title="Tell the agent this call can't be answered"
        onclick={() => void send({ error: "Declined by the user." })}
      >
        {#snippet lead()}
          <XCircle size={13} />
        {/snippet}
        Decline
      </Chip>
      <button type="submit" class="send" disabled={locked}>
        {submitting ? "Sending…" : "Send response"}
      </button>
    </div>
  </form>
  {#if settledElsewhere}
    <p class="note">Already answered elsewhere</p>
  {:else if sent}
    <p class="note" role="status">Sent · waiting for the agent to continue</p>
  {:else if alert}
    <p class="error" role="alert">{alert}</p>
  {/if}
</article>

<style>
  .pending-callback-card {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
    padding: 12px;
    border: 1px solid color-mix(in srgb, var(--queue) 35%, var(--border));
    border-radius: var(--radius-md);
    background: var(--surface-0);
  }

  .head {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: baseline;
    justify-content: space-between;
  }

  .head strong {
    min-width: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: var(--font-lg);
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .head span {
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    line-height: 1.4;
  }

  .actions {
    display: flex;
    gap: 6px;
    align-items: center;
    justify-content: flex-end;
  }

  .send {
    padding: 6px 13px;
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-2));
    color: var(--text-1);
    font: inherit;
    font-size: var(--font-md);
    font-weight: 680;
    cursor: pointer;
  }

  @media (hover: hover) and (pointer: fine) {
    .send:hover:not(:disabled) {
      background: color-mix(in srgb, var(--accent) 26%, var(--surface-2));
    }
  }

  .send:disabled {
    cursor: default;
    opacity: var(--disabled-opacity);
  }

  .note {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .error {
    max-height: 6lh;
    margin: 0;
    overflow-y: auto;
    color: var(--error);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }
</style>
