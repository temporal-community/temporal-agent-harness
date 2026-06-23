<script lang="ts">
  import { formatTokens, type UsageTimelinePoint } from "$lib/cost/pricing";

  interface Props {
    points: UsageTimelinePoint[];
    viewIndex: number;
  }

  let { points, viewIndex }: Props = $props();

  let hover = $state<{ fraction: number; point: UsageTimelinePoint } | null>(null);

  const width = 640;
  const height = 52;
  const inset = {
    top: 8,
    right: 12,
    bottom: 8,
    left: 12
  };

  const chartPoints = $derived(tokenWindow(points));
  const pointTimestamps = $derived(chartPoints.map((point) => point.timestamp));
  const minTimestamp = $derived(
    pointTimestamps.length ? Math.min(...pointTimestamps) : 0
  );
  const maxTimestamp = $derived(
    pointTimestamps.length ? Math.max(...pointTimestamps) : minTimestamp + 1
  );
  const durationSeconds = $derived(Math.max(maxTimestamp - minTimestamp, 1));
  const currentPoint = $derived(
    latestPointAtOrBefore(chartPoints, viewIndex) ?? chartPoints[0]
  );
  const markerX = $derived(xForTimestamp(currentPoint?.timestamp ?? minTimestamp));
  const shapePoints = $derived(pointsForShape(chartPoints));
  const tokenPath = $derived(stepPath(shapePoints));
  const areaPath = $derived(areaFor(tokenPath));
  const currentShapePoint = $derived(shapePointFor(currentPoint));
  const currentTokens = $derived(
    `${formatTokens(currentPoint?.tokens.total ?? 0)} · +${formatDuration(
      (currentPoint?.timestamp ?? minTimestamp) - minTimestamp
    )}`
  );

  function xForTimestamp(timestamp: number): number {
    const usableWidth = width - inset.left - inset.right;
    return inset.left + ((timestamp - minTimestamp) / durationSeconds) * usableWidth;
  }

  function hasTokens(point: UsageTimelinePoint): boolean {
    return point.tokens.total > 0;
  }

  function tokenWindow(source: UsageTimelinePoint[]): UsageTimelinePoint[] {
    const firstTokenIndex = source.findIndex(hasTokens);
    if (firstTokenIndex === -1) return source;
    return source.slice(Math.max(0, firstTokenIndex - 1));
  }

  function latestPointAtOrBefore(
    source: UsageTimelinePoint[],
    index: number
  ): UsageTimelinePoint | undefined {
    for (let i = source.length - 1; i >= 0; i -= 1) {
      const point = source[i];
      if (point.index <= index) return point;
    }
    return undefined;
  }

  interface ShapePoint {
    index: number;
    x: number;
    y: number;
  }

  function baselineY(): number {
    return height - inset.bottom;
  }

  function pointsForShape(source: UsageTimelinePoint[]): ShapePoint[] {
    if (source.length === 0) return [];

    const maxValue = Math.max(...source.map((point) => point.tokens.total), 1);
    const usableHeight = height - inset.top - inset.bottom;

    return source.map((point) => {
      const normalized = point.tokens.total / maxValue;
      return {
        index: point.index,
        x: xForTimestamp(point.timestamp),
        y: baselineY() - normalized * usableHeight
      };
    });
  }

  function stepPath(points: ShapePoint[]): string {
    if (points.length === 0) return "";
    if (points.length === 1) {
      return `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
    }

    let path = `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
    for (let index = 0; index < points.length - 1; index += 1) {
      const point = points[index];
      const next = points[index + 1];
      path += ` L ${next.x.toFixed(2)} ${point.y.toFixed(2)} L ${next.x.toFixed(2)} ${next.y.toFixed(2)}`;
    }
    return path;
  }

  function areaFor(topPath: string): string {
    if (!topPath || shapePoints.length === 0) return "";
    const first = shapePoints[0];
    const last = shapePoints.at(-1)!;
    return `${topPath} L ${last.x.toFixed(2)} ${baselineY().toFixed(2)} L ${first.x.toFixed(2)} ${baselineY().toFixed(2)} Z`;
  }

  function shapePointFor(point: UsageTimelinePoint | undefined): ShapePoint | undefined {
    if (!point) return undefined;
    for (let index = shapePoints.length - 1; index >= 0; index -= 1) {
      const candidate = shapePoints[index];
      if (candidate.index <= point.index) return candidate;
    }
    return shapePoints[0];
  }

  function formatDuration(seconds: number): string {
    const bounded = Math.max(0, Math.round(seconds));
    if (bounded < 60) return `${bounded}s`;
    return `${Math.floor(bounded / 60)}m ${String(bounded % 60).padStart(2, "0")}s`;
  }

  function handleHover(event: MouseEvent): void {
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    if (rect.width === 0 || chartPoints.length === 0) return;
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const targetTs = minTimestamp + fraction * durationSeconds;
    const point = pointAtTimestamp(targetTs);
    hover = { fraction, point };
  }

  const hoverX = $derived(hover ? xForTimestamp(hover.point.timestamp) : 0);

  function pointAtTimestamp(timestamp: number): UsageTimelinePoint {
    let current = chartPoints[0];
    for (const candidate of chartPoints) {
      if (
        candidate.timestamp <= timestamp &&
        (candidate.timestamp > current.timestamp || candidate.index > current.index)
      ) {
        current = candidate;
      }
    }
    return current;
  }
</script>

<section class="usage-chart" aria-label="Replay token usage timeline">
  <div class="chart-head">
    <span>Token total</span>
    <span class="current-value">{currentTokens}</span>
  </div>

  <div
    class="plot"
    role="presentation"
    onmousemove={handleHover}
    onmouseleave={() => (hover = null)}
  >
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img">
      <line class="grid" x1={inset.left} x2={width - inset.right} y1={inset.top} y2={inset.top} />
      <line
        class="grid baseline"
        x1={inset.left}
        x2={width - inset.right}
        y1={height - inset.bottom}
        y2={height - inset.bottom}
      />
      <path class="area tokens" d={areaPath} />
      <path class="series tokens" d={tokenPath} />
      <line
        class="marker"
        x1={markerX}
        x2={markerX}
        y1={inset.top}
        y2={height - inset.bottom}
      />
      {#if hover}
        <line class="hover-line" x1={hoverX} x2={hoverX} y1={inset.top} y2={height - inset.bottom} />
      {/if}
      {#if currentShapePoint}
        <circle
          class="current-dot"
          cx={markerX}
          cy={currentShapePoint.y}
          r="5.5"
        />
      {/if}
    </svg>

    {#if hover}
      <div
        class="tooltip"
        style={`left: ${hover.fraction * 100}%`}
        class:flip={hover.fraction > 0.6}
      >
        <strong>{formatTokens(hover.point.tokens.total)} tok</strong>
        <span>{hover.point.event} · +{formatDuration(hover.point.timestamp - minTimestamp)}</span>
      </div>
    {/if}
  </div>
</section>

<style>
  .usage-chart {
    --token-spike: #f59e0b;
    min-width: 0;
    display: grid;
    grid-template-rows: auto 38px;
    gap: 4px;
    align-self: start;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
  }

  .chart-head {
    min-width: 0;
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: center;
    color: var(--text-2);
    font-size: 11px;
  }

  .current-value {
    color: var(--text-3);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .tokens { color: var(--token-spike); }

  .plot {
    position: relative;
    height: 38px;
    min-height: 0;
  }

  svg {
    width: 100%;
    height: 38px;
    min-height: 0;
    display: block;
    overflow: visible;
  }

  .hover-line {
    stroke: var(--token-spike);
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
  }

  .area {
    fill: color-mix(in srgb, var(--token-spike) 22%, transparent);
    stroke: none;
  }

  .tooltip {
    position: absolute;
    bottom: calc(100% + 4px);
    transform: translateX(-50%);
    display: grid;
    gap: 1px;
    padding: 5px 8px;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    background: var(--surface-3);
    color: var(--text-1);
    font-size: 11px;
    white-space: nowrap;
    pointer-events: none;
    z-index: 4;
  }

  .tooltip.flip {
    transform: translateX(-100%);
  }

  .tooltip strong {
    font-variant-numeric: tabular-nums;
  }

  .tooltip span {
    color: var(--text-3);
    font-size: 10px;
  }

  .grid {
    stroke: var(--border);
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
  }

  .baseline {
    stroke: var(--border-strong);
  }

  .series {
    fill: none;
    stroke: currentColor;
    stroke-width: 2.4;
    stroke-linecap: round;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
  }

  .series.tokens { color: var(--token-spike); }

  .marker {
    stroke: color-mix(in srgb, var(--text-1) 55%, transparent);
    stroke-dasharray: 3 4;
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
  }

  .current-dot {
    fill: var(--surface-2);
    stroke: var(--token-spike);
    stroke-width: 2.4;
    vector-effect: non-scaling-stroke;
  }
</style>
