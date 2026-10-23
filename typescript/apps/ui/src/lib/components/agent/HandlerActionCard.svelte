<script lang="ts">
  /**
   * A handler that takes a form, offered in the attention queue while the run is idle: the same
   * card as a callback's questions, its fields from the handler's input schema and Send in the
   * sticky footer. Several form handlers are one card with a switcher, not a card each, since
   * the run is waiting on one input from the reader, not on all of them. The switcher and its
   * list are the composer's own handler control and rows, handed in by the chat panel.
   */
  import { Check, CircleX } from "@lucide/svelte";
  import { untrack, type Snippet } from "svelte";
  import { buildPayload, describeSchema, emptyValues, validate } from "@temporalio/agent-harness-client";
  import type { AgentInterfaceFunction, JsonRecord } from "$lib/api/types";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import SchemaForm from "$lib/components/chat/SchemaForm.svelte";
  import { focusFirstProblem, problemSummary } from "$lib/components/chat/schemaForm";
  import type { ReplayLogRow } from "$lib/state/replayLog";
  import PendingCard from "./PendingCard.svelte";

  interface Props {
    row: ReplayLogRow;
    when: string;
    handler: AgentInterfaceFunction;
    /** The title's place when there are other handlers to switch to; told when the card is
        locked by a send. */
    switcher?: Snippet<[boolean]>;
    /** The switcher's list, under the title. */
    menu?: Snippet;
    /** Resolves false when the send failed; the card keeps what was typed. */
    onsend?: (payload: JsonRecord) => Promise<boolean>;
    /** Offered when the card was opened by a pick rather than by an idle run. */
    oncancel?: () => void;
  }

  let { row, when, handler, switcher, menu, onsend, oncancel }: Props = $props();

  const formId = $props.id();
  /* The parent keys the card by handler, so the form is built once per handler. */
  const fields = untrack(() => describeSchema(handler.parameters));
  let values = $state(untrack(() => emptyValues(fields)));
  let attempted = $state(false);
  let submitting = $state(false);
  let sent = $state(false);
  let error = $state<string | null>(null);
  let formEl: HTMLFormElement | undefined;
  const problems = $derived(attempted ? validate(fields, values) : {});
  const alert = $derived(problemSummary(problems) ?? error);
  const locked = $derived(!onsend || submitting || sent);
  const sendLabel = $derived(submitting ? "Sending…" : "Send");

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (locked) return;
    attempted = true;
    error = null;
    if (Object.keys(problems).length > 0) return focusFirstProblem(formEl);
    let payload: JsonRecord;
    try {
      payload = buildPayload(fields, values);
    } catch (failure) {
      error = failure instanceof Error ? failure.message : "Could not build the message.";
      return;
    }
    /* Sending locks the field that has focus; the reader's place parks on the card that holds
       it, as a pressed decision's does, rather than dropping to the page. */
    const holder = formEl?.contains(document.activeElement) ? formEl.closest<HTMLElement>('[tabindex="-1"]') : null;
    submitting = true;
    holder?.focus({ preventScroll: true });
    try {
      sent = await onsend!(payload);
      if (!sent) error = `Could not send ${handler.name}.`;
    } finally {
      submitting = false;
    }
  }
</script>

{#snippet heading()}{@render switcher?.(submitting || sent)}{/snippet}

<PendingCard
  {row}
  title={handler.name}
  label={`Action: ${handler.name}`}
  {when}
  heading={switcher ? heading : undefined}
>
  {@render menu?.()}
  <form id={formId} novalidate onsubmit={handleSubmit} {@attach (node) => void (formEl = node)}>
    <SchemaForm {fields} bind:values disabled={locked} idPrefix={`action-${handler.name}`} errors={problems} />
  </form>
  {#snippet status()}
    {#if sent}
      <p class="note" role="status">Sent · waiting for the agent to continue</p>
    {:else if alert}
      <p class="error" role="alert">{alert}</p>
    {/if}
  {/snippet}
  {#snippet foot()}
    {#if oncancel}
      <Chip fill="quiet" dense label="Cancel" aria-label="Cancel" data-tip="Cancel" disabled={submitting} onclick={oncancel}>
        {#snippet lead()}<CircleX size={12} />{/snippet}
      </Chip>
    {/if}
    <Chip type="submit" form={formId} fill="quiet" dense label={sendLabel} aria-label={`Send ${handler.name}`} data-tip={`Send ${handler.name}`} disabled={locked}>
      {#snippet lead()}<Check size={12} />{/snippet}
    </Chip>
  {/snippet}
</PendingCard>

<style>
  form {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }

  /* The last field's own spacing would double the card's gap above the footer. */
  form :global(.schema-field:last-child) {
    margin-bottom: 0;
  }

  .note {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
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
