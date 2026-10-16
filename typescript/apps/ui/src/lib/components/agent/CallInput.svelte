<script lang="ts">
  /**
   * A gated or pending call's arguments, as the person answering it reads them.
   *
   * Approval cards and callback cards both show the input they are asking about. A lone
   * question-like string (ask_user's `question`) reads as the sentence it is; anything
   * else is a name/value list, nested objects opened into dotted key paths. A multi-line
   * value (a script) is shown whole as a block, since that is what is being approved;
   * other long values are clamped, with the full JSON behind a disclosure.
   */
  import { UNKNOWN_TOOL_INPUT } from "$lib/state/logValue";

  interface Props {
    input: unknown;
  }

  interface Row {
    id: string;
    key: string;
    value: unknown;
  }

  /* Deeper objects stay one clamped JSON line; the full input disclosure holds the rest. */
  const MAX_DEPTH = 3;

  let { input }: Props = $props();

  const rows = $derived(isRecord(input) ? flatten(input) : []);
  const question = $derived.by(() => {
    if (rows.length !== 1) return null;
    const { key, value } = rows[0];
    return typeof value === "string" && /^(question|prompt|message)$/i.test(key) ? value.trim() : null;
  });
  /* The clamp hides what a nested or long value holds, so only then is there more to see. */
  const truncated = $derived(
    rows.some(
      ({ value }) => !multiline(value) && ((value !== null && typeof value === "object") || show(value).length > 80)
    )
  );

  function isRecord(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function flatten(record: Record<string, unknown>, path: string[] = []): Row[] {
    return Object.entries(record).flatMap(([key, value]) => {
      const at = [...path, key];
      return isRecord(value) && Object.keys(value).length && at.length < MAX_DEPTH
        ? flatten(value, at)
        : [{ id: JSON.stringify(at), key: at.join("."), value }];
    });
  }

  function multiline(value: unknown): value is string {
    return typeof value === "string" && value.trim().includes("\n");
  }

  function show(value: unknown): string {
    return typeof value === "string" ? value : JSON.stringify(value);
  }
</script>

{#if input === null}
  <p class="prose unknown">{UNKNOWN_TOOL_INPUT}</p>
{:else if typeof input === "string" && input.trim()}
  <p class="prose">{input.trim()}</p>
{:else if question}
  <p class="prose">{question}</p>
{:else if rows.length}
  <dl class="args">
    {#each rows as { id, key, value } (id)}
      <div>
        <dt>{key}</dt>
        <dd class:block={multiline(value)}>{show(value)}</dd>
      </div>
    {/each}
  </dl>
  {#if truncated}
    <details>
      <summary>Full input</summary>
      <pre><code>{JSON.stringify(input, null, 2)}</code></pre>
    </details>
  {/if}
{:else if Array.isArray(input) && input.length}
  <pre><code>{JSON.stringify(input, null, 2)}</code></pre>
{/if}

<style>
  .prose {
    margin: 0;
    color: var(--text-1);
    font-size: var(--font-lg);
    line-height: 1.45;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  .unknown {
    color: var(--text-2);
    font-size: var(--font-md);
  }

  /* Capped, so one long unbroken name cannot push every value off the card. */
  .args {
    margin: 0;
    display: grid;
    grid-template-columns: fit-content(40%) minmax(0, 1fr);
    gap: 4px 12px;
  }

  .args div {
    display: contents;
  }

  .args dt {
    overflow-wrap: anywhere;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    line-height: 1.5;
  }

  .args dd {
    display: -webkit-box;
    margin: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: var(--font-md);
    line-height: 1.45;
    overflow-wrap: anywhere;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
    line-clamp: 3;
  }

  /* A script is shown whole, full width under its key, scrolling within its own cap so
     a long one cannot push the card's actions away. */
  .args dd.block {
    display: block;
    grid-column: 1 / -1;
    max-height: 320px;
    overflow: auto;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-1);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    white-space: pre-wrap;
    -webkit-line-clamp: none;
    line-clamp: none;
  }

  summary {
    width: fit-content;
    color: var(--text-3);
    font-size: var(--font-sm);
    cursor: pointer;
  }

  @media (hover: hover) and (pointer: fine) {
    summary:hover {
      color: var(--text-1);
    }
  }

  pre {
    max-height: 320px;
    overflow: auto;
    margin: 6px 0 0;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-1);
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    line-height: 1.45;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }
</style>
