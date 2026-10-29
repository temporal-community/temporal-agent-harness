<script lang="ts" module>
  import type { JsonValue } from "$lib/api/types";
  import type { StateChange } from "$lib/state/agentState";
  import { escapeToken } from "$lib/state/jsonPatch";

  /**
   * One printed line of the document: a scalar or an empty container with its key, or the
   * line opening or closing a container's subtree. An opening line counts the lines of its
   * whole block, closing line included.
   */
  export interface DocLine {
    kind: "value" | "open" | "close";
    pointer: string;
    depth: number;
    label: string | null;
    value: JsonValue;
    last: boolean;
    lines: number;
  }

  function entriesOf(value: JsonValue[] | Record<string, JsonValue>): Array<[string, JsonValue]> {
    return Array.isArray(value)
      ? value.map((item, index) => [String(index), item])
      : Object.entries(value);
  }

  /** The document as the lines it prints, so a long one can be drawn a window at a time. */
  export function docLines(root: JsonValue): DocLine[] {
    const out: DocLine[] = [];
    const walk = (value: JsonValue, pointer: string, label: string | null, last: boolean, depth: number) => {
      const entries = value !== null && typeof value === "object" ? entriesOf(value) : [];
      if (entries.length === 0) {
        out.push({ kind: "value", pointer, depth, label, value, last, lines: 1 });
        return;
      }
      const array = Array.isArray(value);
      const open: DocLine = { kind: "open", pointer, depth, label, value, last, lines: 1 };
      const at = out.push(open);
      entries.forEach(([key, child], index) =>
        walk(child, `${pointer}/${escapeToken(key)}`, array ? null : key, index === entries.length - 1, depth + 1)
      );
      out.push({ kind: "close", pointer, depth, label, value, last, lines: 1 });
      open.lines = out.length - at + 1;
    };
    walk(root, "", null, true, 0);
    return out;
  }

  /**
   * The marked containers a line is part of, outermost first, and which part of each
   * container's ring it carries: the opening line its top, the closing line its foot, the
   * lines between its sides. Drawn line by line, the segments make the one ring the
   * container used to wear as a single box.
   */
  export function ringsOf(
    line: DocLine,
    marks: ReadonlyMap<string, StateChange["op"]>
  ): Array<{ depth: number; op: StateChange["op"]; edge: "top" | "side" | "foot" }> {
    if (marks.size === 0) return [];
    const tokens = line.pointer === "" ? [] : line.pointer.slice(1).split("/");
    const rings = [];
    let pointer = "";
    for (let depth = 0; depth <= tokens.length; depth += 1) {
      if (depth > 0) pointer += `/${tokens[depth - 1]}`;
      const own = depth === tokens.length;
      if (own && line.kind === "value") break;
      const op = marks.get(pointer);
      if (op) rings.push({ depth, op, edge: own ? (line.kind === "open" ? "top" : "foot") : "side" } as const);
    }
    return rings;
  }
</script>

<script lang="ts">
  import Chip from "$lib/components/primitives/Chip.svelte";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import Reading from "$lib/components/primitives/Reading.svelte";
  import { NEST_CAP } from "$lib/components/primitives/nest";
  import type { Metric } from "$lib/components/primitives/metrics";
  import WindowedRows from "$lib/components/primitives/WindowedRows.svelte";
  import type { AgentStateDoc } from "$lib/state/agentState";
  import { scrollFollower } from "$lib/state/followScroll";

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

  const FIRST_CHANGE_ID = "agent-state-first-change";
  /**
   * A document line that does not wrap: 12px code at a 1.55 line height, as layout lays it
   * out on its 1/64px grid. Exact, because a line that measures what was estimated costs
   * the list nothing, and a page of lines that each differ by a sliver re-renders it.
   */
  const LINE_HEIGHT = 18.59375;

  let chosenKey = $state<string | null>(null);
  let docElement = $state<HTMLElement | null>(null);
  let changesElement = $state<HTMLElement | null>(null);
  let docRows = $state<{ scrollToIndex(index: number, align?: "auto" | "start"): void } | null>(null);

  /* Falls back rather than holding a key that is gone: switching sessions
     replaces every state in the run, and a panel pinned to a document that no
     longer exists would show an empty pane beside a run that has state. */
  const current = $derived(
    states.find((doc) => doc.key === chosenKey) ?? states[0] ?? null
  );

  /* One state is a label, not a choice. The tabs switch between documents, and
     most runs declare exactly one — so with nothing to switch to there are no
     tabs, and the state's name is in the reading's kicker instead. The agent
     name is not: the console header already shows it. */
  const selectable = $derived(states.length > 1);
  const tabsId = $props.id();

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

  const count = new Intl.NumberFormat("en-US");
  const figure = (value: number) => (Number.isFinite(value) ? count.format(value) : "—");

  const headline: Metric[] = $derived(
    current
      ? [
          Number.isFinite(current.version)
            ? { label: "version", value: `v${figure(current.version)}`, tone: "strong" }
            : { label: "version", value: "—" }
        ]
      : []
  );

  const breakdown: Metric[] = $derived(
    current
      ? [
          { label: "commits", value: figure(current.commits) },
          {
            label: "this step",
            value: `${figure(current.changed.length)} op${current.changed.length === 1 ? "" : "s"}`
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
    if (typeof value === "string") return "md-syntax-string";
    if (typeof value === "number") return "md-syntax-number";
    return "md-syntax-literal";
  }

  /* A plan of 3,000 steps printed 12,000 lines; only the lines in view are drawn. */
  const lines = $derived(current?.value == null ? [] : docLines(current.value));
  const firstChanged = $derived(current?.changed[0]?.path ?? null);
  const firstChangedLine = $derived(
    firstChanged == null ? -1 : lines.findIndex((line) => line.kind !== "close" && line.pointer === firstChanged)
  );
  const follower = scrollFollower(() => docElement);

  /**
   * Bring the first changed value into view when the commit at the cursor moves,
   * inside the document's own scroller — see followScroll.ts for why not
   * `scrollIntoView`. A mark already on screen is left where it is.
   *
   * One outside the drawn window is drawn by scrolling the list to where the follower
   * would leave it — a block's last line at the foot of the view, or its first at the top
   * when it is taller than the view — so the list settles its row heights there and the
   * follower has nothing left to move.
   */
  $effect(() => {
    /* Read so the effect re-runs on a commit that touched the same path twice. */
    void current?.version;
    const index = firstChangedLine;
    if (index < 0) return;
    follower.to(FIRST_CHANGE_ID, () => {
      const block = lines[index]!.lines;
      const tall = block * LINE_HEIGHT > (docElement?.clientHeight ?? 0);
      docRows?.scrollToIndex(tall ? index : index + block - 1, tall ? "start" : "auto");
    });
  });
</script>

{#snippet docLine(line: DocLine)}
  {@const array = Array.isArray(line.value)}
  {@const id = line.kind !== "close" && line.pointer === firstChanged ? FIRST_CHANGE_ID : undefined}
  <!-- Inline parts abut, tag to tag: a line break between them would print as a space. -->
  <div
    class="line"
    style:--depth={Math.min(line.depth, NEST_CAP)}
    data-path={line.kind === "open" ? line.pointer : undefined}
  >
    {#if line.kind === "open" && id}<span class="extent" {id} style:--lines={line.lines}></span>{/if}{#each ringsOf(line, marks) as ring (ring.depth)}<span
        class={["ring", ring.edge]}
        style:--at={Math.min(ring.depth, NEST_CAP)}
        data-change={ring.op}
      ></span>{/each}{#if line.kind !== "close" && line.depth > NEST_CAP}<span class="nest-depth"
        >{line.depth}<span class="visually-hidden"> levels deep</span></span
      >{/if}{#if line.kind !== "close" && line.label !== null}<span class="md-syntax-property"
        >"{line.label}"</span
      ><span class="punc colon">:</span>{/if}{#if line.kind === "open"}<span class="punc"
        >{array ? "[" : "{"}</span
      >{:else if line.kind === "close"}<span class="punc">{array ? "]" : "}"}</span>{:else}<span
        class={line.value !== null && typeof line.value === "object" ? "node hollow" : `node leaf ${leafKind(line.value)}`}
        {id}
        data-path={line.pointer}
        data-change={marks.get(line.pointer)}
        >{line.value !== null && typeof line.value === "object"
          ? array
            ? "[]"
            : "{}"
          : JSON.stringify(line.value)}</span
      >{/if}{#if !line.last && line.kind !== "open"}<span class="punc">,</span>{/if}
  </div>
{/snippet}

<section class="agent-state" class:empty={!current} aria-label="Agent state">
  {#if !current}
    <!-- Not an error and not a loading state: most agents declare none. The line
         that would make this pane say something is short enough to print, so it
         is printed rather than described. -->
    <EmptyState
      title="No agent in this run has published observable state."
      language="python"
      code='self._plan = self._runner.state("plan", PlanState())'
    >
      <p>
        A workflow author opts in with one call, and the harness streams every change
        to it from then on:
      </p>
    </EmptyState>
  {:else}
    {#if selectable}
      <!-- Each tab is its own Tab stop rather than one stop with arrow keys between them:
           moving focus from script is what followScroll.test.mjs keeps out of this pane. -->
      <div class="state-tabs" role="tablist" aria-label="Registered state">
        {#each states as doc, index (doc.key)}
          {@const selected = doc.key === current.key}
          <Chip
            label={doc.stateId}
            tone="accent"
            size="xs"
            fill="quiet"
            active={selected}
            role="tab"
            id={`${tabsId}-${index}`}
            aria-selected={selected}
            data-tip={`${doc.label} · v${doc.version}`}
            data-tip-below
            data-tip-align="start"
            onclick={() => (chosenKey = doc.key)}
          />
        {/each}
      </div>
    {/if}

    <div
      class="state-view"
      role={selectable ? "tabpanel" : undefined}
      aria-labelledby={selectable ? `${tabsId}-${states.indexOf(current)}` : undefined}
    >
      <!-- Every figure here is as of the playhead, not as of the run. The same
           sentence the token pane uses, for the same reason: scrubbing moves them. -->
      <Reading label={`${current.stateId} · at cursor`} {headline} {breakdown} />

      {#if current.problem}
        <p class="callout-warning">{current.problem}</p>
      {/if}

      {#if current.changed.length > 0}
        <div class="changes" role="list" aria-label="What this commit changed" bind:this={changesElement}>
          <!-- Keyed by position. One commit can carry two ops at the SAME path: the state
               layer records an op per write and never coalesces per path, so `d.x = 1; d.x = 2`,
               two `pop(0)`s draining a list head, and a `sort()` followed by a `reverse()` all
               emit two ops with one pointer between them — the last two without the author
               writing anything twice. A key built from the op and the path collides on every one
               of those, and Svelte throws `each_key_duplicate` in production as well as in dev,
               which with no boundary around this pane took the whole console down. These rows
               hold no component state and no transition, and the list is rebuilt whenever the
               cursor moves, so their position is all the identity they have to have. -->
          <WindowedRows items={current.changed} getKey={(_, index) => index} scroller={changesElement} start={0} estimateSize={37} gap={2}>
            {#snippet children(change)}
              <div class={`change ${change.op}`} role="listitem">
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
              </div>
            {/snippet}
          </WindowedRows>
        </div>
      {/if}

      {#if current.value !== null}
        <Copyable block value={JSON.stringify(current.value, null, 2)} label="Copy state">
          <!-- LTR whatever the page is: an Arabic key would otherwise swap places with its
               value. Focusable because it is a scroller a keyboard must reach. -->
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <div
            class="doc"
            dir="ltr"
            role="region"
            aria-label="State document"
            tabindex="0"
            bind:this={docElement}
            onscroll={follower.handleScroll}
          >
            <WindowedRows
              items={lines}
              getKey={(line) => (line.kind === "close" ? `${line.pointer}\u0000` : line.pointer)}
              scroller={docElement}
              start={0}
              estimateSize={LINE_HEIGHT}
              bind:this={docRows}
            >
              {#snippet children(line)}{@render docLine(line)}{/snippet}
            </WindowedRows>
          </div>
        </Copyable>
      {/if}
    </div>
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
    padding: var(--pane-inset);
    /* The document is the only scroller. A second one around the whole panel
       would swallow the rail's wheel, which is the trap PANE_META's
       viewport/document note is about. */
    overflow: hidden;
  }

  .agent-state.empty {
    overflow: auto;
  }

  .state-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-xs);
    min-width: 0;
  }

  .state-view,
  .state-view > :global(.copyable.block) {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
  }

  .state-view {
    gap: var(--gutter-tight);
  }

  /* The ops themselves, and the only place a `remove` can be seen at all: the
     node it names is gone from the document by the time the panel draws it. */
  .changes {
    position: relative;
    flex: 0 0 auto;
    /* About seven rows. A commit can carry dozens of ops, and a summary that can
       grow without limit stops being a summary and starts being the document —
       which is directly underneath it, with the values in place. */
    max-height: 170px;
    overflow: auto;
  }

  .change {
    display: grid;
    grid-template-columns: 56px minmax(0, 1fr);
    align-items: baseline;
    gap: var(--gap-sm);
    padding: var(--gap-xs) var(--gap-sm);
    background: var(--selected-bg);
    min-width: 0;
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
    position: relative;
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
    /* One ch of the mono face per level, plus the bracket's own column, so the
       indent lines up under the key rather than under an average glyph. */
    padding-left: calc(var(--depth) * 2ch);
  }

  .colon {
    padding-right: 0.5ch;
  }

  /* A marked container's ring, one line's share of it at a time: the line is all
     that is drawn, so the box the ring used to go round is not there to wear it. */
  .ring {
    position: absolute;
    inset-block: 0;
    left: calc(var(--at) * 2ch);
    right: 0;
    pointer-events: none;
  }

  .ring.top {
    clip-path: inset(-1px -1px 0 -1px);
  }

  .ring.side {
    clip-path: inset(0 -1px);
  }

  .ring.foot {
    clip-path: inset(0 -1px -1px -1px);
  }

  /* The changed container's whole block, for the follower to bring into view as it did
     the block itself: one line-height a line, so a block whose lines wrap measures short
     by the wrapped part. */
  .extent {
    position: absolute;
    top: 0;
    left: 0;
    width: 1px;
    height: calc(var(--lines) * 1lh);
    visibility: hidden;
    pointer-events: none;
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
</style>
