<script lang="ts">
  import { formatTokens, tokenFigure, UNREPORTED_TIP, type CostSummary, type UsageTimelinePoint } from "$lib/cost/pricing";

  interface Props {
    /** At the cursor: what each model has spent so far. */
    usage: CostSummary;
    /** The whole run's calls, which fix the rows' order and the bars' scale. */
    points: UsageTimelinePoint[];
    viewIndex?: number;
  }

  let { usage, points, viewIndex = Number.POSITIVE_INFINITY }: Props = $props();
  const titleId = $props.id();

  /* Every model the run will call, ordered by what it spends by the end (ties by
     first call), so scrubbing never reorders the list or adds a row: a row arriving
     mid-scrub resized the chart above it. */
  const finals = $derived.by(() => {
    /* No prototype, so an id like "__proto__" is just another key. */
    const totals: Record<string, number> = Object.create(null);
    for (const point of points) totals[point.model] = (totals[point.model] ?? 0) + (point.call?.total ?? 0);
    return Object.entries(totals)
      .map(([model, total]) => ({ model, total }))
      .sort((a, b) => b.total - a.total);
  });
  const atCursor = $derived(new Map(usage.modelBreakdown.map((row) => [row.model, row])));
  /* Bars are against the largest final total, so they only grow while you scrub and the
     biggest spender fills its track by the end. */
  const finalMax = $derived(finals.reduce((max, row) => Math.max(max, row.total), 0));
  /* Room for the widest figure the run reaches, so the columns never shift. */
  const valueCh = $derived(Math.max(1, ...finals.map((row) => formatTokens(row.total).length)) + 2);
  const rows = $derived(
    finals.map(({ model }) => {
      const now = atCursor.get(model);
      return { model, called: now != null, total: now?.tokens.total ?? 0, unreportedCalls: now?.unreportedCalls ?? 0 };
    })
  );
  /* Shares of what was reported by the cursor: a model whose calls reported nothing has
     no share to draw, and its figure says so. */
  const reported = $derived(rows.reduce((sum, row) => sum + row.total, 0));
  const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 0 });
  const turns = $derived.by(() => {
    const totals: Record<string, { total: number; unknown: number }> = Object.create(null);
    for (const point of points) {
      if (point.index > viewIndex || !(point.turnNumber > 0)) continue;
      const turn = totals[point.turnNumber] ?? { total: 0, unknown: 0 };
      turn.total += point.call?.total ?? 0;
      if (!point.call) turn.unknown += 1;
      totals[point.turnNumber] = turn;
    }
    return Object.entries(totals).map(([turn, tokens]) => [Number(turn), tokens] as const).slice(-5);
  });

  /* The provider's id, with what hides in it drawn: an empty id says so, a newline
     and a tab get a glyph, and edge spaces stay (white-space: pre), so "gpt\n5" and
     " gpt-5" never pass for ordinary ids. */
  function idOf(model: string): string {
    if (model.length === 0) return "(empty model id)";
    return model.replaceAll("\n", "↵").replaceAll("\t", "→");
  }
</script>

<section class="model-breakdown" aria-labelledby={titleId}>
  <h3 class="kicker" id={titleId}>By model</h3>
  {#if rows.length === 0}
    <p class="empty">No model calls yet.</p>
  {:else}
    <ul style={`--value-ch: ${valueCh}ch`}>
      {#each rows as row (row.model)}
        <!-- One line each, so the bars line up and compare. The id ellipsizes, and the
             tip carries it whole; the name is focusable so the keyboard can reach it. A
             model not called yet by the cursor is dimmed at zero. -->
        <li class={{ pending: !row.called }}>
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <span class="name" dir="auto" tabindex="0" data-tip={idOf(row.model)} data-tip-align="start"><span>{idOf(row.model)}</span></span>
          <span class="bar" aria-hidden="true"><span style={`inline-size: ${finalMax > 0 ? (row.total / finalMax) * 100 : 0}%`}></span></span>
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <span
            class="value"
            data-tip={row.unreportedCalls > 0 ? UNREPORTED_TIP : undefined}
            tabindex={row.unreportedCalls > 0 ? 0 : undefined}
          >{tokenFigure(row.total, row.unreportedCalls)}</span>
          <span class="pct">{!row.called || (row.unreportedCalls > 0 && row.total === 0) || reported === 0 ? "" : percent.format(row.total / reported)}</span>
        </li>
      {/each}
    </ul>
  {/if}
  {#if turns.length > 0}
    <h4 class="kicker">By turn{turns.length === 5 ? " · latest 5" : ""}</h4>
    <dl class="turn-list">
      {#each turns as [turn, tokens] (turn)}
        <div>
          <dt>Turn {turn}</dt>
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <dd data-tip={tokens.unknown > 0 ? UNREPORTED_TIP : undefined} tabindex={tokens.unknown > 0 ? 0 : undefined}>
            {tokenFigure(tokens.total, tokens.unknown)}
          </dd>
        </div>
      {/each}
    </dl>
  {/if}
</section>

<style>
  .model-breakdown {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: var(--gutter-tight);
  }

  h3 {
    margin: 0;
  }

  h4 {
    margin: var(--gutter-tight) 0 0;
    white-space: nowrap;
  }

  /* Columns shared by every row (subgrid), so the bars start and the figures end on
     one line however long each id is. */
  ul {
    margin: 0;
    padding: 0;
    list-style: none;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(48px, 32%) auto auto;
    column-gap: var(--gutter-tight);
    row-gap: 6px;
  }

  li {
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: subgrid;
    align-items: center;
    min-width: 0;
  }

  /* The tip is drawn by the name itself, so the name must not clip: the ellipsis
     lives on the text inside it. */
  .name {
    min-width: 0;
    color: var(--text-1);
    font-size: var(--font-sm);
  }

  .name > span {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: pre;
  }

  .bar {
    display: block;
    height: 6px;
    background: color-mix(in srgb, var(--text-3) 18%, transparent);
  }

  .bar > span {
    display: block;
    height: 100%;
    background: var(--model);
  }

  /* Left to right in any page, as MetricStrip's figures are: see there. */
  .value,
  .pct {
    direction: ltr;
    unicode-bidi: isolate;
  }

  .value {
    min-width: var(--value-ch);
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
    text-align: end;
    white-space: nowrap;
  }

  .pct {
    min-width: 4ch;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
    text-align: end;
  }

  .pending .name,
  .pending .value {
    color: var(--text-3);
  }

  .empty {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .turn-list {
    direction: ltr;
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: var(--gutter-tight);
    margin: 0;
  }

  .turn-list > div {
    min-width: 0;
  }

  .turn-list dt {
    color: var(--text-3);
    font-size: var(--font-xs);
    white-space: nowrap;
  }

  .turn-list dd {
    direction: ltr;
    margin: 0;
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
  }
</style>
