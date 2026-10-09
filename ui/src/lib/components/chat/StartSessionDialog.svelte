<script lang="ts">
  import { Check, Plus, RefreshCw, X } from "@lucide/svelte";
  import type { AgentDescriptor, JsonRecord } from "$lib/api/types";
  import SchemaForm from "$lib/components/chat/SchemaForm.svelte";
  import { problemSummary } from "$lib/components/chat/schemaForm";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import StatusChip from "$lib/components/primitives/StatusChip.svelte";
  import { initDataForm, initDataPayload, type InitDataForm } from "$lib/state/initData";

  interface Props {
    agents: AgentDescriptor[];
    preferredAgentType?: string;
    starting?: boolean;
    refreshing?: boolean;
    error?: string | null;
    onRefresh?: () => void | Promise<void>;
    onStart: (workflowType: string, data?: JsonRecord) => void | Promise<void>;
    onClose: () => void;
  }

  let { agents, preferredAgentType, starting = false, refreshing = false, error = null, onRefresh, onStart, onClose }: Props = $props();
  // Snapshot the initial preference; re-checking workers must not replace a user's choice or form.
  // svelte-ignore state_referenced_locally
  let selectedKey = $state(agents.find((agent) => agent.workflow_type === preferredAgentType)?.key ?? (agents.length === 1 ? agents[0].key : ""));
  const agent = $derived(agents.find((item) => item.key === selectedKey) ?? null);
  let dialogElement = $state<HTMLDialogElement | null>(null);
  let submitting = $state(false);
  const busy = $derived(starting || submitting);
  // svelte-ignore state_referenced_locally
  let form = $state<InitDataForm | null>(agent ? initDataForm(agent) : null);
  // svelte-ignore state_referenced_locally
  let formKey = agent?.key ?? "";
  let problems = $state<Record<string, string>>({});
  let startError = $state<string | null>(null);
  const alert = $derived(problemSummary(problems) ?? startError);
  $effect(() => {
    if ((agent?.key ?? "") === formKey) return;
    formKey = agent?.key ?? "";
    form = agent ? initDataForm(agent) : null;
    problems = {};
    startError = null;
  });

  $effect(() => {
    if (dialogElement && !dialogElement.open) dialogElement.showModal();
  });

  function agentBlocked(agent: AgentDescriptor): boolean {
    return agent.worker?.status === "no_worker";
  }

  function close(): void {
    if (!busy) dialogElement?.close();
  }

  async function start(withData = true): Promise<void> {
    if (!agent || busy || agentBlocked(agent)) return;
    if (!withData && form?.required) return;
    let data: JsonRecord | undefined;
    if (withData && form) {
      const result = initDataPayload(form);
      problems = "problems" in result ? result.problems : {};
      startError = "error" in result ? result.error : null;
      if (!("data" in result)) return;
      data = result.data;
    }
    submitting = true;
    startError = null;
    try {
      await onStart(agent.workflow_type, data);
      dialogElement?.close();
    } catch (error) {
      startError = error instanceof Error ? error.message : "Could not start this session. Try again.";
    } finally {
      submitting = false;
    }
  }
</script>

<dialog
  bind:this={dialogElement}
  class="start-sheet"
  aria-labelledby="start-session-title"
  aria-describedby="start-session-description"
  onkeydown={(event) => event.stopPropagation()}
  onclose={onClose}
  oncancel={(event) => { if (busy) event.preventDefault(); }}
  onclick={(event) => { if (event.target === dialogElement) close(); }}
>
  <form onsubmit={(event) => { event.preventDefault(); void start(); }}>
    <header>
      <div>
        <h2 id="start-session-title">New session</h2>
        <p id="start-session-description">Choose an agent to start a fresh conversation.</p>
      </div>
      <IconButton label="Close new session" data-tip-align="end" data-tip-below disabled={busy} onclick={close}><X size={16} /></IconButton>
    </header>
    <div class="setup-body">
      <div class="choice-heading">
        <h3>Choose an agent</h3>
        {#if onRefresh}
          <button type="button" class="refresh" disabled={refreshing || busy} onclick={() => void onRefresh?.()}>
            <RefreshCw size={12} aria-hidden="true" /> {refreshing ? "Checking…" : "Re-check availability"}
          </button>
        {/if}
      </div>
      {#if error}<p class="form-error" role="alert">{error}</p>{/if}
      {#if agents.length === 0}
        <p class="empty">{refreshing ? "Loading agents…" : "No agents are registered yet."}</p>
      {:else}
        <fieldset class="agent-choices" disabled={busy}>
          <legend class="sr-only">Agent</legend>
          {#each agents as option (option.key)}
            <label class="agent-choice" class:selected={selectedKey === option.key}>
              <input type="radio" name="new-session-agent" value={option.key} bind:group={selectedKey} aria-labelledby={`agent-name-${option.key}`} aria-describedby={`agent-description-${option.key}${selectedKey === option.key && agentBlocked(option) ? " worker-hint" : ""}`} />
              <span class="agent-copy">
                <span class="agent-choice-heading">
                  <strong id={`agent-name-${option.key}`}>{option.label || option.workflow_type}</strong>
                  <StatusChip
                    label={option.worker?.status === "ready" ? "Ready" : agentBlocked(option) ? "No worker" : "Unknown"}
                    kind={option.worker?.status === "ready" ? "available" : agentBlocked(option) ? "blocked" : "idle"}
                    compact
                  />
                </span>
                <span class="agent-description" id={`agent-description-${option.key}`}>{option.description?.trim() || option.workflow_type}</span>
              </span>
              <span class="selection-mark" aria-hidden="true">{#if selectedKey === option.key}<Check size={14} />{/if}</span>
            </label>
          {/each}
        </fieldset>
      {/if}
      <div aria-live="polite">
        {#if agent && agentBlocked(agent)}
          <p class="worker-hint" id="worker-hint">Start a worker on <Copyable value={agent.task_queue} label="Copy task queue name"><code>{agent.task_queue}</code></Copyable> to use this agent, then re-check availability.</p>
        {:else if agent && agent.worker?.status !== "ready"}
          <p class="worker-hint">Worker availability could not be checked. You can still try starting this agent.</p>
        {/if}
      </div>
      {#if agent && form}
        <section class="setup-fields" aria-labelledby="setup-title">
          <h3 id="setup-title">{form.title} <span>{form.required ? "Required" : "Optional"}</span></h3>
          {#if form.description}<p>{form.description}</p>{/if}
          <SchemaForm fields={form.fields} bind:values={form.values} disabled={busy} idPrefix={`init-${agent.key}`} errors={problems} />
        </section>
      {/if}
      {#if alert}<p class="form-error" role="alert">{alert}</p>{/if}
    </div>
    <footer>
      <div class="start-details" aria-live="polite">
        <span class="start-summary" title={agent ? `Start with ${agent.label || agent.workflow_type}` : undefined}>{agent ? `Start with ${agent.label || agent.workflow_type}` : "Select an agent to continue"}</span>
        {#key selectedKey}
          <p class="start-description">{agent?.description?.trim() || ""}</p>
        {/key}
      </div>
      <div class="actions">
        <button type="button" class="secondary" disabled={busy} onclick={close}>Cancel</button>
        {#if form && !form.required}
          <button type="button" class="secondary" disabled={busy || !agent || agentBlocked(agent)} onclick={() => void start(false)}>Start without data</button>
        {/if}
        <button type="submit" class="primary" disabled={busy || !agent || agentBlocked(agent)}><Plus size={14} aria-hidden="true" /> {busy ? "Starting…" : "Start session"}</button>
      </div>
    </footer>
  </form>
</dialog>

<style>
  .start-sheet {
    width: min(800px, calc(100% - var(--gutter) * 2));
    box-sizing: border-box;
    max-height: calc(100dvh - var(--gutter) * 4);
    padding: 0;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-lg);
    background: var(--surface-1);
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
    overflow: hidden;
  }

  .start-sheet::backdrop {
    background: var(--overlay-scrim);
  }

  form {
    display: flex;
    flex-direction: column;
    max-height: inherit;
    min-width: 0;
  }

  header {
    flex: none;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--gutter);
    padding: 24px;
    border-bottom: 1px solid var(--border);
  }

  h2 {
    margin: 0 0 var(--gap-sm);
    font-size: var(--font-xl);
    font-weight: 600;
  }

  p {
    margin: 0;
    font-size: var(--font-sm);
    line-height: 1.5;
    color: var(--text-2);
  }

  .setup-body {
    padding: 24px;
    overflow-y: auto;
    overflow-x: hidden;
    min-height: 0;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .choice-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-sm);
    margin-bottom: var(--gutter);
  }

  h3 {
    margin: 0;
    font-size: var(--font-md);
    font-weight: 550;
  }

  .refresh {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 0;
    border: 0;
    background: transparent;
    color: var(--text-3);
    font-size: var(--font-2xs);
  }

  .agent-choices {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-auto-rows: 1fr;
    gap: var(--gap-sm);
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }

  .agent-choice {
    display: flex;
    min-width: 0;
    align-items: flex-start;
    gap: var(--gutter-tight);
    padding: var(--gutter);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    cursor: pointer;
    background: var(--surface-2);
    transition: border-color var(--duration-fast) var(--ease-ui), background var(--duration-fast) var(--ease-ui);
  }

  .agent-choice.selected {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 6%, var(--surface-1));
  }

  .agent-choice:focus-within {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .agent-choice input {
    flex: none;
    margin: 3px 0 0;
    accent-color: var(--accent);
  }

  .agent-copy {
    flex: 1;
    min-width: 0;
    display: grid;
    gap: var(--gap-sm);
  }

  .agent-choice-heading {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  .agent-choice strong {
    font-size: var(--font-md);
    font-weight: 550;
    overflow-wrap: anywhere;
  }

  .agent-description {
    display: block;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .selection-mark {
    display: flex;
    align-items: center;
    width: 14px;
    height: 20px;
    flex: none;
    color: var(--accent);
  }

  .worker-hint {
    margin-top: var(--gutter);
    padding: var(--gutter-tight);
    border-left: 2px solid var(--border-strong);
    background: var(--surface-2);
    overflow-wrap: anywhere;
  }

  .worker-hint code {
    font-family: var(--font-mono);
    font-size: inherit;
  }

  .worker-hint :global(.copy) {
    opacity: 1;
  }

  .setup-fields {
    margin-top: 24px;
    padding-top: 24px;
    border-top: 1px solid var(--border);
  }

  .setup-fields h3 span {
    margin-left: var(--gap-sm);
    font-size: var(--font-2xs);
    font-weight: 400;
    color: var(--text-3);
  }

  .setup-fields h3, .setup-fields p {
    margin-bottom: var(--gutter);
  }

  .form-error {
    margin-block: var(--gutter);
    color: var(--error);
  }

  .empty {
    padding-block: var(--gutter);
    color: var(--text-3);
  }

  footer {
    flex: none;
    display: grid;
    gap: var(--gutter);
    padding: var(--gutter) 24px;
    border-top: 1px solid var(--border);
    background: var(--surface-2);
  }

  .start-details {
    display: grid;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .start-summary {
    font-size: var(--font-sm);
    color: var(--text-1);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .start-description {
    /* Reserve three lines so choosing another agent does not resize the footer. */
    block-size: 4.5em;
    overflow-y: auto;
    overflow-wrap: anywhere;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--gap-sm);
  }

  button {
    font: inherit;
    cursor: pointer;
  }

  .actions button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--gap-sm);
    min-height: 36px;
    padding: var(--gap-sm) var(--gutter);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-md);
    background: var(--control-bg);
    color: var(--text-1);
    font-size: var(--font-sm);
  }

  .actions .primary {
    border-color: color-mix(in srgb, var(--accent) 50%, var(--border));
    background: color-mix(in srgb, var(--accent) 16%, var(--surface-2));
    font-weight: 600;
  }

  button:disabled {
    cursor: default;
    opacity: var(--disabled-opacity);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  @media (hover: hover) and (pointer: fine) {
    .agent-choice:hover {
      border-color: var(--accent);
    }

    .actions button:hover:not(:disabled) {
      background: var(--control-hover);
    }

    .refresh:hover:not(:disabled) {
      color: var(--text-1);
    }

  }

  @media (max-width: 600px) {
    .agent-choices {
      grid-template-columns: minmax(0, 1fr);
    }

    header, .setup-body {
      padding: var(--gutter);
    }

    footer {
      padding: var(--gutter);
    }

    .start-description {
      block-size: 7.5em;
    }

    .choice-heading {
      align-items: flex-start;
      flex-direction: column;
    }

    .actions button {
      min-height: 44px;
    }

  }

  @media (prefers-reduced-motion: reduce) {
    .agent-choice {
      transition: none;
    }

  }
</style>
