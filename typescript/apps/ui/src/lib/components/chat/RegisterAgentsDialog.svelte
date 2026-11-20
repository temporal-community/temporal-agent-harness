<script lang="ts">
  import { FileUp, FolderUp, Plus, Trash2, X } from "@lucide/svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import {
    checkRows,
    emptyRow,
    filesFromZip,
    rowsFromFiles,
    type AgentRegistryActions,
    type ChosenFile,
    type RegistrationRow,
    type RowField,
    type RowProblems
  } from "$lib/state/agentRegistration";

  interface Props {
    actions: AgentRegistryActions;
    /** The form's first rows; one empty row when absent. */
    rows?: RegistrationRow[];
    onClose: () => void;
  }

  let { actions, rows: initialRows, onClose }: Props = $props();

  const uid = $props.id();
  let dialogElement = $state<HTMLDialogElement | null>(null);
  let rows = $state<RegistrationRow[]>(initialRows?.length ? initialRows.map((row) => ({ ...row })) : [emptyRow()]);
  let problems = $state<RowProblems[]>([]);
  let importError = $state<string | null>(null);
  let failure = $state<string | null>(null);
  let registering = $state(false);

  const FIELDS: { field: Exclude<RowField, "description" | "schema">; label: string; placeholder: string }[] = [
    { field: "key", label: "Key", placeholder: "my-agent" },
    { field: "label", label: "Label", placeholder: "My agent" },
    { field: "workflow_type", label: "Workflow type", placeholder: "MyAgent" },
    { field: "task_queue", label: "Task queue", placeholder: "my-agent-task-queue" }
  ];

  $effect(() => {
    if (dialogElement && !dialogElement.open) {
      dialogElement.showModal();
      dialogElement.querySelector<HTMLInputElement>("input.form-control")?.focus();
    }
  });

  function fieldId(index: number, field: RowField): string {
    return `${uid}-${index}-${field}`;
  }

  function problemOf(index: number, field: RowField): string | undefined {
    return problems[index]?.[field];
  }

  function addRow(): void {
    rows = [...rows, emptyRow()];
    problems = [...problems, {}];
  }

  function removeRow(index: number): void {
    rows = rows.filter((_, i) => i !== index);
    problems = problems.filter((_, i) => i !== index);
    if (rows.length === 0) addRow();
  }

  /* A file's path inside a chosen folder, else its name. */
  function chosenPath(file: File): string {
    return file.webkitRelativePath || file.name;
  }

  async function chosenFiles(list: FileList): Promise<ChosenFile[]> {
    const files: ChosenFile[] = [];
    for (const file of Array.from(list)) {
      if (file.name.toLowerCase().endsWith(".zip")) {
        files.push(...filesFromZip(new Uint8Array(await file.arrayBuffer())));
      } else {
        files.push({ path: chosenPath(file), text: await file.text() });
      }
    }
    return files;
  }

  /* An import replaces the form: the file is the quicker way to fill it in. */
  async function importFiles(event: Event): Promise<void> {
    const input = event.currentTarget as HTMLInputElement;
    if (!input.files?.length) return;
    importError = null;
    failure = null;
    try {
      const imported = rowsFromFiles(await chosenFiles(input.files));
      if ("error" in imported) {
        importError = imported.error;
      } else {
        rows = imported.rows;
        problems = imported.problems;
      }
    } catch (error) {
      importError = error instanceof Error ? error.message : "Could not read the chosen files.";
    } finally {
      input.value = "";
    }
  }

  async function attachSchema(index: number, event: Event): Promise<void> {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    rows[index].schema = await file.text();
    problems[index] = { ...problems[index], schema: undefined };
    input.value = "";
  }

  async function register(): Promise<void> {
    if (registering) return;
    failure = null;
    const checked = checkRows(rows);
    if ("problems" in checked) {
      problems = checked.problems;
      dialogElement?.querySelector<HTMLElement>("[aria-invalid='true']")?.focus();
      return;
    }
    problems = [];
    registering = true;
    try {
      await actions.register(checked.registrations);
      dialogElement?.close();
    } catch (error) {
      failure = error instanceof Error ? error.message : "Could not register these agents.";
    } finally {
      registering = false;
    }
  }
</script>

<dialog
  bind:this={dialogElement}
  class="register-sheet"
  aria-labelledby={`${uid}-title`}
  aria-describedby={`${uid}-subtitle`}
  onkeydown={(event) => event.stopPropagation()}
  onclose={onClose}
  oncancel={(event) => { if (registering) event.preventDefault(); }}
>
  <form onsubmit={(event) => { event.preventDefault(); void register(); }}>
    <header class="surface-head">
      <div class="head-name">
        <h2 class="head-title" id={`${uid}-title`}>Add agents</h2>
        <p class="head-subtitle" id={`${uid}-subtitle`}>Fill in each agent, or import an agents.toml with the schema files it names.</p>
      </div>
      <button
        type="button"
        class="head-button"
        aria-label="Close"
        data-tip="Close"
        data-tip-below
        data-tip-align="end"
        disabled={registering}
        onclick={() => dialogElement?.close()}
      ><X size={14} aria-hidden="true" /></button>
    </header>

    <div class="imports">
      <label class="import">
        <FileUp size={13} aria-hidden="true" />
        <span>Import files</span>
        <input type="file" multiple accept=".toml,.json,.zip" disabled={registering} onchange={(event) => void importFiles(event)} />
      </label>
      <label class="import">
        <FolderUp size={13} aria-hidden="true" />
        <span>Import folder</span>
        <input type="file" webkitdirectory disabled={registering} onchange={(event) => void importFiles(event)} />
      </label>
      <span class="import-hint">An agents.toml and its schema files, a folder holding them, or a downloaded zip.</span>
      {#if importError}<p class="form-error" role="alert">{importError}</p>{/if}
    </div>

    <div class="rows">
      {#each rows as row, index (index)}
        <fieldset class="agent-row" disabled={registering}>
          <legend class="kicker">Agent {index + 1}</legend>
          {#if rows.length > 1}
            <IconButton class="remove-row" label={`Remove agent ${index + 1}`} onclick={() => removeRow(index)}>
              <Trash2 size={13} aria-hidden="true" />
            </IconButton>
          {/if}
          <div class="row-fields">
            {#each FIELDS as { field, label, placeholder } (field)}
              <div class="row-field">
                <label for={fieldId(index, field)}>{label}</label>
                <input
                  id={fieldId(index, field)}
                  class="form-control"
                  bind:value={row[field]}
                  {placeholder}
                  autocomplete="off"
                  spellcheck="false"
                  aria-invalid={problemOf(index, field) ? "true" : undefined}
                  aria-describedby={problemOf(index, field) ? `${fieldId(index, field)}-error` : undefined}
                />
                {#if problemOf(index, field)}<p class="field-error" id={`${fieldId(index, field)}-error`}>{problemOf(index, field)}</p>{/if}
              </div>
            {/each}
            <div class="row-field wide">
              <label for={fieldId(index, "description")}>Description</label>
              <textarea
                id={fieldId(index, "description")}
                class="form-control prose"
                rows="2"
                bind:value={row.description}
                aria-invalid={problemOf(index, "description") ? "true" : undefined}
                aria-describedby={problemOf(index, "description") ? `${fieldId(index, "description")}-error` : undefined}
              ></textarea>
              {#if problemOf(index, "description")}<p class="field-error" id={`${fieldId(index, "description")}-error`}>{problemOf(index, "description")}</p>{/if}
            </div>
            <div class="row-field wide">
              <span class="schema-head">
                <label for={fieldId(index, "schema")}>Schema document <span class="optional">Optional</span></label>
                <label class="import attach">
                  <FileUp size={12} aria-hidden="true" />
                  <span>Attach</span>
                  <input type="file" accept=".json,application/json" onchange={(event) => void attachSchema(index, event)} />
                </label>
              </span>
              <textarea
                id={fieldId(index, "schema")}
                class="form-control"
                rows="3"
                placeholder="Paste the output of temporal-agent-kit schema, or leave empty"
                spellcheck="false"
                bind:value={row.schema}
                aria-invalid={problemOf(index, "schema") ? "true" : undefined}
                aria-describedby={problemOf(index, "schema") ? `${fieldId(index, "schema")}-error` : undefined}
              ></textarea>
              {#if problemOf(index, "schema")}<p class="field-error" id={`${fieldId(index, "schema")}-error`}>{problemOf(index, "schema")}</p>{/if}
            </div>
          </div>
        </fieldset>
      {/each}
      <Chip class="add-row" size="sm" fill="quiet" disabled={registering} onclick={addRow}>
        <Plus size={12} aria-hidden="true" />Add another agent
      </Chip>
    </div>

    <footer class="register-actions">
      {#if failure}<p class="form-error" role="alert">{failure}</p>{/if}
      <div class="actions">
        <Chip fill="quiet" disabled={registering} onclick={() => dialogElement?.close()}>Cancel</Chip>
        <Chip type="submit" fill="solid" tone="accent" disabled={registering}>
          {registering ? "Registering…" : rows.length === 1 ? "Register agent" : `Register ${rows.length} agents`}
        </Chip>
      </div>
    </footer>
  </form>
</dialog>

<style>
  .register-sheet {
    container: register-sheet / inline-size;
    width: min(760px, calc(100% - var(--gutter) * 2));
    height: min(86dvh, 760px);
    box-sizing: border-box;
    max-height: none;
    padding: 0;
    border: 1px solid var(--border-strong);
    border-radius: 0;
    background: var(--surface-1);
    color: var(--text-1);
    box-shadow: var(--shadow-modal);
    overflow: hidden;
  }

  .register-sheet[open] {
    display: flex;
    flex-direction: column;
  }

  .register-sheet::backdrop {
    background: var(--overlay-scrim);
  }

  form {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }

  h2, p {
    margin: 0;
  }

  .imports {
    flex: none;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    padding: var(--pane-inset);
    border-bottom: 1px solid var(--border);
  }

  .imports .form-error {
    flex: 1 0 100%;
  }

  /* A file input is its label's: the label draws the control, the input stays focusable. */
  .import {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: var(--gap-xs);
    height: var(--control-height);
    padding-inline: var(--gap-md);
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-2);
    font-size: var(--font-sm);
    cursor: pointer;
  }

  .import input {
    position: absolute;
    inset: 0;
    width: 100%;
    opacity: 0;
    cursor: pointer;
  }

  .import:has(input:focus-visible) {
    outline: 2px solid var(--focus-ring);
    outline-offset: 1px;
  }

  .import:has(input:disabled) {
    opacity: var(--disabled-opacity);
    cursor: default;
  }

  .import-hint {
    color: var(--text-3);
    font-size: var(--font-xs);
  }

  .rows {
    flex: 1 1 auto;
    display: grid;
    align-content: start;
    gap: var(--gutter-tight);
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: var(--pane-inset);
  }

  .agent-row {
    position: relative;
    min-width: 0;
    margin: 0;
    padding: var(--gutter-tight) var(--pane-inset) var(--pane-inset);
    border: 1px solid var(--border);
    background: var(--surface-2);
  }

  .agent-row legend {
    padding-inline: var(--gap-xs);
  }

  .agent-row :global(.remove-row) {
    position: absolute;
    top: var(--gap-sm);
    inset-inline-end: var(--gap-sm);
  }

  .row-fields {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--gap-sm) var(--gutter-tight);
  }

  .row-field {
    display: grid;
    gap: var(--gap-xs);
    min-width: 0;
  }

  .row-field label {
    color: var(--text-2);
    font-size: var(--font-sm);
  }

  .prose {
    font-family: inherit;
  }

  .schema-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-sm);
  }

  .optional {
    margin-inline-start: var(--gap-sm);
    color: var(--text-3);
    font-size: var(--font-2xs);
  }

  .import.attach {
    height: auto;
    padding: 2px var(--gap-sm);
    font-size: var(--font-xs);
  }

  .field-error,
  .form-error {
    color: var(--error);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }

  .rows :global(.add-row) {
    justify-self: start;
  }

  .register-actions {
    flex: none;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    padding: var(--gap-sm) var(--pane-inset);
    border-top: 1px solid var(--border);
    background: var(--surface-2);
  }

  .register-actions .form-error {
    flex: 1 1 12rem;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--gap-sm);
    margin-inline-start: auto;
  }

  @container register-sheet (min-width: 560px) {
    .row-fields {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .row-field.wide {
      grid-column: 1 / -1;
    }
  }
</style>
