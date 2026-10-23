<script lang="ts">
  // Renders one handler's input schema as a form. Purely schema-driven — see the client's schemaForm.ts.
  // Recurses through itself for nested object fields.
  import { Check, Minus, Plus } from "@lucide/svelte";
  import { emptyValue, fieldPath, type SchemaField } from "@temporalio/agent-harness-client";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import Select from "$lib/components/primitives/Select.svelte";
  // Self-import so a nested object field recurses. This is the Svelte 5 way; the legacy
  // self-referencing element is deprecated (and is rejected by check:svelte5).
  import MarkdownMessage from "./MarkdownMessage.svelte";
  import SchemaForm from "./SchemaForm.svelte";

  let {
    fields,
    values = $bindable(),
    disabled = false,
    idPrefix = "sf",
    errors = {},
    path = "",
    choices = "select",
    other = $bindable(),
    send
  }: {
    fields: SchemaField[];
    values: Record<string, unknown>;
    disabled?: boolean;
    idPrefix?: string;
    /** `validate()`'s result for the whole form; each field shows its own entry. */
    errors?: Record<string, string>;
    /** This form's key prefix in `errors` when it is nested in another. */
    path?: string;
    /** How a field with options asks: a dropdown, or every option in view as radios (one
        answer) or checkboxes (several), for a question put to the reader. */
    choices?: "select" | "list";
    /** Bound, a listed field also offers "Something else…" with a free-text answer, keyed by
        field: null until picked. On a single choice it is one more option; on several it sits
        beside the ticked ones. */
    other?: Record<string, string | null>;
    /** A submit row after the fields, as the attention queue's card ends: the form-level
        alert, then a Send chip named for the handler it sends. */
    send?: { handler: string; disabled?: boolean; alert?: string | null };
  } = $props();

  /* A value set small enough to see at once is a row of buttons rather than a box to type in or
     a list to open: an enum, or an integer whose bounds span a dozen values or fewer, such as
     tic-tac-toe's cell 1-9. A boolean stays a checkbox, already one press and labelled by the
     statement it sets. Questions put as a list keep their radios. */
  const MAX_BUTTONS = 12;

  function buttonValues(field: SchemaField): string[] | null {
    if (choices === "list") return null;
    if (field.kind === "enum") {
      const set = field.choices ?? [];
      return set.length > 0 && set.length <= MAX_BUTTONS ? set : null;
    }
    if (field.kind !== "integer") return null;
    const low = field.minimum ?? (field.exclusiveMinimum == null ? null : Math.floor(field.exclusiveMinimum) + 1);
    const high = field.maximum ?? (field.exclusiveMaximum == null ? null : Math.ceil(field.exclusiveMaximum) - 1);
    if (low == null || high == null || !Number.isInteger(low) || !Number.isInteger(high)) return null;
    const count = high - low + 1;
    return count > 0 && count <= MAX_BUTTONS ? Array.from({ length: count }, (_, i) => String(low + i)) : null;
  }

  /* Picking selects; the form's Send sends, so a mis-click is one more click to fix. Picking
     the selected value of an optional field clears it. */
  function pick(field: SchemaField, value: string): void {
    values[field.name] = !field.required && String(values[field.name] ?? "") === value ? "" : value;
  }

  /* One tab stop per group; arrows move and select as radios do, Enter sends the form. */
  function stepValues(event: KeyboardEvent, field: SchemaField, set: string[]): void {
    const group = event.currentTarget as HTMLElement;
    if (event.key === "Enter") {
      event.preventDefault();
      group.closest("form")?.requestSubmit();
      return;
    }
    const flip = getComputedStyle(group).direction === "rtl" ? -1 : 1;
    const moves: Record<string, number> = { ArrowRight: flip, ArrowDown: 1, ArrowLeft: -flip, ArrowUp: -1 };
    const current = set.indexOf(String(values[field.name] ?? ""));
    let next: number;
    if (event.key === "Home") next = 0;
    else if (event.key === "End") next = set.length - 1;
    else if (event.key in moves) next = (Math.max(current, moves[event.key]! > 0 ? -1 : 0) + moves[event.key]! + set.length) % set.length;
    else return;
    event.preventDefault();
    values[field.name] = set[next]!;
    group.querySelectorAll<HTMLElement>('[role="radio"]')[next]?.focus();
  }

  function options(field: SchemaField): string[] | undefined {
    if (choices !== "list") return undefined;
    return field.kind === "enum" ? field.choices : field.kind === "array" && field.item?.kind === "enum" ? field.item.choices : undefined;
  }

  function choose(field: SchemaField, choice: string, on: boolean): void {
    if (other && field.kind === "enum") other[field.name] = null;
    values[field.name] =
      field.kind === "enum" ? choice : on ? [...asArray(values[field.name]), choice] : asArray(values[field.name]).filter((c) => c !== choice);
  }

  function chooseOther(field: SchemaField, on: boolean): void {
    other![field.name] = on ? "" : null;
    if (on && field.kind === "enum") values[field.name] = "";
  }

  function fieldId(field: SchemaField): string {
    return `${idPrefix}-${field.name}`;
  }

  function fieldError(field: SchemaField): string | undefined {
    return errors[fieldPath(path, field.name)];
  }

  function describedBy(field: SchemaField): string | undefined {
    return fieldError(field) ? `${fieldId(field)}-error` : undefined;
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

{#snippet required(field: SchemaField)}
  {#if field.required}<span class="req" aria-hidden="true">*</span><span class="visually-hidden">required</span>{/if}
{/snippet}

{#each fields as field (field.name)}
  {@const listed = options(field)}
  {@const buttons = buttonValues(field)}
  <div class={["schema-field", { inline: field.kind === "boolean" }]}>
    {#if listed}
      <!-- The legend below names the group. -->
    {:else if buttons}
      <span class="kicker" id={`${fieldId(field)}-label`}>{field.title}{@render required(field)}</span>
    {:else if field.kind === "boolean"}
      <label class="check" for={fieldId(field)}>
        <input
          id={fieldId(field)}
          type="checkbox"
          {disabled}
          checked={values[field.name] === true}
          onchange={(event) =>
            (values[field.name] = event.currentTarget.checked)}
        />
        <span class="option">{field.title}</span>
      </label>
    {:else}
      <label class="kicker" for={fieldId(field)}>
        {field.title}{@render required(field)}
      </label>
    {/if}

    {#if field.description}
      <div class="hint"><MarkdownMessage text={field.description} compact /></div>
    {/if}

    {#if listed}
      {@const many = field.kind === "array"}
      <fieldset class="choices" {disabled} aria-describedby={describedBy(field)}>
        <legend class="kicker">
          {field.title}{@render required(field)}
        </legend>
        <!-- Nothing is picked for the reader: a preselected option is an answer nobody gave. -->
        {#each listed as choice, index (choice)}
          <label class="check">
            <input
              type={many ? "checkbox" : "radio"}
              name={fieldId(field)}
              value={choice}
              checked={many ? asArray(values[field.name]).includes(choice) : other?.[field.name] == null && values[field.name] === choice}
              data-invalid={fieldError(field) && index === 0 ? "" : undefined}
              onchange={(event) => choose(field, choice, event.currentTarget.checked)}
            />
            <span class="option">{choice}</span>
          </label>
        {/each}
        {#if other}
          <label class="check">
            <input
              type={many ? "checkbox" : "radio"}
              name={fieldId(field)}
              data-other=""
              checked={other[field.name] != null}
              onchange={(event) => chooseOther(field, event.currentTarget.checked)}
            />
            <span class="option">Something else…</span>
          </label>
          {#if other[field.name] != null}
            <input
              type="text"
              class="form-control"
              aria-label={`${field.title}: something else, sent to the agent as a note`}
              placeholder="Say it in your own words"
              bind:value={other[field.name]}
            />
          {/if}
        {/if}
      </fieldset>
    {:else if buttons}
      {@const chosen = buttons.indexOf(String(values[field.name] ?? ""))}
      <!-- Hairline buttons, the chosen one tinted. Nothing is chosen for the reader. -->
      <div
        class="values"
        role="radiogroup"
        aria-labelledby={`${fieldId(field)}-label`}
        aria-required={field.required || undefined}
        aria-invalid={fieldError(field) ? "true" : undefined}
        aria-describedby={describedBy(field)}
        tabindex="-1"
        onkeydown={(event) => stepValues(event, field, buttons)}
      >
        {#each buttons as value, index (index)}
          <Chip
            fill="quiet"
            dense
            active={index === chosen}
            role="radio"
            aria-checked={index === chosen}
            tabindex={index === Math.max(chosen, 0) ? 0 : -1}
            id={index === 0 ? fieldId(field) : undefined}
            title={value}
            data-invalid={fieldError(field) && index === 0 ? "" : undefined}
            {disabled}
            onclick={() => pick(field, value)}
          >
            <span class="value fade-end" dir="auto">{value}</span>
          </Chip>
        {/each}
      </div>
    {:else if field.kind === "enum"}
      <!-- A constrained field is a dropdown, so it cannot be got wrong. This is the enforced
           replacement for the old hand-rolled argument metadata. -->
      <Select
        id={fieldId(field)}
        {disabled}
        aria-invalid={fieldError(field) ? "true" : undefined}
        aria-describedby={describedBy(field)}
        aria-required={field.required || undefined}
        options={field.required ? (field.choices ?? []) : [{ value: "", label: "(unset)" }, ...(field.choices ?? [])]}
        placeholder={field.required ? "Choose…" : undefined}
        value={String(values[field.name] ?? "")}
        onchange={(event) => (values[field.name] = event.currentTarget.value)}
      />
    {:else if field.kind === "textarea" || field.kind === "json"}
      <textarea
        id={fieldId(field)}
        class="form-control"
        {disabled}
        aria-invalid={fieldError(field) ? "true" : undefined}
        aria-describedby={describedBy(field)}
        aria-required={field.required || undefined}
        rows={field.kind === "json" ? 4 : 3}
        placeholder={field.kind === "json" ? "JSON value" : ""}
        value={String(values[field.name] ?? "")}
        oninput={(event) => (values[field.name] = event.currentTarget.value)}
      ></textarea>
    {:else if field.kind === "integer" || field.kind === "number"}
      <input
        id={fieldId(field)}
        class="form-control"
        type="number"
        {disabled}
        aria-invalid={fieldError(field) ? "true" : undefined}
        aria-describedby={describedBy(field)}
        aria-required={field.required || undefined}
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
            {#if field.item?.kind === "object" && field.item.fields}
              <fieldset class="nested">
                <SchemaForm
                  fields={field.item.fields}
                  bind:values={(values[field.name] as Record<string, unknown>[])[index]}
                  {disabled}
                  idPrefix={`${fieldId(field)}-${index}`}
                  {errors}
                  path={fieldPath(fieldPath(path, field.name), index)}
                />
              </fieldset>
            {:else if field.item?.kind === "enum"}
              <Select
                {disabled}
                aria-label={`${field.title} ${index + 1}`}
                options={field.item.choices ?? []}
                placeholder="Choose…"
                value={String(entry ?? "")}
                onchange={(event) => setItem(field, index, event.currentTarget.value)}
              />
            {:else}
              <input
                class="form-control"
                type={field.item?.kind === "integer" || field.item?.kind === "number"
                  ? "number"
                  : "text"}
                {disabled}
                aria-label={`${field.title} ${index + 1}`}
                value={String(entry ?? "")}
                oninput={(event) => setItem(field, index, event.currentTarget.value)}
              />
            {/if}
            <Chip fill="quiet" dense {disabled} onclick={() => removeItem(field, index)} aria-label="Remove {field.title} entry">
              {#snippet lead()}<Minus size={12} />{/snippet}
            </Chip>
          </div>
        {/each}
        <!-- A list-level problem (too few or too many entries) has no input to mark, so focus
             lands on the control that fixes it, which also points at the message. -->
        <Chip
          class="row-add"
          fill="quiet"
          dense
          {disabled}
          data-invalid={fieldError(field) ? "" : undefined}
          aria-describedby={describedBy(field)}
          onclick={() => addItem(field)}
        >
          {#snippet lead()}<Plus size={12} />{/snippet}
          Add {field.title}
        </Chip>
      </div>
    {:else if field.kind === "object" && field.fields}
      <fieldset class="nested">
        <SchemaForm
          fields={field.fields}
          bind:values={values[field.name] as Record<string, unknown>}
          {disabled}
          idPrefix={fieldId(field)}
          {errors}
          path={fieldPath(path, field.name)}
        />
      </fieldset>
    {:else if field.kind !== "boolean"}
      <input
        id={fieldId(field)}
        class="form-control"
        type="text"
        {disabled}
        aria-invalid={fieldError(field) ? "true" : undefined}
        aria-describedby={describedBy(field)}
        aria-required={field.required || undefined}
        value={String(values[field.name] ?? "")}
        oninput={(event) => (values[field.name] = event.currentTarget.value)}
      />
    {/if}

    {#if fieldError(field)}
      <p class="field-error" id={`${fieldId(field)}-error`}>{fieldError(field)}</p>
    {/if}
  </div>
{/each}

{#if send}
  {#if send.alert}<p class="field-error" role="alert">{send.alert}</p>{/if}
  <div class="schema-foot">
    <Chip
      type="submit"
      fill="quiet"
      dense
      disabled={disabled || send.disabled}
      aria-label={`Send ${send.handler}`}
      data-tip={`Send ${send.handler}`}
      data-tip-align="end"
    >
      {#snippet lead()}<Check size={12} />{/snippet}
      Send
    </Chip>
  </div>
{/if}

<style>
  /* Tokens come from src/app.css. The controls are its `.form-control` box (dropdowns the
     shared Select) and the labels its
     `.kicker`, so a form reads as the console's chrome rather than a set of its own. */
  /* Pinned to the form's width: an implicit `auto` column grows to a long unbroken title or
     description and carries every control past the card's edge. */
  .schema-field {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--gap-xs);
    margin-bottom: var(--gap-lg);
    overflow-wrap: anywhere;
  }

  .schema-field.inline {
    margin-bottom: var(--gap-md);
  }

  .req {
    margin-inline-start: var(--gap-2xs);
    color: var(--text-3);
  }

  .hint,
  .field-error {
    margin: 0;
    font-size: var(--font-sm);
    line-height: 1.4;
  }

  .hint {
    color: var(--text-3);
  }

  .field-error {
    color: var(--error);
  }

  .check {
    min-width: 0;
    display: flex;
    gap: var(--gap-sm);
    align-items: flex-start;
    cursor: pointer;
  }

  .option {
    min-width: 0;
    color: var(--text-1);
    font-size: var(--font-md);
  }

  .check input {
    flex: none;
    margin: 1px 0 0;
    accent-color: var(--accent);
  }

  .rows {
    display: grid;
    gap: var(--gap-sm);
  }

  .row {
    display: flex;
    gap: var(--gap-sm);
    align-items: center;
  }

  .row > :global(:first-child) {
    flex: 1 1 auto;
    min-width: 0;
  }

  .rows > :global(.row-add) {
    justify-self: start;
  }

  /* Every option a row of its own, the whole row the target. A fieldset's default
     `min-inline-size` is its content's, which would undo the pinning. */
  .choices {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--gap-2xs);
    margin: 0;
    padding: 0;
    border: 0;
  }

  .choices legend {
    margin-bottom: var(--gap-xs);
    padding: 0;
  }

  .choices .check {
    padding: var(--gap-sm) var(--gap-md);
    border: 1px solid transparent;
  }

  .choices .check:has(:checked) {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
    background: var(--selected-bg);
  }

  .choices .check:has(:focus-visible) {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  /* The row draws the ring; a second one on the radio inside it is noise. */
  .choices .check input:focus-visible {
    outline: none;
  }

  /* A wrapping row; each button at least square, never wider than the form. */
  .values {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-xs);
  }

  .values :global(.chip) {
    min-width: var(--control-height);
    max-width: 100%;
    justify-content: center;
  }

  .value {
    min-width: 0;
  }

  .nested {
    min-width: 0;
    margin: 0;
    padding: var(--gutter-tight);
    border: 1px solid var(--border);
  }

  /* The queue card's footer: the decision at the end, a gap under the last field. */
  .schema-foot {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    justify-content: flex-end;
    padding-top: var(--gap-sm);
  }
</style>
