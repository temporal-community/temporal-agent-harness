<script lang="ts" module>
  import { createContext } from "svelte";
  import type { AgentNodeData } from "$lib/state/flowProjection";
  import { STATUS_TONES, type StatusKind } from "$lib/components/primitives/StatusChip.svelte";

  /** How a card asks the canvas to open its inspector; the canvas owns the dialog. */
  export const [getNodeOpener, setNodeOpener, hasNodeOpener] =
    createContext<(nodeId: string) => void>();

  /**
   * Which chip a card wears, read off the words already on it.
   *
   * Exported for its test: this is a
   * pure decision with a lot of branches and no way to reach it from a rendered
   * node, because a node needs SvelteFlow's context to render at all.
   *
   * State first, tone second. The state string is what the card is saying right
   * now and the tone is only what kind of thing it is, so a running tool has to
   * beat a tool that is merely toned.
   */
  export function kindFromState(
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
    if (normalized.includes("delegating")) return "delegating";
    if (normalized.includes("closed")) return "closed";
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
    if (tone === "error") return "error";
    /* Last, so a state string still outranks it. Without this a node toned done
       whose state says so in its own words — an Output card reading "available"
       — fell through to the neutral dot, which is the one chip that means
       nothing has happened yet. */
    if (tone === "done") return "complete";
    return "idle";
  }

  /** Where the result box scrolls when its text changes; null leaves the reader where they are. */
  export function resultScrollTop(
    element: { scrollHeight: number },
    streaming: boolean,
    following: boolean
  ): number | null {
    if (!streaming) return 0;
    return following ? element.scrollHeight : null;
  }
</script>

<script lang="ts">
  import { Maximize2 } from "@lucide/svelte";
  import { Handle, Position } from "@xyflow/svelte";
  import { drawnPart } from "$lib/components/chat/JsonReply.svelte";
  import MarkdownMessage from "$lib/components/chat/MarkdownMessage.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import MetricStrip from "$lib/components/primitives/MetricStrip.svelte";
  import StateGlyph from "$lib/components/primitives/StateGlyph.svelte";
  import StatusChip from "$lib/components/primitives/StatusChip.svelte";

  interface Props {
    id: string;
    data: AgentNodeData;
    selected?: boolean;
  }

  let { id, data, selected = false }: Props = $props();

  const openNode = hasNodeOpener() ? getNodeOpener() : null;

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
  /* Not reactive: a scroll must never re-run the effect below, or scrolling a finished
     reply would snap it back to its top. */
  let following = true;
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
     rather than as smoothness. Only a stream is tailed: a finished reply opens at
     its top, or a nested payload shows nothing but its closing braces. */
  $effect(() => {
    data.detail;
    const element = resultBody;
    if (!element) return;
    const top = resultScrollTop(element, streaming, following);
    if (top !== null) element.scrollTop = top;
    syncEdges(element);
  });

  function scriptFromDetail(detail: unknown): string | null {
    /* The substring test first: a megabyte reply would otherwise be parsed on every scrub step. */
    if (typeof detail !== "string" || !detail.includes('"script"')) return null;
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
      typeof data.nodeHeight === "number" ? `min-height: ${data.nodeHeight}px;` : null,
      /* Inline, so it beats the .container rule below: a Code Mode host reserves
         room for its script in the same number flowProjection lays out against. */
      typeof data.resultHeight === "number" ? `--result-height: ${data.resultHeight}px;` : null
    ]
      .filter(Boolean)
      .join(" ")
  );
  const detailScript = $derived(scriptFromDetail(data.detail));
  const statusKind = $derived(kindFromState(data.state, data.statusTone ?? data.tone));
  /* Present, not truthy. An empty reply is still a reply slot, and rendering the
     region only once text arrives is what made the card grow mid-turn. */
  const hasResult = $derived(typeof data.detail === "string");
  /* Same vocabulary kindFromState reads. Only the node the cursor is on tails
     its text, so a scrub never drags a card the reader is not looking at. */
  const running = $derived(/streaming|running/.test(data.state.toLowerCase()));
  const streaming = $derived(data.active === true && running);
  /* A string, so an equal one stops here: a graph update that leaves this card's
     text alone never reaches MarkdownMessage's parse. */
  const drawnDetail = $derived(drawnPart(data.detail ?? "", streaming));
  /* By length rather than by measuring, so no card observes its own size: about
     this many characters fill the title's share of a 230px header. A title near
     the line may clip without a tip. */
  const titleTip = $derived(data.title.length > 18 ? data.title : undefined);

  /* A hidden handle, drawn as a port in its edge's color only when an edge uses it. */
  function port(handle: string) {
    const color = data.ports?.[handle];
    return {
      id: handle,
      class: color ? "node-handle port" : "node-handle",
      style: color ? `--port-color: ${color}` : undefined,
      isConnectable: false,
      "aria-hidden": true
    } as const;
  }
</script>

<div
  class={[
    "state-node",
    `status-${STATUS_TONES[statusKind]}`,
    data.size ?? "default",
    data.active && "active",
    selected && "selected"
  ]}
  style={nodeStyle}
>
  <Handle {...port("target-left")} type="target" position={Position.Left} />
  <Handle {...port("target-top")} type="target" position={Position.Top} />
  <Handle {...port("target-bottom")} type="target" position={Position.Bottom} />
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
  <div class="head">
    <!-- The tip sits on the wrapper because the title clips its own overflow,
         and a tip is drawn with the element's pseudo-elements. -->
    <span class="title-wrap" data-tip={titleTip} data-tip-below>
      <StateGlyph kind={statusKind} {running} />
      <span class="title" dir="auto">{data.title}</span>
    </span>
    <StatusChip label={data.state} kind={statusKind} plain />
  </div>
  <div class="body">
    {#if data.subtitle}
      <span class="kind" dir="auto">{data.subtitle}</span>
    {/if}
    {#if hasResult}
      <div
        class={[
          "result",
          { code: detailScript != null, "clipped-top": clippedTop, "clipped-bottom": clippedBottom }
        ]}
      >
        {#if detailScript}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex: a scroller a keyboard must reach -->
          <pre
            class="result-body code"
            data-language="python"
            tabindex="0"
            bind:this={resultBody}
            onscroll={handleScroll}
          ><code>{detailScript}</code></pre>
        {:else if data.detail}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex: a scroller a keyboard must reach -->
          <div
            class={["result-body", { markdown: data.markdown }]}
            dir="auto"
            tabindex="0"
            role="group"
            aria-label={`${data.title} result`}
            bind:this={resultBody}
            onscroll={handleScroll}
          >{#if data.markdown}<MarkdownMessage text={drawnDetail} streaming={running} />{:else}{drawnDetail}{/if}</div>
        {:else}
          <p class="result-empty">{running ? "Waiting for output…" : "No result yet"}</p>
        {/if}
      </div>
    {/if}
    {#if data.metrics?.length}
      <MetricStrip metrics={data.metrics} dense bare />
    {/if}
  </div>
  {#if openNode}
    <div class="actions">
      <IconButton
        class="nodrag nopan"
        label={`Open ${data.title} details`}
        tip="Open details"
        onclick={(event) => {
          event.stopPropagation();
          openNode(id);
        }}
      >
        <Maximize2 size={14} />
      </IconButton>
    </div>
  {/if}
  <Handle {...port("source-right")} type="source" position={Position.Right} />
  <Handle {...port("source-top")} type="source" position={Position.Top} />
  <Handle {...port("source-bottom")} type="source" position={Position.Bottom} />
</div>

<style>
  /* A column whose height flowProjection reserves (min-height, inline), so the
     body flexes into whatever the header and tag leave rather than sizing to
     its text — the card must not grow as a reply streams in. */
  .state-node {
    --status-color: var(--text-3);
    --result-bg: var(--surface-1);
    position: relative;
    display: flex;
    flex-direction: column;
    width: 230px;
    min-height: 96px;
    border: 1px solid color-mix(in srgb, var(--status-color) 30%, var(--border));
    border-radius: var(--radius-md);
    background: var(--surface-1);
    color: var(--text-1);
    box-shadow: var(--shadow-node);
  }

  .status-neutral { --status-color: var(--text-3); }
  .status-accent { --status-color: var(--accent); }
  .status-model { --status-color: var(--model); }
  .status-reasoning { --status-color: var(--reasoning); }
  .status-tool { --status-color: var(--tool); }
  .status-queue { --status-color: var(--queue); }
  .status-retry { --status-color: var(--retry); }
  .status-success { --status-color: var(--success); }
  .status-error { --status-color: var(--error); }
  .status-live { --status-color: var(--live); }

  /* The state lives in the band, so the word on it needs no box of its own. */
  .head {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-md);
    min-width: 0;
    height: 36px;
    padding: 0 10px;
    border-bottom: 1px solid color-mix(in srgb, var(--status-color) 22%, var(--border));
    background: color-mix(in srgb, var(--status-color) 12%, var(--surface-2));
  }

  /* A status is a closed vocabulary, and one clipped character makes it ambiguous:
     "AWAITIN…" reads as either `awaiting` or `awaiting approval`. So the word
     does not shrink and the title yields instead. The cap only catches the
     free-form subagent state ("fn → turn n"); see replyState in flowProjection. */
  .head :global(.chip) {
    flex: 0 0 auto;
    max-width: 128px;
  }

  .title-wrap {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
  }

  .title {
    min-width: 0;
    overflow: hidden;
    font-size: var(--font-lg);
    font-weight: 700;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Same cues the transcript spends on the playhead row: the hairline
     brightens and the band takes a fuller wash. */
  .state-node.active {
    border-color: color-mix(in srgb, var(--status-color) 55%, var(--border-strong));
  }

  .state-node.active .head {
    background: color-mix(in srgb, var(--status-color) 24%, var(--surface-2));
  }

  /* Chip's active recipe, not a bright outline: selection follows every click,
     so it has to sit quietly once the inspector closes. Keyboard focus keeps
     the app's ring, offset outward (AgentStateFlow), so the two never merge. */
  .state-node.selected {
    border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
    box-shadow:
      0 0 0 1px color-mix(in srgb, var(--accent) 35%, transparent),
      var(--shadow-node);
  }

  .state-node.large {
    width: 255px;
    min-height: 112px;
  }

  /* A container's own body has to fit above the children laid out inside it.
     A Code Mode host overrides --result-height inline with the height
     flowProjection reserved for its script. */
  .state-node.container {
    --result-height: 26px;
    --result-bg: color-mix(in srgb, var(--status-color) 6%, var(--surface-1));
    background: var(--result-bg);
    box-shadow: var(--shadow-node-soft);
  }

  .body {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: var(--gap-md) 10px 10px;
  }

  /* A label, so no box, and not the uppercase label recipe: this is often a
     model id, which is case-sensitive. One line, always — anything above the
     preview that could wrap would outgrow the height flowProjection reserved. */
  .kind {
    flex: 0 0 auto;
    overflow: hidden;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Zero-sized, min-size included: xyflow's 5px minimum centered the handle on
     the border, which ended every edge 2.5px short of the card. The -0.5px
     margin is half the card's border, so the edge ends on the border's midline. */
  :global(.node-handle) {
    width: 0;
    height: 0;
    min-width: 0;
    min-height: 0;
    margin: -0.5px;
    border: 0;
    opacity: 0;
    pointer-events: none;
  }

  /* One status pixel, square like every pip, centered on the edge's end. */
  :global(.node-handle.port) {
    opacity: 1;
  }

  /* The flow colors mix toward the translucent --border-strong, so the port is
     laid over the canvas: the same color its edge shows, but solid, so the card
     border and the edge underneath do not show through it. */
  :global(.node-handle.port::before) {
    content: "";
    position: absolute;
    inset: calc(var(--pip) / -2);
    background: linear-gradient(var(--port-color), var(--port-color)), var(--flow-canvas);
  }

  :global(.node-handle.approval-port) {
    width: 9px;
    height: 9px;
    border: 1px solid color-mix(in srgb, var(--live) 76%, var(--text-1) 10%);
    background: var(--live);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--live) 24%, transparent);
    opacity: 1;
  }

  :global(.node-handle.tool-approval-port) {
    top: 72px;
  }

  /**
   * Flexed into the reserved height, never sized by its text, and not animated:
   * the graph re-renders on every streamed delta, and a region that grew with
   * its text moved the node under the cursor hundreds of times a turn. The
   * overflow scrolls and the fades say it is clipped; the full text is one
   * click away in the inspector.
   */
  .result {
    position: relative;
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .state-node.container .result {
    flex: 0 0 var(--result-height);
  }

  .result-body {
    flex: 1 1 0;
    min-height: 0;
    margin: 0;
    overflow-x: hidden;
    overflow-y: auto;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.4;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    overscroll-behavior: contain;
  }

  /* The chat's renderer at the card's density: headings at body size, tight
     blocks, and fences without the copy strip, which the inspector carries. */
  .result-body.markdown {
    --markdown-block-gap: 4px;
    --markdown-list-gap: 1px;
    --markdown-body-size: var(--font-sm);
    --markdown-body-line-height: 1.4;
    --markdown-heading-size: var(--font-sm);
    --markdown-heading-line-height: 1.4;
    /* A thumbnail inside the reserved height; the full image is one click away. */
    --image-max-height: 64px;
    white-space: normal;
  }

  .result :global(.markdown-message ul),
  .result :global(.markdown-message ol) {
    padding-left: 0;
    padding-inline-start: 16px;
  }

  .result :global(.markdown-message code) {
    font-size: inherit;
  }

  .result :global(.markdown-message .md-code-head) {
    display: none;
  }

  /* The card is the one scroller; a fence still streaming is not capped into a second. */
  .result :global(.markdown-message pre.md-code-block) {
    max-height: none;
    padding: var(--gap-sm);
  }

  .result :global(.markdown-message table) {
    min-width: 360px;
    font-size: inherit;
  }

  .result :global(.markdown-message th),
  .result :global(.markdown-message td) {
    padding: 3px 6px;
  }

  .result-body:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .result-empty {
    margin: 0;
    color: var(--text-4);
    font-size: var(--font-sm);
  }

  /* Only on the edge that is actually hiding something — see clippedTop/clippedBottom. */
  .result::before,
  .result::after {
    content: "";
    position: absolute;
    right: 0;
    left: 0;
    height: 20px;
    opacity: 0;
    pointer-events: none;
  }

  .result::before {
    top: 0;
    background: linear-gradient(to bottom, var(--result-bg), transparent);
  }

  .result::after {
    bottom: 0;
    background: linear-gradient(to top, var(--result-bg), transparent);
  }

  .result.clipped-top::before,
  .result.clipped-bottom::after {
    opacity: 1;
  }

  /* app.css owns how a code block looks, box included; this only keeps it to
     the region's height and pins the language tag to the wrapper, because a
     tag inside the scroller scrolls away. */
  .result.code {
    --result-bg: var(--code-block-bg);
  }

  .result.code::before {
    top: 1px;
  }

  .result.code::after {
    bottom: 1px;
  }

  .result-body.code {
    padding: var(--gap-sm);
    border: 1px solid var(--code-block-border);
    background: var(--code-block-bg);
    color: var(--code-block-text);
    box-shadow: var(--code-block-shadow);
    font-family: var(--font-mono);
    line-height: 1.55;
    tab-size: 2;
    white-space: pre;
    overflow-wrap: normal;
    overflow-x: auto;
  }

  .result-body.code::before {
    position: absolute;
    top: 0;
    right: 0;
  }

  .result-body code {
    display: block;
    font: inherit;
  }

  /* Revealed where the reader is: on hover, or when the card or its button has
     keyboard focus. A touch screen has no hover to reveal it with. */
  .actions {
    position: absolute;
    right: var(--gap-md);
    bottom: var(--gap-md);
    opacity: 0;
    transition: opacity var(--duration-fast) var(--ease-out);
  }

  .state-node:hover .actions,
  .state-node:focus-within .actions,
  :global(.svelte-flow__node:focus-visible) .actions,
  :global(.svelte-flow__node.selected) .actions {
    opacity: 1;
  }

  @media (hover: none) {
    .actions {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .actions {
      transition: none;
    }
  }
</style>
