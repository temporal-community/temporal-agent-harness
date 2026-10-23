<script lang="ts">
  import { ChevronDown, MessageCircleQuestion } from "@lucide/svelte";
  import { tick, untrack, type Snippet } from "svelte";
  import { quintOut } from "svelte/easing";
  import { prefersReducedMotion } from "svelte/motion";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import StateGlyph from "$lib/components/primitives/StateGlyph.svelte";
  import { answeredFronts, stackAnswer } from "$lib/motion/stackAnswer";
  import { toastClose } from "$lib/motion/toastClose";
  import type { ReplayLogRow } from "$lib/state/replayLog";
  import QueuePager from "./QueuePager.svelte";

  interface Props {
    /** Everything the agent is blocked on, oldest first: it is blocked on them in this order. */
    rows: ReplayLogRow[];
    key: (row: ReplayLogRow) => string | number | null;
    /** Names the region. */
    label: string;
    /** What a row is: "approval" counts "2 approvals" and pages "Approval 1 of 3". A queue of
        several kinds pages "Request 1 of 3". */
    noun: (row: ReplayLogRow) => string;
    /** The front banner, with its decisions in its footer. The pager is the header's. */
    banner: Snippet<[ReplayLogRow]>;
    /** The header's height: all a folded stack is, and the room the chat keeps clear for it. */
    headHeight?: number;
    /** Where focus goes when the last request is answered from inside the stack. */
    onempty?: () => void;
  }

  let { rows, key, label, noun, banner, headHeight = $bindable(0), onempty }: Props = $props();

  /* Banners drawn behind the front one; the rest are only counted. */
  const PEEKING = 2;

  const listId = $props.id();
  let open = $state(true);
  let activeKey = $state<string | number | null>(null);
  let lastIndex = $state(0);
  /* 1: the next row comes up from below (next, a decision); -1: back down. */
  let direction = $state(1);
  let height = $state<number | null>(null);
  /* While a row slides in. Only then does the banner hold a measured height to animate from:
     at rest it is the row's own, so a reason field opening inside it moves nothing a frame
     late, and the sticky footer never lands on it. */
  let paging = $state(false);
  let card: HTMLElement | undefined;
  let viewport: HTMLElement | undefined;
  let section: HTMLElement | undefined;
  /* By kind, in a fixed order so answering one kind never reorders the rest:
     "· 2 approvals · 1 response". */
  const kinds = $derived.by(() => {
    const counts: Record<string, number> = {};
    for (const row of rows) counts[noun(row)] = (counts[noun(row)] ?? 0) + 1;
    return Object.entries(counts).sort(([a], [b]) => a.localeCompare(b));
  });
  const count = $derived(`${rows.length} need${rows.length === 1 ? "s" : ""} you`);
  const breakdown = $derived(kinds.map(([kind, n]) => ` · ${n} ${kind}${n === 1 ? "" : "s"}`).join(""));
  const pagerNoun = $derived(kinds.length === 1 ? kinds[0]![0] : "request");
  /* The shield means a decision; a queue of questions only is not one. */
  const deciding = $derived(kinds.some(([kind]) => kind === "approval"));

  const keyOf = (row: ReplayLogRow) => key(row) ?? row.ordinal;
  /* A decided row leaves `rows` and the one after takes its place. One decided elsewhere
     ahead of the shown row shifts the queue under it; the card then shows the row after,
     still counted and reachable with Previous. */
  const index = $derived.by(() => {
    const found = rows.findIndex((row) => keyOf(row) === activeKey);
    return found >= 0 ? found : Math.max(0, Math.min(lastIndex, rows.length - 1));
  });
  const active = $derived(rows[index]);
  const activeId = $derived(active ? keyOf(active) : null);
  /* The rows after the front one, wrapping, keyed by row: paging or a decision moves each
     plate up or back a depth. */
  const behind = $derived(Array.from({ length: Math.min(PEEKING, rows.length - 1) }, (_, d) => rows[(index + 1 + d) % rows.length]!));
  /* The front banner is a new one each time its row is answered: the answered one drops away
     as the next comes forward from its plate, in one pass. Paging keeps it. */
  const countAnswered = answeredFronts<string | number>();
  const answered = $derived(countAnswered(activeId, rows.map(keyOf)));
  const title = $derived(pagerNoun.charAt(0).toUpperCase() + pagerNoun.slice(1));
  /* One announcement per change, in words. Folded, only the count; it never reopens itself. */
  const spoken = $derived(rows.length === 0 ? "" : open ? `${title} ${index + 1} of ${rows.length}` : count);

  function step(by: 1 | -1): void {
    const next = index + by;
    if (next < 0 || next >= rows.length) return;
    direction = by;
    height = viewport?.offsetHeight ?? height;
    paging = true;
    activeKey = keyOf(rows[next]!);
    lastIndex = next;
  }

  /* Before the DOM drops the answered banner or the count chip, while focus is still in them. */
  $effect.pre(() => {
    if (rows.length === 0 && untrack(() => section?.contains(document.activeElement))) onempty?.();
  });

  /* Escape folds the stack from anywhere in it, before a field's own key handling sees it,
     and leaves focus on the count chip that opens it again. */
  function fold(event: KeyboardEvent): void {
    if (event.key !== "Escape" || !open) return;
    event.preventDefault();
    open = false;
    section?.querySelector<HTMLElement>(".stack-toggle")?.focus();
  }

  function openMs(node: HTMLElement): number {
    return parseFloat(getComputedStyle(node).getPropertyValue("--duration-settle")) || 350;
  }

  /* The header's height, back to 0 only once the queue has gone, so the chat keeps the room
     clear while it leaves. */
  function measureHead(node: HTMLElement) {
    const observer = new ResizeObserver(() => (headHeight = node.offsetHeight));
    observer.observe(node);
    return () => {
      observer.disconnect();
      headHeight = 0;
    };
  }

  /* Rows slide vertically; the leaving one is lifted out of flow so the banner sizes to the
     arriving one, and its height follows. */
  function move(node: HTMLElement, { into }: { into: boolean }) {
    if (!into) handoff(node);
    if (prefersReducedMotion.current) return { duration: 0 };
    const from = (into ? 1 : -1) * direction;
    if (!into) node.classList.add("is-leaving");
    return {
      duration: openMs(node),
      easing: quintOut,
      css: (t: number, u: number) => `transform: translateY(${u * from * 32}%); opacity: ${t}; filter: blur(${u * 2}px);`,
      /* Paged straight back, Svelte keeps this node and ticks it to 1 with inert lifted. */
      tick: into ? undefined : (t: number) => node.classList.toggle("is-leaving", t < 1 || node.inert)
    };
  }

  /* Focus in the answered banner moves to the next one once its attachment has made it `card`. */
  function answer(node: HTMLElement) {
    if (node.contains(document.activeElement)) void tick().then(() => card?.focus({ preventScroll: true }));
    paging = false;
    return stackAnswer(node);
  }

  function measure(node: HTMLElement) {
    const observer = new ResizeObserver(([entry]) => (height = entry?.borderBoxSize[0]?.blockSize ?? null));
    observer.observe(node);
    return () => observer.disconnect();
  }

  /* Deciding disables the row's buttons while it resolves, which drops focus to the body; it
     parks on the banner instead, so the reader keeps their place and a second Enter answers
     nothing. Captured, since the decision handlers stop propagation. */
  function clicked(): void {
    direction = 1;
    setTimeout(() => {
      const focused = document.activeElement;
      if (!focused || focused === document.body || (focused as HTMLButtonElement).disabled) {
        card?.focus({ preventScroll: true });
      }
    });
  }

  /* A row leaving with focus inside hands focus to the banner as it starts to go, not once
     it is removed and focus has fallen to the body. The pager is in the header, so paging
     keeps focus where it is. */
  function handoff(node: HTMLElement): void {
    if (node.contains(document.activeElement)) card?.focus({ preventScroll: true });
  }
</script>

<section
  class="pending-banners"
  role={rows.length > 0 ? "region" : undefined}
  aria-label={rows.length > 0 ? label : undefined}
  onkeydowncapture={fold}
  {@attach (node) => void (section = node)}
>
  <!-- The position is what is announced, once per change, not the rolling digits. -->
  <div class="sr-only" aria-live="polite" aria-atomic="true">{spoken}</div>
  {#if rows.length > 0}
    <!-- The whole queue opens like a toast when the first request arrives and closes like one
         after the last answer; its card folds into the count chip the same way. -->
    <div class="queue is-open" out:toastClose>
      <header class="stack-head" {@attach measureHead}>
        <!-- No dismiss: closing folds the banners into their count, which opens them again. -->
        <Chip
          class="stack-toggle"
          tone="live"
          size="xs"
          toned
          ring
          aria-expanded={open}
          aria-controls={listId}
          onclick={() => (open = !open)}
        >
          {#snippet lead()}
            {#if deciding}<StateGlyph kind="approval" size={12} />{:else}<MessageCircleQuestion size={12} />{/if}
          {/snippet}
          <span dir="auto">{count}</span>
          <span class={["caret", { "is-open": open }]} aria-hidden="true"><ChevronDown size={12} /></span>
        </Chip>
        <span class="breakdown" dir="auto">{breakdown}</span>
        {#if open && rows.length > 1}
          <!-- Counts the queue, whatever is asked inside one request. -->
          <div class="head-pager"><QueuePager {index} count={rows.length} noun={pagerNoun} onstep={step} /></div>
        {/if}
      </header>

      <!-- Folded, the card stays mounted, so a half-typed answer survives, but inert. -->
      <div id={listId} class={["fold", { "is-open": open }]} inert={!open}>
        <div class="fold-body">
          <div class="stack" style:--plates={behind.length}>
            {#each behind as row, depth (keyOf(row))}
              <div class="layer" data-depth={depth + 1} aria-hidden="true"></div>
            {/each}
            {#key answered}
              <div
                class="layer"
                data-depth="0"
                tabindex="-1"
                onclickcapture={clicked}
                out:answer
                {@attach (node) => void (card = node)}
              >
                <div
                  class="viewport animate-size"
                  style:height={paging && height !== null ? `${height}px` : null}
                  {@attach (node) => void (viewport = node)}
                >
                  {#key activeId}
                    <div
                      class="row"
                      in:move={{ into: true }}
                      out:move={{ into: false }}
                      onintrostart={() => (paging = true)}
                      onintroend={() => (paging = false)}
                      {@attach measure}
                    >
                      {#if active}{@render banner(active)}{/if}
                    </div>
                  {/key}
                </div>
              </div>
            {/key}
          </div>
        </div>
      </div>
    </div>
  {/if}
</section>

<style>
  /* The front request and up to two layers behind it, each a step further back: a --gap-lg
     higher, 6% narrower from its top edge, and fainter. Paging or an answer changes a layer's
     depth, and every layer moves to its new one on --duration-settle so the stack restacks as
     one. These are transitions, so a second click mid-move turns them round from where they
     are. Only the top edge of a layer behind shows, so those are bare boxes, sized to the
     front card and cut to it. */
  .stack {
    --step: var(--gap-lg);
    --step-shrink: 0.06;
    --plates-height: calc(var(--step) * var(--plates, 0));
    position: relative;
    isolation: isolate;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    margin-top: 10px;
    padding-top: var(--plates-height);
  }

  /* The blur stays at 0 but is held, with will-change, so each layer keeps its own compositing
     layer and stays the containing block for anything fixed inside the card. */
  .layer {
    --depth: 0;
    position: relative;
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    border: 1px solid color-mix(in srgb, var(--live) 40%, var(--border));
    background: var(--surface-0);
    transform: translateY(calc(var(--depth) * var(--step) * -1)) scale(calc(1 - var(--depth) * var(--step-shrink)));
    filter: blur(0);
    will-change: transform, opacity, filter;
    transition:
      transform var(--duration-settle) var(--ease-out),
      opacity var(--duration-settle) var(--ease-out),
      filter var(--duration-settle) var(--ease-out);
  }

  .layer:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .layer[data-depth="0"] {
    z-index: 3;
  }

  .layer[data-depth="1"],
  .layer[data-depth="2"] {
    position: absolute;
    inset: var(--plates-height) 0 0;
    overflow: hidden;
    transform-origin: top;
  }

  .layer[data-depth="1"] {
    --depth: 1;
    z-index: 2;
    opacity: 0.6;
  }

  .layer[data-depth="2"] {
    --depth: 2;
    z-index: 1;
    opacity: 0.36;
  }

  /* An answered card drops below the stack, a whole card and a step down, out of flow and over
     the next one; the queue's clip hides it as it goes. */
  .layer:global(.is-answered) {
    position: absolute;
    inset: var(--plates-height) 0 auto;
    z-index: 4;
    transform: translateY(calc(100% + var(--step)));
  }

  @starting-style {
    /* Paged back, the card that was in front steps behind from where it stood. */
    .layer[data-depth="1"] {
      --depth: 0;
      opacity: 1;
    }

    /* A new last layer fades in where it sits. */
    .layer[data-depth="2"] {
      opacity: 0;
    }

    /* After an answer, the next card comes forward from the layer it was. */
    .layer:global(.is-answered) ~ .layer[data-depth="0"] {
      --depth: 1;
      opacity: 0.6;
    }
  }

  /* Clipped while a row slides, with room for the focus rings inside it. Its size follows the
     slide's clock. */
  .viewport {
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    overflow: clip;
    overflow-clip-margin: var(--gap-xs);
    transition-duration: var(--duration-settle);
  }

  .row {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-self: start;
  }

  /* Focus scrolls a field clear of the sticky footer below it. */
  .row :global(*) {
    scroll-margin-bottom: 3.5rem;
  }

  .row:global(.is-leaving) {
    position: absolute;
    inset: 0 0 auto;
  }

  .caret {
    display: inline-flex;
    margin-inline-start: 4px;
    transform: rotate(-90deg);
    transition: transform var(--duration-fast) var(--ease-out);
  }

  .caret.is-open {
    transform: none;
  }

  .caret:dir(rtl):not(.is-open) {
    transform: rotate(90deg);
  }

  .pending-banners {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }

  /* The queue arrives with the first request and leaves after the last answer; folding its
     card into the count chip is the same show and hide, plus giving up its height. Hidden is
     faded, blurred, a --motion-scale step smaller and --rise off; shown is at rest. Showing
     runs on --duration-settle with the restack and hiding on the quicker --duration-pane;
     either reverses from wherever the other left off. */
  .queue,
  .fold {
    --haze: 2px;
    opacity: 0;
    transform: translateY(var(--rise)) scale(var(--motion-scale));
    filter: blur(var(--haze));
    will-change: transform, opacity, filter;
  }

  .queue.is-open,
  .fold.is-open {
    opacity: 1;
    transform: translateY(0) scale(1);
    filter: blur(0);
    transition-duration: var(--duration-settle);
  }

  @starting-style {
    .queue.is-open {
      opacity: 0;
      transform: translateY(var(--rise)) scale(var(--motion-scale));
      filter: blur(var(--haze));
    }
  }

  /* The queue's own surface, so all of it comes and goes together. The gap under the card is
     content rather than padding: a sticky footer stops at the padding edge, and card text
     would show beneath it. */
  .queue {
    --rise: 16px;
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    padding-top: 8px;
    background: var(--surface-0);
    box-shadow: 0 -1px 0 var(--border);
    transition:
      opacity var(--duration-pane) var(--ease-out),
      transform var(--duration-pane) var(--ease-out),
      filter var(--duration-pane) var(--ease-out);
  }

  .queue::after {
    content: "";
    height: 10px;
  }

  /* Clipped, not scrolled, so the card's sticky footer still sticks to the queue's scroller;
     the margin is the front card's focus ring. It folds up, into the chip: pushed down, its
     clipped box would hang past the queue's foot and give the scroller an empty scrollbar. */
  .fold {
    --rise: calc(var(--gap-md) * -1);
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: 0fr;
    overflow: clip;
    overflow-clip-margin: var(--gap-xs);
    transform-origin: top;
    transition:
      grid-template-rows var(--duration-pane) var(--ease-out),
      opacity var(--duration-pane) var(--ease-out),
      transform var(--duration-pane) var(--ease-out),
      filter var(--duration-pane) var(--ease-out);
  }

  .fold.is-open {
    grid-template-rows: 1fr;
  }

  /* Bare, so nothing of the stack's own margin or padding holds a folded row open. */
  .fold-body {
    min-width: 0;
    min-height: 0;
  }

  /* Everything lands at once, unmoved and unblurred. The layers behind step up and in by their
     box instead of a transform, so the stack still reads as a stack. */
  @media (prefers-reduced-motion: reduce) {
    .layer,
    .queue,
    .fold {
      transition: none !important;
      transform: none !important;
      filter: none !important;
    }

    .layer[data-depth="1"] {
      inset: calc(var(--plates-height) - var(--step)) 3% 0;
    }

    .layer[data-depth="2"] {
      inset: calc(var(--plates-height) - var(--step) * 2) 6% 0;
    }

    .viewport,
    .caret,
    .head-pager {
      transition: none !important;
    }
  }

  /* The count stays in view while a tall banner scrolls under it. As tall as the pager it
     drops when folded, so folding never moves the room the chat keeps clear for it. */
  .stack-head {
    position: sticky;
    top: 0;
    z-index: 1;
    min-width: 0;
    min-height: var(--control-height);
    display: flex;
    flex-wrap: wrap;
    gap: 2px 0;
    align-items: center;
    background: var(--surface-0);
  }

  .breakdown {
    min-width: 0;
    padding-inline-start: 6px;
    color: var(--text-3);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }

  /* Out of the way until the reader reaches for the header; always there on touch, which
     has no hover to reveal it. */
  .head-pager {
    margin-inline-start: auto;
    opacity: 0.5;
    transition: opacity var(--duration-fast) var(--ease-out);
  }

  .stack-head:hover .head-pager,
  .stack-head:focus-within .head-pager {
    opacity: 1;
  }

  @media (hover: none) {
    .head-pager {
      opacity: 1;
    }
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>
