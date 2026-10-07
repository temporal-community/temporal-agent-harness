<script lang="ts">
  /**
   * Asks for an agent's init data before its session starts: the data model's form, generated
   * from its JSON Schema like a handler's input. Required data must be filled in; optional data
   * can be skipped.
   */
  import type { AgentDescriptor, JsonRecord } from "$lib/api/types";
  import SchemaForm from "$lib/components/chat/SchemaForm.svelte";
  import { problemSummary } from "$lib/components/chat/schemaForm";
  import { initDataForm, initDataPayload, type InitDataForm } from "$lib/state/initData";

  interface Props {
    /** The agent being started; `null` keeps the dialog closed. */
    agent: AgentDescriptor | null;
    starting?: boolean;
    onStart: (workflowType: string, data?: JsonRecord) => void | Promise<void>;
    onClose: () => void;
  }

  let { agent, starting = false, onStart, onClose }: Props = $props();

  let dialogElement = $state<HTMLDialogElement | null>(null);
  // Built from the first agent right away, so the first paint already has the form; the effect
  // below replaces it whenever the agent changes.
  // svelte-ignore state_referenced_locally
  let form = $state<InitDataForm | null>(agent ? initDataForm(agent) : null);
  /* Keyed by field path, so each field shows its own; the summary line counts them. */
  let problems = $state<Record<string, string>>({});
  let error = $state<string | null>(null);
  const alert = $derived(problemSummary(problems) ?? error);
  let formFor: AgentDescriptor | null = null;

  /* A fresh form per agent, so a half-filled one never carries over to the next start. */
  $effect(() => {
    if (agent === formFor) return;
    formFor = agent;
    form = agent ? initDataForm(agent) : null;
    problems = {};
    error = null;
  });

  /* The dialog stays mounted and `agent` drives it, so every way out (Cancel, Escape, the
     backdrop) ends at the native `close` event and its focus restore, as in HotkeyHelp. */
  $effect(() => {
    const element = dialogElement;
    if (!element) return;
    if (agent && !element.open) element.showModal();
    if (!agent && element.open) element.close();
  });

  async function start(withData: boolean): Promise<void> {
    if (!agent || starting) return;
    if (!withData) {
      await onStart(agent.workflow_type);
      return;
    }
    if (!form) return;
    const result = initDataPayload(form);
    problems = "problems" in result ? result.problems : {};
    error = "error" in result ? result.error : null;
    if (!("data" in result)) return;
    await onStart(agent.workflow_type, result.data);
  }
</script>

<dialog
  bind:this={dialogElement}
  class="start-sheet"
  aria-labelledby="start-session-title"
  onclose={onClose}
  onclick={(event) => {
    if (event.target === dialogElement) dialogElement?.close();
  }}
>
  {#if agent && form}
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void start(true);
      }}
    >
      <h2 id="start-session-title">Start {agent.label}</h2>
      <p class="lede">
        {form.required ? "This agent needs" : "This agent can take"}
        <strong>{form.title}</strong> to start{form.required ? "." : ", or none."}
        {#if form.description}<span class="about">{form.description}</span>{/if}
      </p>
      <SchemaForm
        fields={form.fields}
        bind:values={form.values}
        disabled={starting}
        idPrefix={`init-${agent.key}`}
        errors={problems}
      />
      {#if alert}
        <p class="form-error" role="alert">{alert}</p>
      {/if}
      <div class="actions">
        <button type="button" class="secondary" onclick={() => dialogElement?.close()}>
          Cancel
        </button>
        {#if !form.required}
          <button
            type="button"
            class="secondary"
            disabled={starting}
            onclick={() => void start(false)}
          >
            Start without
          </button>
        {/if}
        <button type="submit" class="primary" disabled={starting}>Start session</button>
      </div>
    </form>
  {/if}
</dialog>

<style>
  .start-sheet {
    width: min(460px, calc(100% - var(--gutter) * 4));
    max-height: calc(100% - var(--gutter) * 4);
    overflow: auto;
    padding: var(--gutter);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-lg);
    background: var(--surface-2);
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
  }

  .start-sheet::backdrop {
    background: var(--overlay-scrim);
  }

  h2 {
    margin: 0 0 var(--gap-sm);
    font-size: var(--font-lg);
    font-weight: 650;
  }

  .lede {
    margin: 0 0 var(--gutter);
    color: var(--text-2);
    font-size: var(--font-md);
    line-height: 1.45;
  }

  .about {
    display: block;
    margin-top: var(--gap-xs);
    color: var(--text-3);
  }

  .form-error {
    margin: 0 0 9px;
    color: var(--error);
    font-size: var(--font-sm);
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--gap-sm);
    margin-top: var(--gutter);
  }

  button {
    padding: 6px 13px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-md);
    background: var(--control-bg);
    color: var(--text-1);
    font: inherit;
    font-size: var(--font-md);
    cursor: pointer;
  }

  .primary {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-2));
    font-weight: 680;
  }

  @media (hover: hover) and (pointer: fine) {
    .primary:hover:not(:disabled) {
      background: color-mix(in srgb, var(--accent) 26%, var(--surface-2));
    }
  }

  button:disabled {
    cursor: default;
    opacity: var(--disabled-opacity);
  }
</style>
