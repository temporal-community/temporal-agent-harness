<script lang="ts" module>
  export type CallbackOutcome = { result: unknown } | { error: string };
  /** The error a Decline answers with, which is how a turn's summary tells it from a failure. */
  export const DECLINED = "Declined by the user.";
</script>

<script lang="ts">
  import { Check, CircleX } from "@lucide/svelte";
  import { tick, untrack } from "svelte";
  import { prefersReducedMotion } from "svelte/motion";
  import { fly } from "svelte/transition";
  import { callbackAlreadyResolved } from "$lib/api/httpClient";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import SchemaForm from "$lib/components/chat/SchemaForm.svelte";
  import { buildPayload, emptyValues, resultForm, validate, type SchemaField } from "@temporalio/agent-harness-client";
  import { focusFirstProblem, problemSummary } from "$lib/components/chat/schemaForm";
  import type { ReplayLogRow } from "$lib/state/replayLog";
  import PendingCard from "./PendingCard.svelte";
  import QueuePager from "./QueuePager.svelte";

  interface Props {
    row: ReplayLogRow;
    /** Requested past the replay cursor: live state the agent is blocked on now. */
    ahead?: boolean;
    onSubmit?: (outcome: CallbackOutcome) => void | Promise<void>;
  }

  let { row, ahead = false, onSubmit }: Props = $props();

  const formId = $props.id();
  const title = $derived(row.toolName || "Callback");
  /* Long enough to see a pick land before the next question replaces it. */
  const ADVANCE_MS = 480;

  /* The parent keys each card by its call, so the form is built once per call. */
  const form = untrack(() => resultForm(row.outputSchema ?? {}));
  /* One question at a time. */
  const last = form.fields.length - 1;
  /* A required choice starts unanswered: a preselected option is an answer nobody gave. */
  let values = $state({
    ...emptyValues(form.fields),
    ...Object.fromEntries(form.fields.filter((f) => f.kind === "enum" && (f.required || f.default === undefined)).map((f) => [f.name, ""]))
  });
  /* "Something else…" by question, null until picked. A choice cannot carry free text, so an
     answer in the reader's own words goes back as the call's error, as a note. */
  let other = $state<Record<string, string | null>>({});
  let step = $state(0);
  let forward = $state(true);
  let formEl: HTMLFormElement | undefined;
  let advanceTimer: ReturnType<typeof setTimeout> | undefined;
  const field = $derived(form.fields[step]!);
  let submitting = $state(false);
  let sent = $state(false);
  let settledElsewhere = $state(false);
  let error = $state<string | null>(null);
  /* Marks follow the values once Send was pressed, so a fixed field clears as it is fixed. */
  let attempted = $state(false);
  const problems = $derived.by(() => {
    if (!attempted) return {};
    const found = validate(form.fields.filter((f) => other[f.name] == null), values);
    for (const f of form.fields) if (other[f.name]?.trim() === "") found[f.name] = "Say what instead, or pick an option.";
    return found;
  });
  const alert = $derived(problemSummary(problems) ?? error);

  const locked = $derived(!onSubmit || submitting || sent || settledElsewhere);
  const sendLabel = $derived(submitting ? "Sending…" : "Send");

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

  async function goTo(next: number): Promise<void> {
    clearTimeout(advanceTimer);
    const hadFocus = formEl?.querySelector(".step")?.contains(document.activeElement) ?? false;
    forward = next > step;
    step = next;
    await tick();
    if (hadFocus) formEl?.querySelector<HTMLElement>(".step :is(input, select, textarea)")?.focus();
  }

  /* Send answers every question; one left unanswered is shown first. */
  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    attempted = true;
    error = null;
    const first = form.fields.findIndex((f) => problems[f.name]);
    if (first >= 0) {
      await goTo(first);
      void focusFirstProblem(formEl);
      return;
    }
    if (form.fields.some((f) => other[f.name] != null)) {
      const answer = (f: SchemaField) => other[f.name] != null ? `${other[f.name]!.trim()} (none of the options)` : [values[f.name]].flat().join(", ");
      void send({ error: `Answered outside the options. ${form.fields.map((f) => `${f.title}: ${answer(f)}`).join("; ")}` });
      return;
    }
    let payload;
    try {
      payload = buildPayload(form.fields, values);
    } catch (failure) {
      error = failure instanceof Error ? failure.message : "Could not build the response.";
      return;
    }
    void send({ result: form.wrapped ? payload.result : payload });
  }

  /* Only a click or tap on a single choice moves on: arrow keys walk a radio group, and each
     step of the walk is a change too. Never from the last question. Enter in a field before
     the last moves on rather than sending. */
  function track(node: HTMLFormElement) {
    formEl = node;
    let pointerPick = false;
    const set = (event: Event) => (pointerPick = event.type === "pointerdown");
    const enter = (event: KeyboardEvent) => {
      if (event.key !== "Enter" || step >= last || !(event.target instanceof HTMLInputElement)) return;
      event.preventDefault();
      void goTo(step + 1);
    };
    const picked = (event: Event) => {
      clearTimeout(advanceTimer);
      const input = event.target as HTMLInputElement;
      if (pointerPick && input.type === "radio" && !input.hasAttribute("data-other") && step < last) {
        advanceTimer = setTimeout(() => void goTo(step + 1), ADVANCE_MS);
      }
    };
    node.addEventListener("pointerdown", set, true);
    node.addEventListener("keydown", set, true);
    node.addEventListener("keydown", enter);
    node.addEventListener("change", picked);
    return () => {
      clearTimeout(advanceTimer);
      node.removeEventListener("pointerdown", set, true);
      node.removeEventListener("keydown", set, true);
      node.removeEventListener("keydown", enter);
      node.removeEventListener("change", picked);
    };
  }
</script>

<PendingCard {row} {title} label={`Response: ${title}`} {ahead}>
  <form id={formId} onsubmit={handleSubmit} {@attach track}>
    {#if last > 0}
      <QueuePager index={step} count={last + 1} noun="question" live spelled onstep={(by) => void goTo(step + by)} />
    {/if}
    {#key step}
      <div class="step" in:fly={{ y: forward ? 12 : -12, duration: prefersReducedMotion.current ? 0 : 220 }}>
        <SchemaForm
          fields={[field]}
          bind:values
          bind:other
          choices="list"
          disabled={locked}
          idPrefix={`callback-${row.toolId ?? row.id}`}
          errors={problems}
        />
      </div>
    {/key}
  </form>
  {#snippet status()}
    {#if settledElsewhere}
      <p class="note">Already answered elsewhere</p>
    {:else if sent}
      <p class="note" role="status">Sent · waiting for the agent to continue</p>
    {:else if alert}
      <p class="error" role="alert">{alert}</p>
    {/if}
  {/snippet}
  {#snippet foot()}
    <Chip type="submit" form={formId} fill="quiet" dense label={sendLabel} aria-label={sendLabel} data-tip={sendLabel} disabled={locked}>
      {#snippet lead()}<Check size={12} />{/snippet}
    </Chip>
    <Chip tone="error" fill="quiet" toned dense label="Decline" aria-label="Decline" data-tip="Decline" disabled={locked} onclick={() => void send({ error: DECLINED })}>
      {#snippet lead()}<CircleX size={12} />{/snippet}
    </Chip>
  {/snippet}
</PendingCard>

<style>
  form,
  .step {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }

  form {
    gap: 6px;
  }

  /* The question's own pager, at the start of the form: "⌃ Question 2 of 3 ⌄". */
  form > :global(.pager) {
    margin-inline-start: -6px;
  }

  /* The last field's own spacing would double the card's gap above the footer. */
  .step :global(.schema-field:last-child) {
    margin-bottom: 0;
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
