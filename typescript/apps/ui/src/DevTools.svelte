<script lang="ts" module>
  import { CLIENT_TOOL_STORAGE_PREFIX } from "$lib/state/clientToolProvider";

  export const CORNERS = ["bottom-right", "bottom-left", "top-right", "top-left"] as const;
  export type Corner = (typeof CORNERS)[number];

  /* The fixtures loaded last, newest first: the same few are used all day. */
  const RECENT_KEPT = 4;

  /** The recent list with `name` moved to its front; anything stored that isn't a name is dropped. */
  export function withRecent(stored: unknown, name: string): string[] {
    const names = Array.isArray(stored) ? stored.filter((item): item is string => typeof item === "string") : [];
    return [name, ...names.filter((item) => item !== name)].slice(0, RECENT_KEPT);
  }

  /* How far a throw carries the button past where it was let go, in ms of its last speed. */
  const THROW_MS = 150;

  /** The corner a dragged button snaps to: the one nearest where it was let go, carried a little
   *  further by the throw, as Next.js's indicator does. */
  export function snapCorner(x: number, y: number, vx: number, vy: number, width: number, height: number): Corner {
    const landX = x + vx * THROW_MS;
    const landY = y + vy * THROW_MS;
    return `${landY < height / 2 ? "top" : "bottom"}-${landX < width / 2 ? "left" : "right"}`;
  }

  /* What belongs to the fixture being left: its variant, and the session it had open. */
  const FIXTURE_PARAMS = ["data", "s"];

  /** A fixture's query names only its own parameters; merged into the current URL it keeps
   *  whatever else the page carries, such as its panes, and drops what was the last fixture's. */
  export function fixtureUrl(current: string, link: string): string {
    const url = new URL(current);
    for (const key of FIXTURE_PARAMS) url.searchParams.delete(key);
    for (const [key, value] of new URL(link).searchParams) url.searchParams.set(key, value);
    return url.toString();
  }

  /** The same page against the real server: no fixture, and none of its parameters. */
  export function liveUrl(current: string): string {
    const url = new URL(current);
    for (const key of ["mock", ...FIXTURE_PARAMS]) url.searchParams.delete(key);
    return url.toString();
  }

  const SAVED_PREFIXES = ["temporal-agent-ui.", CLIENT_TOOL_STORAGE_PREFIX];

  /** Forgets what the console saved, as on a first visit, but keeps the dev tools where they were put. */
  export function clearSavedState(storages: Storage[]): void {
    for (const storage of storages) {
      const keys = Array.from({ length: storage.length }, (_, i) => storage.key(i) ?? "");
      for (const key of keys) {
        if (SAVED_PREFIXES.some((prefix) => key.startsWith(prefix)) && !key.startsWith("temporal-agent-ui.dev-tools-")) storage.removeItem(key);
      }
    }
  }
</script>

<script lang="ts">
  /**
   * Dev only: the live-or-mock data controls, folded into one round button in a corner the way TanStack
   * Query's devtools and Next.js's dev indicator are, so a demo or a screenshot shows the console
   * rather than the switches for it. Drawn from the console's own parts: the pane head, its
   * IconButton and a Chip for the badge, with ScenarioPicker for the fixtures.
   */
  import { EyeOff, RotateCcw } from "@lucide/svelte";
  import { untrack } from "svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import type { MockListing } from "$lib/mock/catalog";
  import { clearDevErrors, devErrors } from "./devErrors.svelte";
  import ScenarioPicker from "./ScenarioPicker.svelte";
  interface Props {
    catalog: MockListing[];
    /** The fixture loaded, "" for the live API, and its variant, "" for its first. */
    current: string;
    variant: string;
    /** The fixture on screen, beside the button. */
    badge?: string;
  }

  let { catalog, current, variant, badge }: Props = $props();

  const OPEN_KEY = "temporal-agent-ui.dev-tools-open";
  const HIDDEN_KEY = "temporal-agent-ui.dev-tools-hidden";
  const CORNER_KEY = "temporal-agent-ui.dev-tools-corner";
  const RECENT_KEY = "temporal-agent-ui.dev-tools-recent";
  /* Moved this far, a press is a drag rather than a click. */
  const DRAG_SLOP = 4;

  const saved = localStorage.getItem(CORNER_KEY) as Corner | null;
  let corner = $state<Corner>(saved && CORNERS.includes(saved) ? saved : "bottom-right");
  let open = $state(sessionStorage.getItem(OPEN_KEY) === "1");
  let hidden = $state(sessionStorage.getItem(HIDDEN_KEY) === "1");
  /* Where the button is while it is carried; null once it settles in a corner. */
  let carried = $state<{ left: number; top: number } | null>(null);
  let button = $state<HTMLButtonElement | null>(null);
  let panel = $state<HTMLElement | null>(null);

  /* What the dev server says, from Vite's own events: a change being applied, and whether it is
     still connected. There are none outside `vite dev`, as in a test. The mark turns from the
     start of an update to the end of a turn after it lands, so a change applied in a frame still
     shows, and it stops upright. */
  let updating = $state(false);
  let turning = $state(false);
  let offline = $state(false);
  $effect(() => {
    const hot = import.meta.hot;
    if (!hot) return;
    const listeners: [string, () => void][] = [
      ["vite:beforeUpdate", () => ((updating = true), (turning = !matchMedia("(prefers-reduced-motion: reduce)").matches))],
      ["vite:afterUpdate", () => (updating = false)],
      ["vite:ws:disconnect", () => (offline = true)],
      ["vite:ws:connect", () => (offline = false)]
    ];
    for (const [event, listener] of listeners) hot.on(event, listener);
    return () => {
      for (const [event, listener] of listeners) hot.off(event, listener);
    };
  });
  const status = $derived(
    [offline && "dev server disconnected", devErrors.length && `${devErrors.length} ${devErrors.length === 1 ? "error" : "errors"}`].filter(Boolean).join(", ")
  );

  $effect(() => sessionStorage.setItem(OPEN_KEY, open ? "1" : "0"));
  $effect(() => (hidden ? sessionStorage.setItem(HIDDEN_KEY, "1") : sessionStorage.removeItem(HIDDEN_KEY)));

  /* Loading a fixture reloads the page, so what was loaded is kept where the next page reads it,
     once per page. */
  const recent = untrack(() => withRecent(readRecent(), current));
  localStorage.setItem(RECENT_KEY, JSON.stringify(recent));
  function readRecent(): unknown {
    try {
      return JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    } catch {
      return [];
    }
  }

  function choose(name: string, data: string): void {
    const query = new URLSearchParams({ mock: name, ...(data ? { data } : {}) });
    location.assign(name ? fixtureUrl(location.href, new URL(`?${query}`, location.href).href) : liveUrl(location.href));
  }

  let drag: { id: number; x: number; y: number; left: number; top: number; moved: boolean; t: number; vx: number; vy: number } | null = null;
  let swallowClick = false;
  function press(event: PointerEvent): void {
    if (event.button !== 0 || !button) return;
    const box = button.getBoundingClientRect();
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: box.left, top: box.top, moved: false, t: event.timeStamp, vx: 0, vy: 0 };
    button.setPointerCapture(event.pointerId);
  }
  function move(event: PointerEvent): void {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < DRAG_SLOP) return;
    if (!drag.moved) {
      drag.moved = true;
      open = false;
    }
    const dt = Math.max(1, event.timeStamp - drag.t);
    drag.vx = event.movementX / dt;
    drag.vy = event.movementY / dt;
    drag.t = event.timeStamp;
    carried = { left: drag.left + dx, top: drag.top + dy };
  }
  /* Let go, it snaps to the corner nearest where it was thrown, and stays there across reloads. */
  function release(event: PointerEvent): void {
    if (!drag || event.pointerId !== drag.id) return;
    const { moved, vx, vy } = drag;
    drag = null;
    if (!moved || !button) return;
    swallowClick = true;
    const box = button.getBoundingClientRect();
    corner = snapCorner(box.left + box.width / 2, box.top + box.height / 2, vx, vy, innerWidth, innerHeight);
    localStorage.setItem(CORNER_KEY, corner);
    carried = null;
  }

  function toggle(): void {
    if (swallowClick) swallowClick = false;
    else open = !open;
  }

  function keydown(event: KeyboardEvent): void {
    if (event.altKey && event.code === "KeyD") {
      event.preventDefault();
      if (hidden) {
        hidden = false;
        open = true;
      } else open = !open;
    } else if (event.key === "Escape" && open && panel?.contains(document.activeElement)) {
      open = false;
      button?.focus();
    }
  }
</script>

<svelte:document onkeydown={keydown} />

<div
  class={["dev-tools", carried && "carried"]}
  data-corner={corner}
  hidden={hidden}
  style:left={carried ? `${carried.left}px` : undefined}
  style:top={carried ? `${carried.top}px` : undefined}
>
  {#if open}
    <section class="dev-tools-panel" id="dev-tools-panel" aria-label="Dev tools" bind:this={panel}>
      <header class="surface-head">
        <h2 class="head-name"><span class="kicker head-kind">Dev tools</span><span class="dev-tools-note">Development only · not in production builds</span></h2>
        <IconButton label="Reset saved state" tip="Reset saved state&#10;Layout, preferences and recent runs, as on a first visit" onclick={() => (clearSavedState([localStorage, sessionStorage]), location.reload())}>
          <RotateCcw size={13} aria-hidden="true" />
        </IconButton>
        <IconButton label="Hide dev tools" tip="Hide dev tools for this tab&#10;⌥D brings them back" onclick={() => ((open = false), (hidden = true))}>
          <EyeOff size={13} aria-hidden="true" />
        </IconButton>
      </header>
      <div class="dev-tools-rows">
        <ScenarioPicker {catalog} {current} {variant} {recent} onchoose={choose} />
      </div>
      {#if offline}
        <p class="dev-tools-alert" role="status">Dev server disconnected. Changes won't load until it's back.</p>
      {/if}
      {#if devErrors.length}
        <section class="dev-tools-errors" aria-labelledby="dev-tools-errors-head">
          <header class="dev-tools-errors-head">
            <h3 class="kicker" id="dev-tools-errors-head">Errors <span class="dev-tools-errors-count">{devErrors.length}</span></h3>
            <Chip size="xs" fill="quiet" onclick={clearDevErrors}>Clear</Chip>
          </header>
          <ol class="dev-tools-error-list">
            {#each devErrors as error (error.message)}
              <li><span class="dev-tools-error-message">{error.message}</span>{#if error.count > 1}<span class="dev-tools-error-times">×{error.count}</span>{/if}</li>
            {/each}
          </ol>
          <p class="dev-tools-note">Stack traces are in the browser console.</p>
        </section>
      {/if}
    </section>
  {/if}
  <div class="dev-tools-launcher">
    {#if badge}<Chip size="xs" aria-hidden="true">{badge}</Chip>{/if}
    <button
      bind:this={button}
      type="button"
      class="dev-tools-button"
      aria-label={["Dev tools", badge, status].filter(Boolean).join(", ")}
      aria-expanded={open}
      data-updating={updating || undefined}
      data-turning={turning || undefined}
      data-offline={offline || undefined}
      onanimationiteration={() => !updating && (turning = false)}
      aria-controls="dev-tools-panel"
      data-tip="Dev tools&#10;⌥D"
      data-tip-align="end"
      onpointerdown={press}
      onpointermove={move}
      onpointerup={release}
      onpointercancel={release}
      onclick={toggle}
    >
      <span class="dev-tools-glyph">
        <!-- The mark as two rings, so they can fold into a heart. The notch weaves one ring under
             the other at their lower-right crossing; the seams trim the heart where the rings meet. -->
        <svg class="dev-tools-mark" viewBox="0 0 400 400" aria-hidden="true">
          <defs>
            <clipPath id="dev-tools-notch" clipPathUnits="objectBoundingBox">
              <path
                clip-rule="evenodd"
                d="M-1,-1 H2 V2 H-1 Z M0.62934,0.62934 L0.64034,0.62839 L0.65034,0.62735 L0.66034,0.62624 L0.67034,0.62504 L0.68034,0.62376 L0.69034,0.62239 L0.69034,0.60589 L0.61284,0.61284 L0.61284,0.69034 L0.62239,0.69034 L0.62376,0.68034 L0.62504,0.67034 L0.62624,0.66034 L0.62735,0.65034 L0.62839,0.64034 Z"
              />
            </clipPath>
            <clipPath id="dev-tools-seam-v" clipPathUnits="userSpaceOnUse">
              <rect x="200" y="-600" width="800" height="1600" transform="rotate(135 200 200)" />
            </clipPath>
            <clipPath id="dev-tools-seam-h" clipPathUnits="userSpaceOnUse">
              <rect x="-1000" y="-600" width="1200" height="1600" transform="rotate(135 200 200)" />
            </clipPath>
          </defs>
          <ellipse class="h" cx="200" cy="200" rx="190" ry="64.6" pathLength="100" />
          <ellipse class="v" cx="200" cy="200" rx="64.6" ry="190" pathLength="100" />
        </svg>
      </span>
      {#if devErrors.length && !open}<span class="dev-tools-count" aria-hidden="true">{devErrors.length > 99 ? "99+" : devErrors.length}</span>{/if}
    </button>
  </div>
</div>

<style>
  /* Above the console and its own popovers, in a corner clear of the strips at its top and bottom
     edges. */
  /* Built from the console's parts, but marked as not one of them: a phosphor-terminal chip and a
     hazard band, neither of which the console's own chrome uses, so a maintainer can't take the
     dev tools for part of the product. */
  .dev-tools {
    --dev-accent: var(--success);
    --dev-tools-inset-top: 48px;
    --dev-tools-inset-bottom: 40px;
    position: fixed;
    z-index: 2147483647;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--gap-md);
  }

  .dev-tools[hidden] {
    display: none;
  }

  .dev-tools[data-corner^="top"] {
    top: var(--dev-tools-inset-top);
    flex-direction: column-reverse;
  }

  .dev-tools[data-corner^="bottom"] {
    bottom: var(--dev-tools-inset-bottom);
  }

  .dev-tools[data-corner$="right"] {
    right: var(--gutter);
  }

  .dev-tools[data-corner$="left"] {
    left: var(--gutter);
    align-items: flex-start;
  }

  /* Carried, it goes where the pointer puts it rather than to its corner. */
  .dev-tools.carried {
    inset: auto;
  }

  .dev-tools-launcher {
    display: flex;
    align-items: center;
    gap: var(--gap-md);
  }

  .dev-tools[data-corner$="left"] .dev-tools-launcher {
    flex-direction: row-reverse;
  }

  /* The fixture's name shows only while the button is pointed at or focused; its label says it for
     a screen reader, and the open panel's Scenario control shows it too. */
  .dev-tools-launcher :global(.chip) {
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--duration-fast) var(--ease-out);
  }

  .dev-tools:not(.carried) .dev-tools-launcher:has(.dev-tools-button:hover, .dev-tools-button:focus-visible) :global(.chip) {
    opacity: 1;
  }

  /* The one part with no primitive: the Temporal mark glowing on a round button with scanlines,
     which none of the console's own controls have. */
  .dev-tools-button {
    --dev-beat: var(--duration-settle);
    --dev-snap: var(--ease-out);
    --dev-retime: var(--ease-in-out);
    position: relative;
    width: 44px;
    height: 44px;
    display: grid;
    place-items: center;
    padding: 0;
    border: 1px solid color-mix(in srgb, var(--dev-accent) 40%, transparent);
    border-radius: var(--radius-pill);
    /* Scanlines in the background, not a pseudo-element: ::before and ::after carry its tooltip. */
    background:
      repeating-linear-gradient(to bottom, transparent 0 2px, color-mix(in srgb, var(--dev-accent) 16%, transparent) 2px 3px),
      var(--surface-0);
    box-shadow: 0 4px 14px color-mix(in srgb, var(--surface-0) 60%, transparent);
    color: var(--dev-accent);
    cursor: pointer;
    touch-action: none;
  }

  /* Hover and press move this wrapper, so they never fight the open turn on the mark. */
  .dev-tools-glyph {
    display: grid;
    transition: scale var(--duration-press) var(--ease-out);
  }

  .dev-tools-button:active .dev-tools-glyph {
    scale: 0.9;
    animation: none;
  }

  /* Clips can't be tweened, so each swaps at the end of the move into its shape and at the start
     of the move out of it; in between the rings turn unclipped. */
  .dev-tools-mark {
    display: block;
    width: 26px;
    height: 26px;
    overflow: visible;
    clip-path: url(#dev-tools-notch);
    filter: drop-shadow(0 0 3px currentColor);
    transition:
      translate var(--duration-settle) var(--ease-out),
      opacity var(--duration-fast) var(--ease-out),
      clip-path 0s var(--duration-settle) allow-discrete;
  }

  .dev-tools-mark ellipse {
    fill: none;
    stroke: currentColor;
    stroke-width: 20.25;
    transform-box: view-box;
    transform-origin: 200px 200px;
    transition:
      rotate var(--duration-settle) var(--ease-out),
      stroke-dasharray var(--duration-settle) var(--ease-out),
      stroke-dashoffset var(--duration-settle) var(--ease-out),
      clip-path 0s allow-discrete;
  }

  .dev-tools-mark .v {
    stroke-dasharray: 87.4 12.6;
    stroke-dashoffset: -5.65;
  }

  .dev-tools-mark .h {
    stroke-dasharray: 87.18 12.82;
    stroke-dashoffset: -32.17;
  }

  /* Open, both rings snap three-eighths of a turn and fold into a heart, nudged down because the
     heart's box sits above the mark's center. */
  .dev-tools-button[aria-expanded="true"] .dev-tools-mark {
    translate: 0 6.9%;
    clip-path: none;
    transition:
      translate var(--dev-beat) var(--dev-retime),
      clip-path 0s allow-discrete;
  }

  .dev-tools-button[aria-expanded="true"] .dev-tools-mark ellipse {
    rotate: -135deg;
    transition:
      rotate var(--dev-beat) var(--dev-snap),
      stroke-dasharray var(--dev-beat) var(--dev-retime),
      stroke-dashoffset var(--dev-beat) var(--dev-retime),
      clip-path 0s var(--dev-beat) allow-discrete;
  }

  .dev-tools-button[aria-expanded="true"] .dev-tools-mark .v {
    stroke-dasharray: 52.4 48.1;
    stroke-dashoffset: -6.02;
    clip-path: url(#dev-tools-seam-v);
  }

  .dev-tools-button[aria-expanded="true"] .dev-tools-mark .h {
    stroke-dasharray: 86.95 47.6;
    stroke-dashoffset: -66.58;
    clip-path: url(#dev-tools-seam-h);
  }

  @keyframes dev-tools-pulse {
    0%,
    52%,
    100% {
      scale: 1;
    }
    14% {
      scale: 1.075;
    }
    26% {
      scale: 1;
    }
    38% {
      scale: 1.042;
    }
  }

  /* While Vite applies a change the mark dims and turns, brightening once it lands; open, the
     heart outranks it and the panel says the rest. */
  .dev-tools-button[data-updating] .dev-tools-mark {
    opacity: 0.55;
  }

  .dev-tools-button[data-turning]:not([aria-expanded="true"]) .dev-tools-mark {
    animation: dev-tools-turn 800ms linear infinite;
  }

  @keyframes dev-tools-turn {
    to {
      rotate: 360deg;
    }
  }

  /* With the dev server gone the mark goes grey: nothing changed on disk will load. */
  .dev-tools-button[data-offline] {
    --dev-accent: var(--text-3);
  }

  .dev-tools-count {
    position: absolute;
    top: -4px;
    right: -4px;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    border: 2px solid var(--surface-0);
    border-radius: var(--radius-pill);
    background: var(--error);
    color: var(--surface-0);
    font-family: var(--font-mono);
    font-size: var(--font-xs);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    line-height: 14px;
    text-align: center;
  }

  .dev-tools.carried .dev-tools-button {
    cursor: grabbing;
  }

  .dev-tools-button:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .dev-tools-panel {
    width: min(360px, calc(100vw - 2 * var(--gutter)));
    /* The band's height, which the head starts below: it paints a background of its own. */
    --dev-band: 6px;
    padding-top: var(--dev-band);
    border: 1px solid color-mix(in srgb, var(--dev-accent) 40%, transparent);
    background:
      repeating-linear-gradient(-45deg, var(--dev-accent) 0 6px, var(--surface-0) 6px 12px) top / 100% var(--dev-band) no-repeat,
      var(--surface-1);
  }

  .dev-tools-panel :global(.head-kind) {
    color: var(--dev-accent);
  }

  .dev-tools-panel .head-name {
    display: grid;
    gap: var(--gap-2xs);
  }

  .dev-tools-note {
    color: var(--text-3);
    font-size: var(--font-sm);
    text-transform: none;
    letter-spacing: normal;
  }

  .dev-tools-rows {
    display: grid;
    gap: var(--gap-md);
    padding: var(--gap-lg);
  }

  .dev-tools-alert {
    margin: 0 var(--gap-lg) var(--gap-lg);
    color: var(--warning);
    font-size: var(--font-sm);
  }

  .dev-tools-errors {
    display: grid;
    gap: var(--gap-sm);
    padding: var(--gap-md) var(--gap-lg) var(--gap-lg);
    border-top: 1px solid var(--border);
  }

  .dev-tools-errors-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .dev-tools-errors-head h3 {
    margin: 0;
    color: var(--error);
  }

  .dev-tools-errors-count {
    margin-inline-start: var(--gap-xs);
    color: var(--text-3);
  }

  .dev-tools-error-list {
    display: grid;
    gap: var(--gap-xs);
    max-height: 160px;
    margin: 0;
    padding: 0;
    overflow: auto;
    list-style: none;
  }

  .dev-tools-error-list li {
    display: flex;
    gap: var(--gap-sm);
    align-items: baseline;
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    color: var(--text-2);
  }

  .dev-tools-error-message {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .dev-tools-error-times {
    flex: none;
    color: var(--text-3);
  }

  @media (hover: hover) and (pointer: fine) {
    .dev-tools-button:hover {
      border-color: var(--dev-accent);
    }

    .dev-tools-button:hover .dev-tools-glyph {
      animation: dev-tools-pulse 1600ms var(--ease-out) 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .dev-tools-glyph,
    .dev-tools-launcher :global(.chip),
    .dev-tools-button[aria-expanded] .dev-tools-mark,
    .dev-tools-button[aria-expanded] .dev-tools-mark ellipse {
      transition: none;
    }

    .dev-tools-button:hover .dev-tools-glyph,
    .dev-tools-button:active .dev-tools-glyph,
    .dev-tools-button[data-turning] .dev-tools-mark {
      animation: none;
      scale: none;
    }
  }
</style>
