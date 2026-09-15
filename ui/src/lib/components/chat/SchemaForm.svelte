<script lang="ts">
  // Renders one handler's input schema as a form. Purely schema-driven — see schemaForm.ts.
  // Recurses through itself for nested object fields.
  import type { SchemaField } from "./schemaForm";
  import { emptyValue } from "./schemaForm";
  // Self-import so a nested object field recurses. This is the Svelte 5 way; the legacy
  // self-referencing element is deprecated (and is rejected by check:svelte5).
  import SchemaForm from "./SchemaForm.svelte";

  let {
    fields,
    values = $bindable(),
    disabled = false,
    idPrefix = "sf"
  }: {
    fields: SchemaField[];
    values: Record<string, unknown>;
    disabled?: boolean;
    idPrefix?: string;
  } = $props();

  function fieldId(field: SchemaField): string {
    return `${idPrefix}-${field.name}`;
  }

  function asArray(value: unknown): unknown[] {
    return Array.isArray(value) ? value : [];
  }

  function addItem(field: SchemaField) {
    values[field.name] = [
      ...asArray(values[field.name]),
      field.item ? emptyValue(field.item) : ""
    ];
  }

  function removeItem(field: SchemaField, index: number) {
    values[field.name] = asArray(values[field.name]).filter((_, i) => i !== index);
  }

  function setItem(field: SchemaField, index: number, value: unknown) {
    values[field.name] = asArray(values[field.name]).map((entry, i) =>
      i === index ? value : entry
    );
  }
</script>

{#each fields as field (field.name)}
  <div class="field" class:inline={field.kind === "boolean"}>
    {#if field.kind === "boolean"}
      <label class="check" for={fieldId(field)}>
        <input
          id={fieldId(field)}
          type="checkbox"
          {disabled}
          checked={Boolean(values[field.name])}
          onchange={(event) =>
            (values[field.name] = event.currentTarget.checked)}
        />
        <span class="label-text">{field.title}</span>
      </label>
    {:else}
      <label class="label-text" for={fieldId(field)}>
        {field.title}
        {#if field.required}<span class="req" aria-label="required">*</span>{/if}
      </label>
    {/if}

    {#if field.description}
      <p class="hint">{field.description}</p>
    {/if}

    {#if field.kind === "enum"}
      <!-- A constrained field is a dropdown, so it cannot be got wrong. This is the enforced
           replacement for the old hand-rolled argument metadata. -->
      <select
        id={fieldId(field)}
        {disabled}
        value={String(values[field.name] ?? "")}
        onchange={(event) => (values[field.name] = event.currentTarget.value)}
      >
        {#if !field.required}
          <option value="">(unset)</option>
        {/if}
        {#each field.choices ?? [] as choice (choice)}
          <option value={choice}>{choice}</option>
        {/each}
      </select>
    {:else if field.kind === "textarea" || field.kind === "json"}
      <textarea
        id={fieldId(field)}
        {disabled}
        rows={field.kind === "json" ? 4 : 3}
        placeholder={field.kind === "json" ? "JSON value" : ""}
        value={String(values[field.name] ?? "")}
        oninput={(event) => (values[field.name] = event.currentTarget.value)}
      ></textarea>
    {:else if field.kind === "integer" || field.kind === "number"}
      <input
        id={fieldId(field)}
        type="number"
        {disabled}
        step={field.kind === "integer" ? 1 : "any"}
        min={field.minimum}
        max={field.maximum}
        value={String(values[field.name] ?? "")}
        oninput={(event) => (values[field.name] = event.currentTarget.value)}
      />
    {:else if field.kind === "array"}
      <div class="rows">
        {#each asArray(values[field.name]) as entry, index (index)}
          <div class="row">
            {#if field.item?.kind === "enum"}
              <select
                {disabled}
                value={String(entry ?? "")}
                onchange={(event) => setItem(field, index, event.currentTarget.value)}
              >
                {#each field.item.choices ?? [] as choice (choice)}
                  <option value={choice}>{choice}</option>
                {/each}
              </select>
            {:else}
              <input
                type={field.item?.kind === "integer" || field.item?.kind === "number"
                  ? "number"
                  : "text"}
                {disabled}
                value={String(entry ?? "")}
                oninput={(event) => setItem(field, index, event.currentTarget.value)}
              />
            {/if}
            <button
              type="button"
              class="row-action"
              {disabled}
              onclick={() => removeItem(field, index)}
              aria-label="Remove {field.title} entry"
            >
              &minus;
            </button>
          </div>
        {/each}
        <button type="button" class="row-add" {disabled} onclick={() => addItem(field)}>
          + Add {field.title}
        </button>
      </div>
    {:else if field.kind === "object" && field.fields}
      <fieldset class="nested">
        <!-- svelte-ignore binding_property_non_reactive -->
        <SchemaForm
          fields={field.fields}
          bind:values={values[field.name] as Record<string, unknown>}
          {disabled}
          idPrefix={fieldId(field)}
        />
      </fieldset>
    {:else}
      <input
        id={fieldId(field)}
        type="text"
        {disabled}
        value={String(values[field.name] ?? "")}
        oninput={(event) => (values[field.name] = event.currentTarget.value)}
      />
    {/if}
  </div>
{/each}

<style>
  /* Tokens come from ui/src/app.css (a dark theme): --surface-0..3, --text-1..3, --border,
     --border-strong, --accent, --error. No fallbacks — a missing token should look obviously
     wrong in dev rather than silently render a light-theme control on a dark panel. */
  .field {
    display: grid;
    gap: 5px;
    margin-bottom: 11px;
  }

  .field.inline {
    margin-bottom: 9px;
  }

  .label-text {
    color: var(--text-2);
    font-size: 11px;
    font-weight: 680;
    letter-spacing: 0.02em;
  }

  .req {
    margin-left: 2px;
    color: var(--error);
  }

  .hint {
    margin: 0;
    color: var(--text-3);
    font-size: 11px;
    line-height: 1.4;
  }

  .check {
    display: inline-flex;
    gap: 7px;
    align-items: center;
    cursor: pointer;
  }

  .check .label-text {
    color: var(--text-1);
  }

  .check input {
    accent-color: var(--accent);
  }

  input[type="text"],
  input[type="number"],
  select,
  textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 7px 9px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
    color: var(--text-1);
    font: inherit;
    font-size: 12px;
  }

  input[type="text"]:focus-visible,
  input[type="number"]:focus-visible,
  select:focus-visible,
  textarea:focus-visible {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
    outline: 0;
    box-shadow: 0 0 0 3px var(--focus-ring);
  }

  input::placeholder,
  textarea::placeholder {
    color: var(--text-3);
  }

  textarea {
    resize: vertical;
    font-family: SFMono-Regular, Consolas, "Liberation Mono", monospace;
  }

  input:disabled,
  select:disabled,
  textarea:disabled {
    cursor: default;
    opacity: 0.58;
  }

  .rows {
    display: grid;
    gap: 6px;
  }

  .row {
    display: flex;
    gap: 6px;
    align-items: center;
  }

  .row-action,
  .row-add {
    padding: 5px 9px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
    color: var(--text-2);
    font: inherit;
    font-size: 11px;
    cursor: pointer;
  }

  .row-action:hover:not(:disabled),
  .row-add:hover:not(:disabled) {
    border-color: var(--border-strong);
    color: var(--text-1);
  }

  .row-action:disabled,
  .row-add:disabled {
    cursor: default;
    opacity: 0.58;
  }

  .row-add {
    justify-self: start;
  }

  .nested {
    margin: 0;
    padding: 10px 11px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-2) 60%, var(--surface-1));
  }
</style>
