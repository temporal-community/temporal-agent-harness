<script lang="ts">
  import { Handle, Position } from "@xyflow/svelte";
  import type { AgentNodeData } from "$lib/state/flowProjection";
  import StatusChip, {
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";

  interface Props {
    data: AgentNodeData;
    selected?: boolean;
  }

  let { data, selected = false }: Props = $props();

  let resultBody = $state<HTMLElement | null>(null);
  /**
   * Mirrors the replay transport's own following/pinned distinction rather than
   * inventing a second vocabulary for it. There, `following` is `viewIndex ===
   * total` — a fact about where the cursor IS, recomputed on every goTo, not a
   * mode anyone toggles. Here it is `scrollTop + clientHeight === scrollHeight`,
   * recomputed on every scroll, and arriving text is allowed to move the box
   * only while it holds. cursorAfterPublish() is the same decision one layer up:
   * following means tail the live edge, not following means someone scrolled
   * back to read something and must not be dragged off it.
   */
  let following = $state(true);
  /* A fade on the edge that is hiding something, so the region reads as clipped
     rather than as mysteriously ending. Each edge separately: while following,
     the newest text is against the bottom and a fade there would dim the one
     line the reader is waiting for. */
  let clippedTop = $state(false);
  let clippedBottom = $state(false);

  function atEnd(element: HTMLElement): boolean {
    /* Two pixels of slack: line boxes land on fractional heights, so an exact
       comparison reports "not at the end" while sitting at the end. */
    return element.scrollHeight - element.scrollTop - element.clientHeight <= 2;
  }

  function syncEdges(element: HTMLElement): void {
    clippedTop = element.scrollTop > 2;
    clippedBottom = !atEnd(element);
  }

  function handleScroll(): void {
    const element = resultBody;
    if (!element) return;
    following = atEnd(element);
    syncEdges(element);
  }

  /* Jumped, never animated. This runs once per streamed delta — a smooth scroll
     that is restarted every few milliseconds never arrives, and reads as lag
     rather than as smoothness. */
  $effect(() => {
    data.detail;
    const element = resultBody;
    if (!element) return;
    if (following) element.scrollTop = element.scrollHeight;
    syncEdges(element);
  });

  function scriptFromDetail(detail: unknown): string | null {
    if (typeof detail !== "string") return null;
    try {
      const parsed = JSON.parse(detail);
      if (typeof parsed?.script === "string") return parsed.script;
      if (typeof parsed?.payload?.script === "string") return parsed.payload.script;
    } catch {
      return null;
    }
    return null;
  }

  const nodeStyle = $derived(
    [
      typeof data.nodeWidth === "number" ? `width: ${data.nodeWidth}px;` : null,
      typeof data.nodeHeight === "number" ? `min-height: ${data.nodeHeight}px;` : null
    ]
      .filter(Boolean)
      .join(" ")
  );
  const detailScript = $derived(scriptFromDetail(data.detail));
  const statusKind = $derived(kindFromState(data.state, data.statusTone ?? data.tone));
  /* Present, not truthy. An empty reply is still a reply slot, and rendering the
     region only once text arrives is what made the card grow mid-turn. */
  const hasResult = $derived(typeof data.detail === "string");
  /* Same vocabulary kindFromState reads, and only for the node the cursor is on,
     so exactly one caret blinks during a scrub. */
  const streaming = $derived(
    data.active === true && /streaming|running/.test(data.state.toLowerCase())
  );

  function kindFromState(
    state: string,
    tone: AgentNodeData["tone"] | undefined
  ): StatusKind {
    const normalized = state.toLowerCase();
    if (normalized.includes("fail") || normalized.includes("error") || normalized.includes("denied")) {
      return "error";
    }
    if (normalized.includes("approval") || normalized.includes("pending") || normalized.includes("await")) {
      return "approval";
    }
    if (normalized.includes("queue") || normalized.includes("requested") || normalized.includes("dispatch")) {
      return "queued";
    }
    if (
      normalized.includes("done") ||
      normalized.includes("complete") ||
      normalized.includes("replied") ||
      normalized.includes("captured") ||
      normalized.includes("approved") ||
      normalized.includes("stopped")
    ) {
      return "complete";
    }
    if (normalized.includes("running") || normalized.includes("streaming")) {
      if (tone === "tool") return "tool";
      if (tone === "model") return "model";
      if (tone === "reasoning") return "reasoning";
      if (tone === "queue") return "queued";
      return "thinking";
    }
    if (tone === "tool") return "tool";
    if (tone === "model") return "model";
    if (tone === "reasoning") return "reasoning";
    if (tone === "approval") return "approval";
    if (tone === "queue") return "queued";
    return "idle";
  }
</script>

<div
  class={`state-node ${data.tone} ${data.size ?? "default"} ${data.active ? "active" : ""} ${selected ? "selected" : ""}`}
  style={nodeStyle}
>
  <Handle id="target-left" class="node-handle" type="target" position={Position.Left} />
  <Handle id="target-top" class="node-handle" type="target" position={Position.Top} />
  <Handle id="target-bottom" class="node-handle" type="target" position={Position.Bottom} />
  {#if data.approvalPort}
    <Handle
      id="approval-out"
      class="node-handle approval-port tool-approval-port"
      type="source"
      position={Position.Left}
    />
  {/if}
  {#if data.approvalDecisionPort}
    <Handle
      id="approval-request-in"
      class="node-handle approval-port decision-approval-port"
      type="target"
      position={Position.Right}
    />
  {/if}
  <div class="topline">
    <span class="title-wrap">
      {#if data.dotTone}
        <span class={`title-dot ${data.dotTone}`} aria-hidden="true"></span>
      {/if}
      <span class="title">{data.title}</span>
    </span>
    <StatusChip
      label={data.state}
      kind={statusKind}
      compact
      active={data.active}
    />
  </div>
  {#if data.subtitle}
    <div class="subtitle">{data.subtitle}</div>
  {/if}
  {#if hasResult}
    <div
      class="result"
      class:code={detailScript != null}
      class:clipped-top={clippedTop}
      class:clipped-bottom={clippedBottom}
    >
      <div class="result-head">
        <span class="kicker">Result</span>
        <!-- The chip above already says "streaming" in words; this is the same
             fact where the text is, so an in-progress reply is distinguishable
             from a finished one without reading the status. -->
        {#if streaming}
          <span class="caret" aria-hidden="true"></span>
        {/if}
      </div>
      {#if detailScript}
        <pre
          class="result-body code"
          data-language="python"
          bind:this={resultBody}
          onscroll={handleScroll}
        ><code>{detailScript}</code></pre>
      {:else}
        <div
          class="result-body"
          bind:this={resultBody}
          onscroll={handleScroll}
        >{data.detail}</div>
      {/if}
    </div>
  {/if}
  {#if data.metrics?.length}
    <div class="metrics kicker">
      {#each data.metrics as metric}
        <span><strong>{metric.value}</strong>{metric.label}</span>
      {/each}
    </div>
  {/if}
  <Handle id="source-right" class="node-handle" type="source" position={Position.Right} />
  <Handle id="source-top" class="node-handle" type="source" position={Position.Top} />
  <Handle id="source-bottom" class="node-handle" type="source" position={Position.Bottom} />
</div>

<style>
  .state-node {
    position: relative;
    width: 230px;
    min-height: 96px;
    padding: 12px;
    overflow: hidden;
    border: 1px solid color-mix(in srgb, var(--tone-color, var(--text-2)) 18%, var(--border));
    border-radius: var(--radius-md);
    background:
      linear-gradient(
        180deg,
        color-mix(in srgb, var(--tone-color, var(--text-2)) 9%, var(--surface-2)),
        color-mix(in srgb, var(--surface-1) 88%, black)
      );
    color: var(--text-1);
    box-shadow: var(--shadow-node);
  }

  .state-node::before {
    content: "";
    position: absolute;
    inset: 0 auto 0 0;
    width: 3px;
    background: color-mix(in srgb, var(--tone-color, var(--text-2)) 86%, white 4%);
    opacity: 0.78;
  }

  .state-node.selected {
    outline: 2px solid color-mix(in srgb, var(--accent) 65%, transparent);
  }

  /* Same three cues the transcript spends on the row at the playhead
     (.turn-group.active-turn): the hairline brightens neutrally, the surface
     lifts, and the left edge marker fills in. Not a brighter edge in the tone,
     which is what this used to be — colour on this card already names the
     node's KIND, so a lit-up kind hue has to say "current" through a channel
     that is taken, and the only way it could be heard over eight equally
     colourful siblings was to get thick. A surface that is simply lighter than
     every other card is findable at a glance and adds no stroke at all. */
  .state-node.active {
    border-color: var(--border-strong);
    background: linear-gradient(
      180deg,
      color-mix(in srgb, var(--tone-color, var(--text-2)) 14%, var(--surface-2)),
      var(--surface-1)
    );
  }

  .state-node.active::before {
    width: 4px;
    opacity: 1;
    box-shadow: var(--shadow-node-soft);
  }

  .state-node.large {
    width: 255px;
    min-height: 112px;
  }

  .state-node.container {
    background: color-mix(in srgb, var(--tone-color, var(--warning)) 12%, transparent);
    box-shadow: var(--shadow-node-soft);
  }

  /* A container's own body has to fit above the children laid out inside it,
     which start at codeModeHeaderHeight (126px) in flowProjection. */
  .state-node.container {
    --result-height: 26px;
  }

  .neutral { --tone-color: var(--text-3); }
  .agent { --tone-color: var(--accent); }
  .model { --tone-color: var(--model); }
  .reasoning { --tone-color: var(--reasoning); }
  .tool { --tone-color: var(--warning); }
  .approval,
  .queue { --tone-color: var(--queue); }
  .done { --tone-color: var(--success); }
  .error { --tone-color: var(--error); }

  .topline {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }

  .title {
    font-size: var(--font-lg);
    font-weight: 700;
  }

  .title-wrap {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }

  .title-dot {
    flex: 0 0 auto;
    width: 8px;
    height: 8px;
    border-radius: var(--radius-chip);
    background: var(--text-3);
    box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 10%, transparent);
  }

  .title-dot.model { background: var(--model); }
  .title-dot.reasoning { background: var(--reasoning); }
  .title-dot.tool { background: var(--warning); }
  .title-dot.approval { background: var(--queue); }

  :global(.node-handle) {
    width: 1px;
    height: 1px;
    border: 0;
    opacity: 0;
    pointer-events: none;
  }

  :global(.node-handle.approval-port) {
    width: 9px;
    height: 9px;
    border: 1px solid color-mix(in srgb, var(--queue) 76%, white 10%);
    background: var(--queue);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--queue) 24%, transparent);
    opacity: 1;
  }

  :global(.node-handle.tool-approval-port) {
    top: 72px;
  }

  /* One line, always. A card's height is reserved by flowProjection from data,
     so anything above the Result region that can wrap makes the card outgrow its
     reservation for some values and not others — which is the same content-driven
     geometry the fixed region exists to remove. What gets clipped here is
     readable in full in the node inspector. */
  .subtitle {
    margin-top: 7px;
    overflow: hidden;
    color: var(--text-2);
    font-size: var(--font-md);
    line-height: 1.35;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /**
   * FIXED, not content-sized, and deliberately not animated.
   *
   * The graph re-renders on every streamed delta while someone scrubs, so a
   * region that grew with its text would relayout the card hundreds of times a
   * turn and the node would move under the cursor. Measured on the run this came
   * from (1,805 reply deltas), the old `max-height: 58px` card stepped 96px ->
   * 137px as the reply crossed four lines; every other node held still. So the
   * height is a constant, the overflow scrolls, and the fade says it is clipped.
   *
   * 132px holds ~8 lines at the 11px/1.35 body scale — roughly a paragraph,
   * against the ~4 lines that were visible before. It lands the card at 231px,
   * which flowProjection reserves as resultNodeHeight; measured on the same run,
   * the graph's bounds are still set by the runtime boundary, so auto-fit holds
   * the same zoom it did at 137px and the extra height costs no type size.
   */
  .result {
    --result-top: calc(16px + var(--gap-xs));
    position: relative;
    margin-top: 8px;
  }

  /* A definite height, so the card's own height stays a constant the projection
     can reserve — and so the fades know where the body starts. */
  .result-head {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    height: 16px;
    margin-bottom: var(--gap-xs);
    color: var(--text-4);
  }

  /* A terminal caret, because that is what the region behaves like. */
  .caret {
    width: 2px;
    height: 9px;
    background: var(--tone-color, var(--text-2));
    animation: caret-blink 1.1s steps(2, jump-none) infinite;
  }

  @keyframes caret-blink {
    to {
      opacity: 0.15;
    }
  }

  .result-body {
    height: var(--result-height, 132px);
    max-width: 100%;
    margin: 0;
    padding: var(--gap-sm);
    overflow-y: auto;
    overflow-x: hidden;
    border: 1px solid color-mix(in srgb, var(--tone-color, var(--text-3)) 20%, var(--border));
    background: var(--result-bg);
    color: var(--text-3);
    font-size: var(--font-sm);
    line-height: 1.35;
    white-space: pre-wrap;
    word-break: break-word;
    overscroll-behavior: contain;
  }

  .state-node {
    --result-bg: color-mix(in srgb, var(--surface-0) 88%, black 6%);
  }

  /* Inside the border, so a fade ends where the box does. Only on the edge that
     is actually hiding something — see clippedTop/clippedBottom above. */
  .result::before,
  .result::after {
    content: "";
    position: absolute;
    right: 1px;
    left: 1px;
    height: 20px;
    opacity: 0;
    pointer-events: none;
  }

  .result::before {
    top: calc(var(--result-top) + 1px);
    background: linear-gradient(to bottom, var(--result-bg), transparent);
  }

  .result::after {
    bottom: 1px;
    background: linear-gradient(to top, var(--result-bg), transparent);
  }

  .result.clipped-top::before,
  .result.clipped-bottom::after {
    opacity: 1;
  }

  /* app.css owns how a code block looks; this only says where the language tag
     goes and that it keeps the region's height contract. On the wrapper, not the
     `pre`, because the fade above reads --result-bg from `.result`. */
  .result.code {
    --result-bg: var(--code-block-bg);
  }

  .result-body.code {
    border-color: var(--code-block-border);
    color: var(--code-block-text);
    box-shadow: var(--code-block-shadow);
    font-family: var(--font-mono);
    line-height: 1.55;
    tab-size: 2;
    white-space: pre;
    word-break: normal;
    overflow-x: auto;
  }

  /* On the head row opposite the "Result" label, rather than floating over the
     body — inside the body it is a child of the scroller and scrolls away. */
  .result-body.code::before {
    position: absolute;
    top: 0;
    right: 0;
  }

  .result-body code {
    display: block;
    font: inherit;
  }

  /* The caret is the one thing here that moves, and it repeats forever. */
  @media (prefers-reduced-motion: reduce) {
    .caret {
      animation: none;
    }
  }

  .metrics {
    display: flex;
    gap: 8px;
    margin-top: 10px;
  }

  .metrics span {
    display: inline-flex;
    flex-direction: column;
    gap: 2px;
  }

  .metrics strong {
    color: var(--text-1);
    font-size: var(--font-md);
    font-variant-numeric: tabular-nums;
  }
</style>
