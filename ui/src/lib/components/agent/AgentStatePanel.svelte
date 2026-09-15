<script lang="ts">
  import Chip from "$lib/components/primitives/Chip.svelte";
  import MetricStrip from "$lib/components/primitives/MetricStrip.svelte";
  import type { Metric } from "$lib/components/primitives/metrics";
  import type { JsonValue } from "$lib/api/types";
  import type { AgentStateDoc, StateChange } from "$lib/state/agentState";
  import { escapeToken } from "$lib/state/jsonPatch";

  /**
   * What an agent's own working state holds, as of the playhead.
   *
   * The document below is not fetched and not polled: it is one snapshot plus
   * every RFC 6902 op the agent has published since, folded up to the cursor.
   * That is the point of the feature — a consumer that holds one snapshot and
   * applies ops is exactly in sync, with nothing missed between polls — so the
   * panel renders the fold rather than asking the server what is true now.
   *
   * Every value carries its own JSON pointer as `data-path`, which is what makes
   * the marking exact: `replace /steps/0/done` marks that token, not the row it
   * is in and not the object around it. The pointers the panel draws and the
   * pointers the agent publishes are the same addressing scheme, so nothing has
   * to be matched up or guessed at.
   */
  interface Props {
    states: AgentStateDoc[];
  }

  let { states }: Props = $props();

  let chosenKey = $state<string | null>(null);
  let docElement = $state<HTMLElement | null>(null);

  /* Falls back rather than holding a key that is gone: switching sessions
     replaces every state in the run, and a panel pinned to a document that no
     longer exists would show an empty pane beside a run that has state. */
  const current = $derived(
    states.find((doc) => doc.key === chosenKey) ?? states[0] ?? null
  );

  /* One state is a label, not a choice. The bar is a switch between documents,
     and most runs declare exactly one — so with nothing to switch to the chip
     renders as a plain chip rather than a button that answers a click with
     nothing. */
  const selectable = $derived(states.length > 1);

  /**
   * Where the commit at the cursor landed, as pointers the document can be asked for.
   *
   * A `remove` is marked on the container it emptied, because the node it names
   * is the one thing no longer there to mark. Everything else marks the value
   * itself.
   */
  const marks = $derived.by(() => {
    const byPath = new Map<string, StateChange["op"]>();
    for (const change of current?.changed ?? []) {
      const path =
        change.op === "remove"
          ? change.path.slice(0, change.path.lastIndexOf("/"))
          : change.path;
      byPath.set(path, change.op);
    }
    return byPath;
  });

  const metrics: Metric[] = $derived(
    current
      ? [
          { label: "version", value: `v${current.version}`, tone: "strong" },
          { label: "commits", value: String(current.commits) },
          {
            label: "this step",
            value: `${current.changed.length} op${current.changed.length === 1 ? "" : "s"}`
          }
        ]
      : []
  );

  /** A value, short enough to sit on one line of a change row. */
  function preview(value: JsonValue | undefined): string {
    if (value === undefined) return "—";
    const text = JSON.stringify(value);
    return text.length > 52 ? `${text.slice(0, 51)}…` : text;
  }

  function leafKind(value: JsonValue): string {
    if (value === null) return "nil";
    if (typeof value === "string") return "str";
    if (typeof value === "number") return "num";
    return "bool";
  }

  function entriesOf(value: JsonValue[] | Record<string, JsonValue>): Array<[string, JsonValue]> {
    return Array.isArray(value)
      ? value.map((item, index) => [String(index), item])
      : Object.entries(value);
  }

  /**
   * Bring the first changed value into view when the commit at the cursor moves.
   *
   * `block: "nearest"` so a mark already on screen does not drag the document
   * under the reader — the panel follows the state, it does not chase it.
   */
  $effect(() => {
    const first = current?.changed[0]?.path;
    /* Read so the effect re-runs on a commit that touched the same path twice. */
    void current?.version;
    if (!docElement || first == null || typeof CSS === "undefined") return;
    const target = docElement.querySelector(`[data-path="${CSS.escape(first)}"]`);
    target?.scrollIntoView({ block: "nearest" });
  });
</script>

{#snippet jsonNode(value: JsonValue, pointer: string, label: string | null, last: boolean)}
  {#snippet key()}
    {#if label !== null}<span class="key">"{label}"</span><span class="punc colon">:</span>{/if}
  {/snippet}
  {#if value !== null && typeof value === "object"}
    {@const entries = entriesOf(value)}
    {@const array = Array.isArray(value)}
    {#if entries.length === 0}
      <div class="line">
        {@render key()}<span class="node hollow" data-path={pointer} data-change={marks.get(pointer)}
          >{array ? "[]" : "{}"}</span
        >{#if !last}<span class="punc">,</span>{/if}
      </div>
    {:else}
      <!-- The container is a block and its OPENING bracket shares the key's line, rather than
           the container being an inline-block that wraps whole the moment its subtree is wider
           than what is left of the row. That wrapping is what put `[` under its key with the
           trailing comma stranded at the far right of an otherwise empty line. -->
      <div class="node" data-path={pointer} data-change={marks.get(pointer)}>
        <div class="line">{@render key()}<span class="punc">{array ? "[" : "{"}</span></div>
        <div class="children">
          {#each entries as [childKey, child], index (childKey)}
            {@render jsonNode(
              child,
              `${pointer}/${escapeToken(childKey)}`,
              array ? null : childKey,
              index === entries.length - 1
            )}
          {/each}
        </div>
        <div class="line">
          <span class="punc">{array ? "]" : "}"}</span>{#if !last}<span class="punc">,</span>{/if}
        </div>
      </div>
    {/if}
  {:else}
    <div class="line">
      {@render key()}<span
        class={`node leaf ${leafKind(value)}`}
        data-path={pointer}
        data-change={marks.get(pointer)}>{JSON.stringify(value)}</span
      >{#if !last}<span class="punc">,</span>{/if}
    </div>
  {/if}
{/snippet}

<section class="agent-state" aria-label="Agent state">
  {#if !current}
    <!-- Not an error and not a loading state: most agents declare none. The line
         that would make this pane say something is short enough to print, so it
         is printed rather than described. -->
    <div class="empty">
      <p>No agent in this run has published observable state.</p>
      <p class="empty-note">
        A workflow author opts in with one call, and the harness streams every change
        to it from then on:
      </p>
      <pre data-language="python">self._plan = self._runner.state("plan", PlanState())</pre>
    </div>
  {:else}
    <div class="state-bar" role="group" aria-label="Registered state">
      {#each states as doc (doc.key)}
        <Chip
          label={doc.stateId}
          tone="accent"
          size="xs"
          fill="quiet"
          active={doc.key === current.key}
          aria-pressed={selectable ? doc.key === current.key : undefined}
          data-tip={`${doc.label} · v${doc.version}`}
          onclick={selectable ? () => (chosenKey = doc.key) : undefined}
        />
      {/each}
    </div>

    <div class="reading">
      <!-- Every figure here is as of the playhead, not as of the run. The same
           sentence the token pane uses, for the same reason: scrubbing moves them. -->
      <p class="kicker owner">{current.label} · at cursor</p>
      <MetricStrip {metrics} dense />
    </div>

    {#if current.problem}
      <p class="problem">{current.problem}</p>
    {/if}

    {#if current.changed.length > 0}
      <ul class="changes" aria-label="What this commit changed">
        <!-- Deliberately unkeyed. One commit can carry two ops at the SAME path: the state
             layer records an op per write and never coalesces per path, so `d.x = 1; d.x = 2`,
             two `pop(0)`s draining a list head, and a `sort()` followed by a `reverse()` all
             emit two ops with one pointer between them — the last two without the author
             writing anything twice. A key built from the op and the path collides on every one
             of those, and Svelte throws `each_key_duplicate` in production as well as in dev,
             which with no boundary around this pane took the whole console down. These rows
             hold no component state and no transition, and the list is rebuilt whenever the
             cursor moves, so their position is all the identity they have to have. -->
        {#each current.changed as change}
          <li class={`change ${change.op}`}>
            <span class="kicker op">{change.op}</span>
            <span class="path">{change.path || "/ (whole document)"}</span>
            <span class="delta">
              {#if change.op === "remove"}
                <span class="was">{preview(change.before)}</span>
              {:else if change.before !== undefined}
                <span class="was">{preview(change.before)}</span>
                <span class="arrow">→</span>
                <span class="now">{preview(change.value)}</span>
              {:else}
                <span class="now">{preview(change.value)}</span>
              {/if}
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if current.value !== null}
      <div class="doc" bind:this={docElement}>
        {@render jsonNode(current.value, "", null, true)}
      </div>
    {/if}
  {/if}
</section>

<style>
  /* A column rather than a grid with named rows, because half of what is in it is
     conditional: the problem line and the change list are each absent most of the
     time, and a fixed row template hands the 1fr track to whatever happens to be
     fifth — which, with both of them missing, is nothing. */
  .agent-state {
    display: flex;
    flex-direction: column;
    gap: var(--gutter-tight);
    min-width: 0;
    height: 100%;
    /* The document is the only scroller. A second one around the whole panel
       would swallow the rail's wheel, which is the trap PANE_META's
       viewport/document note is about. */
    overflow: hidden;
  }

  .state-bar {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .reading {
    display: grid;
    gap: var(--gap-sm);
    padding: var(--gutter-tight);
    border: 1px solid var(--border);
    background: var(--surface-2);
    min-width: 0;
  }

  .owner {
    margin: 0;
  }

  /* Says the document is not to be trusted, and why, without taking it away —
     the value above the break is a state the agent really passed through. */
  .problem {
    margin: 0;
    padding: var(--gutter-tight);
    border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border));
    background: color-mix(in srgb, var(--warning) 8%, var(--surface-2));
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.45;
  }

  /* The ops themselves, and the only place a `remove` can be seen at all: the
     node it names is gone from the document by the time the panel draws it. */
  .changes {
    display: grid;
    gap: var(--gap-2xs);
    flex: 0 0 auto;
    margin: 0;
    padding: 0;
    /* About seven rows. A commit can carry dozens of ops, and a summary that can
       grow without limit stops being a summary and starts being the document —
       which is directly underneath it, with the values in place. */
    max-height: 170px;
    overflow: auto;
    list-style: none;
  }

  .change {
    display: grid;
    grid-template-columns: 56px minmax(0, 1fr);
    align-items: baseline;
    gap: var(--gap-sm);
    padding: var(--gap-xs) var(--gap-sm);
    border-left: 2px solid var(--border-strong);
    background: var(--surface-1);
    min-width: 0;
  }

  .change.add {
    border-left-color: var(--success);
  }

  .change.replace {
    border-left-color: var(--accent);
  }

  .change.remove {
    border-left-color: var(--error);
  }

  .op {
    color: var(--text-3);
  }

  .change.add .op {
    color: var(--success);
  }

  .change.replace .op {
    color: var(--accent-soft);
  }

  .change.remove .op {
    color: var(--error);
  }

  .path {
    grid-column: 2;
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--figure-size);
    overflow-wrap: anywhere;
  }

  .delta {
    grid-column: 2;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    overflow-wrap: anywhere;
  }

  .was {
    text-decoration: line-through;
  }

  .arrow {
    color: var(--text-4);
  }

  .now {
    color: var(--text-2);
  }

  .doc {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    overflow: auto;
    padding: var(--gutter-tight);
    border: 1px solid var(--border);
    background: var(--surface-1);
    font-family: var(--font-mono);
    font-size: var(--font-code);
    line-height: 1.55;
  }

  /* One printed line of the document: a key and its scalar, or a key and the bracket
     that opens its subtree. Wrapping happens inside a line, never between a key and
     the thing it names. */
  .line {
    min-width: 0;
  }

  .colon {
    padding-right: 0.5ch;
  }

  .children {
    /* One ch of the mono face per level, plus the bracket's own column, so the
       indent lines up under the key rather than under an average glyph. */
    padding-left: 2ch;
  }

  .key {
    color: var(--text-3);
  }

  .punc,
  /* `{}` / `[]` — punctuation, but a node in its own right: it carries the pointer,
     so a commit that empties a container has something to mark. */
  .hollow {
    color: var(--text-4);
  }

  .leaf {
    overflow-wrap: anywhere;
  }

  .str {
    color: var(--syntax-string);
  }

  .num {
    color: var(--syntax-number);
  }

  .bool,
  .nil {
    color: var(--syntax-literal);
  }

  /* The mark. It stays put while the cursor does, rather than flashing on
     arrival: the commit at the playhead is a position, so what it changed is a
     fact about where you are standing and not about when the frame landed. */
  [data-change] {
    --mark: var(--accent);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--mark) 45%, transparent);
  }

  [data-change="add"] {
    --mark: var(--success);
  }

  [data-change="remove"] {
    --mark: var(--error);
  }

  /* A leaf is filled and a container is only outlined, because a container holds
     everything under it: washing the object a key was removed from tints its whole
     subtree, and a `remove` at a top-level key would tint the document. The ring
     says the same thing at the size of the thing that changed. */
  .leaf[data-change] {
    background: color-mix(in srgb, var(--mark) 22%, transparent);
  }

  .empty {
    display: grid;
    gap: var(--gutter-tight);
    align-content: start;
    padding: var(--gutter);
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.5;
  }

  .empty p {
    margin: 0;
  }

  .empty-note {
    color: var(--text-3);
  }

  .empty pre {
    position: relative;
    margin: 0;
    /* Room above for the language tag, which app.css draws and every host has to
       place — the markdown blocks put it in the same corner. */
    padding: 30px var(--gutter-tight) var(--gutter-tight);
    border: 1px solid var(--code-block-border);
    background: var(--code-block-bg);
    color: var(--code-block-text);
    font-family: var(--font-mono);
    font-size: var(--font-code);
    overflow-x: auto;
  }

  .empty pre[data-language]::before {
    position: absolute;
    top: 7px;
    right: 8px;
  }
</style>
