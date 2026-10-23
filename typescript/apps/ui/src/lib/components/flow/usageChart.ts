/* Narrowest subpath per symbol, not the barrel: every mark has its own entry and
   that is the only lever on what the d3 dependency tree drags in. */
import { areaY } from "@tanstack/charts/area";
import { barY } from "@tanstack/charts/bar";
import { dot } from "@tanstack/charts/dot";
import { lineY } from "@tanstack/charts/line";
import { decorative } from "@tanstack/charts/mark/decorative";
import { rect } from "@tanstack/charts/rect";
import { ruleX } from "@tanstack/charts/rule";
import { scaleLinear } from "@tanstack/charts/scales/linear";
import { defineChart } from "@tanstack/charts/scene";
import { tooltip } from "@tanstack/charts/tooltip";
import { portal, type ChartTooltipPortalExtension } from "@tanstack/charts/tooltip/portal";
import type { ChartCurve } from "@tanstack/charts/types";
import { ArrowDownToLine, ArrowUpFromLine, Brain, Clock, Cpu, DatabaseZap, Gauge, Hash, ShieldCheck, Sigma, Timer, Wrench } from "@lucide/svelte";
import type { Component } from "svelte";
import type { TipField } from "$lib/components/primitives/TipCard.svelte";
import { formatTokens, type OptionalUsageField, type UsageTimelinePoint, type UsageTotals } from "$lib/cost/pricing";
import { formatDuration } from "$lib/state/replayLog";
import { niceTimeTicks } from "$lib/state/timeTicks";
import type { StepTimeline } from "$lib/state/stepTimeline";
import {
  buildWorkflowScale,
  distinctTickLabels,
  GAP_BREAK_GLYPH,
  gapBreakX,
  projectTime,
  unprojectTime,
  type CollapsedGap,
  type ScaledSegment
} from "$lib/state/workflowTimelineScale";

/** A bar's segments, bottom to top. They do not overlap, unlike the protocol's fields. */
export type TokenCategory = "uncached" | "cached" | "output" | "thought" | "toolUse" | "decisions";
export const TOKEN_CATEGORIES: readonly TokenCategory[] = ["uncached", "cached", "output", "thought", "toolUse", "decisions"];
export type CategoryTokens = Record<TokenCategory, number>;

/* Mixed into the pane's surface like the old single bar, each 3:1 or more against it
   (contrast.test.mjs). The two lavenders and the two greys are told apart by
   lightness, every other pair by hue, and a surface hairline separates neighbours. */
export const TOKEN_CATEGORY_STYLES: Record<TokenCategory, { label: string; color: string }> = {
  uncached: { label: "input (uncached)", color: "color-mix(in srgb, var(--model) 80%, var(--surface-1))" },
  cached: { label: "cached", color: "color-mix(in srgb, var(--model) 55%, var(--surface-1))" },
  output: { label: "output", color: "var(--text-2)" },
  thought: { label: "thought", color: "color-mix(in srgb, var(--reasoning) 80%, var(--surface-1))" },
  toolUse: { label: "tool-use", color: "color-mix(in srgb, var(--tool) 80%, var(--surface-1))" },
  decisions: { label: "decisions", color: "var(--text-3)" }
};

/** A figure the tooltip or the readout under the chart shows about a call. */
export type UsageMetric = "model" | "at" | "call" | "input" | "running" | "duration" | "rate" | TokenCategory;

/* The Lucide icon each figure leads with, the same in the tooltip and the readout. The
   model, tool and approval ones are the latency waterfall's span kinds; the clock is the
   Session Manager's start time. */
export function metricIcon(metric: UsageMetric): Component<{ size?: number | string }> {
  switch (metric) {
    case "model": return Cpu;
    case "at": return Clock;
    case "call": return Hash;
    case "input":
    case "uncached": return ArrowDownToLine;
    case "cached": return DatabaseZap;
    case "output": return ArrowUpFromLine;
    case "thought": return Brain;
    case "toolUse": return Wrench;
    case "decisions": return ShieldCheck;
    case "running": return Sigma;
    case "duration": return Timer;
    case "rate": return Gauge;
    default: {
      const unhandled: never = metric;
      throw new Error(`No icon for ${String(unhandled)}`);
    }
  }
}

const callNumber = new Intl.NumberFormat("en-US");
/* A list of ids isolated one by one, so a right-to-left first id cannot turn the whole
   line right to left and carry the left-to-right ids after it to the wrong end. */
const isolate = (text: string) => `\u2068${text}\u2069`;

/* One bar's tip, the Session Manager's way: an icon in place of each key, and every value
   worded to say what it is ("call 16", "600 cached"), so no line spends width on a label. */
export function slotTipFields(slot: UsageSlot, categories: readonly TokenCategory[]): TipField[] {
  const field = (metric: UsageMetric, key: string, value: string | null, mono = false): TipField => ({ key, value, mono, glyph: metricIcon(metric) });
  return [
    slot.calls === 1
      ? field("model", "Model", slot.models[0] ?? null, true)
      : field("model", "Models", `${slot.models.map(isolate).join(", ")}${slot.models.length < slot.calls ? "…" : ""}`),
    slot.calls === 1
      ? field("call", "Call", `call ${callNumber.format(slot.call)}`)
      : field("call", "Calls", `${callNumber.format(slot.calls)} calls · ${callNumber.format(slot.first)}–${callNumber.format(slot.call)}`),
    ...categories.map((category) => {
      const { label } = TOKEN_CATEGORY_STYLES[category];
      return field(category, label, slot.parts[category] > 0 ? `${formatTokens(slot.parts[category])} ${label}` : null);
    }),
    field("running", "Running", `${slot.unknownSoFar > 0 ? "≥ " : ""}${formatTokens(slot.running)} so far`),
    field("at", "At", `+${formatDuration(slot.x)}`)
  ];
}

export const emptyCategories = (): CategoryTokens => ({ uncached: 0, cached: 0, output: 0, thought: 0, toolUse: 0, decisions: 0 });

const count = (value: number) => (Number.isFinite(value) && value > 0 ? value : 0);

/**
 * One call's counts as disjoint parts that sum to its total.
 *
 * The protocol's fields overlap, and not the same way for every producer: cached is
 * always inside input, but OpenAI and pydantic-ai count thought inside output while
 * Gemini counts thought and tool-use beside it, in its own total. So thought is taken
 * out of output only when the total leaves no room for it outside. A decision model's
 * call is all decisions.
 */
export function callCategories(call: UsageTotals, decision = false): CategoryTokens {
  const parts = emptyCategories();
  if (decision) {
    parts.decisions = count(call.total);
    return parts;
  }
  const [input, cached, output, thought, toolUse] = [call.input, call.cached, call.output, call.thought, call.toolUse].map(count) as [number, number, number, number, number];
  parts.cached = cached;
  parts.uncached = Math.max(0, input - cached);
  parts.thought = thought;
  parts.toolUse = toolUse;
  parts.output = input + output + thought + toolUse > count(call.total) ? Math.max(0, output - thought) : output;
  return parts;
}

export const categorySum = (parts: CategoryTokens) => TOKEN_CATEGORIES.reduce((sum, category) => sum + parts[category], 0);

/** Every reported call's parts up to a 1-based frame, the replay cursor. */
export function categoryTotals(points: readonly UsageTimelinePoint[], throughIndex = Number.POSITIVE_INFINITY): CategoryTokens {
  const totals = emptyCategories();
  for (const point of points) {
    if (point.index > throughIndex || !point.call) continue;
    const parts = callCategories(point.call, point.decision);
    for (const category of TOKEN_CATEGORIES) totals[category] += parts[category];
  }
  return totals;
}

/** The categories some call in the run spent tokens on, in stacking order; the rest are not drawn or listed. */
export function presentCategories(points: readonly UsageTimelinePoint[]): TokenCategory[] {
  const totals = categoryTotals(points);
  return TOKEN_CATEGORIES.filter((category) => totals[category] > 0);
}

/** The protocol field a category is read from, when that field is optional. */
function categoryField(category: TokenCategory): OptionalUsageField | null {
  switch (category) {
    case "cached":
      return "cached";
    case "thought":
      return "thought";
    case "toolUse":
      return "toolUse";
    case "uncached":
    case "output":
    case "decisions":
      return null;
    default: {
      const unhandled: never = category;
      return unhandled;
    }
  }
}

/**
 * Per category, the agent models whose reported calls left its field null, so the
 * category's figure leaves them out. Decision calls are all decisions, so theirs never count.
 */
export function unreportedCategories(points: readonly UsageTimelinePoint[]): Map<TokenCategory, string[]> {
  const byCategory = new Map<TokenCategory, string[]>();
  for (const point of points) {
    if (!point.call || point.decision) continue;
    for (const category of TOKEN_CATEGORIES) {
      const field = categoryField(category);
      if (field == null || !point.missing?.includes(field)) continue;
      const models = byCategory.get(category) ?? [];
      if (!models.includes(point.model)) models.push(point.model);
      byCategory.set(category, models);
    }
  }
  return byCategory;
}

/** One bar on the chart: a model call, or the calls that share its few pixels. */
export interface UsageSlot {
  /** The latest frame index folded in; the slot is reached once the cursor gets here. */
  index: number;
  /** Seconds from the run's first call to the middle of this slot's stretch of time. */
  x: number;
  calls: number;
  /** 1-based position among the run's calls of the earliest one in this slot. */
  first: number;
  /** 1-based position among the run's calls of the latest one in this slot. */
  call: number;
  /** What the reported calls in this slot spent, by category. */
  parts: CategoryTokens;
  /** Calls in this slot that reported no usage. */
  unknown: number;
  /** What the reported calls in this slot spent. */
  tokens: number;
  /** The reported running total after this slot. */
  running: number;
  /** Calls with no usage up to and including this slot; above zero, `running` is a lower bound. */
  unknownSoFar: number;
  /** Up to three distinct models, for the tooltip. */
  models: string[];
}

/** One drawn line: a model's running total, or the models past the drawn ones summed. */
export interface ModelLine {
  label: string;
  /** Position in MODEL_LINE_STYLES, or null for the shared "other models" line. */
  line: number | null;
  /** The running total after each slot, zero before `from`. */
  running: number[];
  /** The first slot with one of its calls. */
  from: number;
  /** The first slot where one of its calls reported no usage, or -1; past it the line is a lower bound. */
  unknownFrom: number;
}

export interface UsageLayout {
  slots: UsageSlot[];
  /** Largest first, ties by first call, so a line keeps its style for the whole run. */
  lines: ModelLine[];
  /** Seconds from the first call to the last. */
  span: number;
  /** More calls than slots, so a slot is a stretch of time rather than a call. */
  downsampled: boolean;
}

/* The bars take --model's lavender, so the lines take --syntax-literal's magenta in
   four lightness steps that each carry their own dash too: told apart by
   lightness at a glance and by pattern where color fails. Models past these share
   one neutral dotted line. */
export interface LineStyle {
  color: string;
  dash: string | null;
}

export const MODEL_LINE_STYLES: readonly LineStyle[] = [
  { color: "color-mix(in srgb, var(--syntax-literal) 45%, var(--text-1))", dash: null },
  { color: "var(--syntax-literal)", dash: "6 3" },
  { color: "color-mix(in srgb, var(--syntax-literal) 70%, var(--surface-2))", dash: "2 2" },
  { color: "color-mix(in srgb, var(--syntax-literal) 50%, var(--surface-2))", dash: "6 2 1 2" }
];

export const OTHER_LINE_STYLE: LineStyle = { color: "var(--text-3)", dash: "1 2" };

/* The run's total leads: neutral rather than another magenta step, twice a model
   line's weight, with a faint fill under it, so it reads as the frame the model
   lines sit inside instead of one more line among them. */
export const TOTAL_LINE_STYLE: LineStyle = { color: "var(--text-1)", dash: null };
export const TOTAL_WIDTH = 2.5;
export const MODEL_WIDTH = 1.25;

/** A legend entry, one per series the reader can hide on its own. */
export type SeriesKey = "calls" | "total" | "tools" | "waits" | `model:${string}`;
export const modelKey = (line: ModelLine): SeriesKey => `model:${line.label}`;

export const lineStyle = (line: number | null): LineStyle =>
  line == null ? OTHER_LINE_STYLE : MODEL_LINE_STYLES[line % MODEL_LINE_STYLES.length]!;

/** A legend sample of a line: its color, broken by its dash pattern. */
export function lineSwatch({ color, dash }: LineStyle): string {
  if (!dash) return color;
  let at = 0;
  const stops = dash.split(" ").map(Number).map((length, k) => {
    const stop = `${k % 2 === 0 ? color : "transparent"} ${at}px ${at + length}px`;
    at += length;
    return stop;
  });
  return `repeating-linear-gradient(90deg, ${stops.join(", ")})`;
}

/**
 * Tokens on the chart's log axes. One plus the count, so a zero sits on the floor
 * rather than at -Infinity, and anything that is not a positive count is zero.
 */
export const logTokens = (value: number): number => (Number.isFinite(value) && value > 0 ? Math.log10(1 + value) : 0);

/**
 * A segment's [bottom, top] on the call axis, or null when the slot spent none of it.
 * The bar keeps its log height and each part takes a linear share of it: stacked
 * logs would make the bottom part look largest whatever it is.
 */
export function segmentSpan(slot: Pick<UsageSlot, "tokens" | "parts">, category: TokenCategory): [number, number] | null {
  const sum = categorySum(slot.parts);
  if (!(slot.tokens > 0) || sum <= 0 || slot.parts[category] <= 0) return null;
  const height = logTokens(slot.tokens);
  let below = 0;
  for (const other of TOKEN_CATEGORIES) {
    if (other === category) break;
    below += slot.parts[other];
  }
  return [(height * below) / sum, (height * (below + slot.parts[category])) / sum];
}

/* The models in their fixed order; past the styled ones they share one summed line. */
function modelLines(
  byModel: Map<string, { running: (number | undefined)[]; total: number; from: number; unknownFrom: number }>,
  length: number
): ModelLine[] {
  const ordered = [...byModel.entries()]
    .map(([label, entry]) => {
      let last = 0;
      const running = Array.from({ length }, (_, k) => (last = entry.running[k] ?? last));
      return { label, running, from: entry.from, unknownFrom: entry.unknownFrom, total: entry.total };
    })
    .sort((a, b) => b.total - a.total || a.from - b.from);
  const drawn = ordered.slice(0, MODEL_LINE_STYLES.length).map(({ total: _, ...line }, k) => ({ ...line, line: k }));
  const rest = ordered.slice(MODEL_LINE_STYLES.length);
  if (rest.length === 0) return drawn;
  const known = rest.map((line) => line.unknownFrom).filter((at) => at >= 0);
  return [
    ...drawn,
    {
      label: `${rest.length} other ${rest.length === 1 ? "model" : "models"}`,
      line: null,
      running: Array.from({ length }, (_, k) => rest.reduce((sum, line) => sum + line.running[k]!, 0)),
      from: Math.min(...rest.map((line) => line.from)),
      unknownFrom: known.length ? Math.min(...known) : -1
    }
  ];
}

export interface UsageActivity {
  kind: "tool" | "wait";
  x1: number;
  x2: number;
  label: string;
  durationSeconds: number;
  /** The wait's row in the band under the plot; tools all share row 0. */
  lane: number;
  /** 1-based frame the span opened on, so a cursor can leave later ones out. */
  startIndex?: number;
  /** Tool starts drawn as this one tick, when more than one shares its pixel column. */
  count?: number;
  /** The latest of those starts. */
  last?: { label: string; x1: number };
}

/* Concurrent waits each get a row, so two at once read as two rather than as one
   brighter bar. Past the last row they share it, and the tooltip still names each. */
export const WAIT_LANES = 3;

/** Tool starts and human waits, projected once by the latency timeline. */
export function usageActivity(timeline: StepTimeline, origin: number, span: number): UsageActivity[] {
  const items = timeline.turns.flatMap((turn) =>
    [...turn.spans, ...turn.subagentTurns.flatMap((subagent) => subagent.spans)]
      .filter((item) => item.kind === "tool" || item.kind === "approval")
      .map((item) => ({
        kind: item.kind === "tool" ? "tool" as const : "wait" as const,
        x1: Math.max(0, Math.min(span, item.startTs - origin)),
        x2: Math.max(0, Math.min(span, item.endTs - origin)),
        label: item.label,
        durationSeconds: item.durationSeconds,
        lane: 0,
        ...(item.startIndex == null ? {} : { startIndex: item.startIndex })
      }))
  );
  const laneEnds: number[] = [];
  for (const wait of items.filter((item) => item.kind === "wait").sort((a, b) => a.x1 - b.x1)) {
    const free = laneEnds.findIndex((end) => end <= wait.x1);
    wait.lane = free >= 0 ? free : Math.min(laneEnds.length, WAIT_LANES - 1);
    laneEnds[wait.lane] = Math.max(laneEnds[wait.lane] ?? 0, wait.x2);
  }
  return items;
}

/* A tool tick's width in px: past one start per column the ticks only draw over each other. */
const TOOL_TICK_PX = 2;

/** Tool ticks, one per pixel column: those sharing a column become the earliest, counting the
    rest and naming the latest. A long run has tens of thousands of tools and a plot has
    hundreds of columns. */
export function mergeToolTicks<T extends UsageActivity & { axisX1: number }>(ticks: readonly T[]): T[] {
  const merged: T[] = [];
  let column = Number.NaN;
  for (const tick of ticks.toSorted((a, b) => a.axisX1 - b.axisX1)) {
    const at = Math.floor(tick.axisX1 / TOOL_TICK_PX);
    const head = merged.at(-1);
    if (head && at === column) {
      head.count = (head.count ?? 1) + 1;
      head.last = { label: tick.label, x1: tick.x1 };
    } else {
      merged.push({ ...tick });
      column = at;
    }
  }
  return merged;
}

/** Seconds in which at least one wait was open: overlapping waits count once. */
export function waitedSeconds(activity: readonly UsageActivity[]): number {
  let total = 0;
  let end = -Infinity;
  for (const { x1, x2 } of activity.filter((item) => item.kind === "wait").sort((a, b) => a.x1 - b.x1)) {
    total += Math.max(0, x2 - Math.max(x1, end));
    end = Math.max(end, x2);
  }
  return total;
}

export const IDLE_GAP_WIDTH = 24;

/**
 * The workflow timeline's own idle gaps, in seconds from the first call, so both
 * views fold the same stretches. The scale clips any that fall outside the calls.
 */
export function usageGaps(timeline: StepTimeline, origin: number): CollapsedGap[] {
  if (!Number.isFinite(origin)) return [];
  return timeline.workflow.gaps.map((gap) => ({ ...gap, startTs: gap.startTs - origin, endTs: gap.endTs - origin }));
}

/**
 * The playhead's x: on the cursor's call, unless the replay is inside a folded gap,
 * where it sits on the break rather than at a guessed spot in time that is not drawn.
 */
export function playheadAxisX(segments: readonly ScaledSegment[], callX: number, cursorSeconds?: number): number {
  const gap = cursorSeconds == null
    ? undefined
    : segments.find((segment) => segment.collapsed && cursorSeconds > segment.startTs && cursorSeconds < segment.endTs);
  return gap ? gapBreakX(gap) : projectTime(segments, callX);
}

/* A bar narrower than this stops reading as a bar, and past a few hundred the
   renderer is drawing more marks than the plot has room to show. */
const SLOT_PX = 4;
export const MAX_SLOTS = 400;
const PLOT_GUESS = 480;
/* Budget in steps, so a pane drag does not re-bucket the series every pixel. */
const BUDGET_STEP = 25;

/** How many slots a plot this wide can show. */
export function slotBudget(width: number): number {
  const raw = Math.floor((width > 0 ? width : PLOT_GUESS) / SLOT_PX);
  return Math.max(BUDGET_STEP, Math.min(MAX_SLOTS, Math.floor(raw / BUDGET_STEP) * BUDGET_STEP));
}

/**
 * The calls as at most `budget` slots of equal time.
 *
 * Calls that land in one slot are summed, which also merges calls sharing an
 * instant: two bars at one x would draw as one spike. Each slot sits at the middle
 * of its stretch, so slots are never closer than one stretch apart: the renderer
 * sizes every bar from the tightest gap, and two calls a moment apart drew all of
 * them as hairlines. The running total is
 * re-summed in time order here rather than read off the fold, because calls from
 * several agents can arrive out of order and a running total must not fall.
 *
 * `throughIndex` leaves out every call after that 1-based frame, which is the
 * replay cursor. Buckets and the span still come from the whole run, so a prefix
 * keeps the positions it has when the cursor is at the end and scrubbing does
 * not rescale the axis. One cut feeds every series.
 */
export function usageSlots(
  points: readonly UsageTimelinePoint[],
  budget: number,
  throughIndex = Number.POSITIVE_INFINITY
): UsageLayout {
  if (points.length === 0) return { slots: [], lines: [], span: 0, downsampled: false };
  let ordered = true;
  for (let i = 1; i < points.length && ordered; i += 1) {
    ordered = points[i - 1]!.timestamp <= points[i]!.timestamp;
  }
  const sorted = ordered
    ? points
    : points.slice().sort((a, b) => a.timestamp - b.timestamp || a.index - b.index);
  const ordinal = ordered ? null : new Map(points.map((point, k) => [point, k + 1]));
  const origin = sorted[0]!.timestamp;
  const span = Math.max(0, sorted[sorted.length - 1]!.timestamp - origin);
  const width = span / Math.max(1, budget);

  const slots: UsageSlot[] = [];
  let bucket = -1;
  let running = 0;
  let unknownSoFar = 0;
  const byModel = new Map<string, { running: (number | undefined)[]; total: number; from: number; unknownFrom: number }>();
  for (const [k, point] of sorted.entries()) {
    if (point.index > throughIndex) continue;
    const x = Math.max(0, point.timestamp - origin);
    const at = width > 0 ? Math.min(budget - 1, Math.floor(x / width)) : 0;
    if (at !== bucket) {
      bucket = at;
      slots.push({ index: point.index, x: (at + 0.5) * width, calls: 0, first: Number.POSITIVE_INFINITY, call: 0, parts: emptyCategories(), unknown: 0, tokens: 0, running, unknownSoFar, models: [] });
    }
    const slot = slots[slots.length - 1]!;
    slot.calls += 1;
    slot.first = Math.min(slot.first, ordinal?.get(point) ?? k + 1);
    slot.call = Math.max(slot.call, ordinal?.get(point) ?? k + 1);
    slot.index = Math.max(slot.index, point.index);
    if (point.call) {
      slot.tokens += point.call.total;
      running += point.call.total;
      const parts = callCategories(point.call, point.decision);
      for (const category of TOKEN_CATEGORIES) slot.parts[category] += parts[category];
    } else {
      slot.unknown += 1;
      unknownSoFar += 1;
    }
    slot.running = running;
    slot.unknownSoFar = unknownSoFar;
    if (slot.models.length < 3 && !slot.models.includes(point.model)) slot.models.push(point.model);
    const position = slots.length - 1;
    let model = byModel.get(point.model);
    if (!model) byModel.set(point.model, (model = { running: [], total: 0, from: position, unknownFrom: -1 }));
    if (point.call) model.total += point.call.total;
    else if (model.unknownFrom === -1) model.unknownFrom = position;
    model.running[position] = model.total;
  }
  return { slots, lines: modelLines(byModel, slots.length), span, downsampled: points.length > budget };
}

/** The plotted slot center nearest a call's elapsed time. */
export function slotXAt(slots: readonly UsageSlot[], x: number): number {
  let lo = 0;
  let hi = slots.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (slots[mid]!.x < x) lo = mid + 1;
    else hi = mid;
  }
  const after = slots[lo];
  const before = slots[lo - 1];
  if (!after) return before?.x ?? 0;
  if (!before) return after.x;
  return x - before.x <= after.x - x ? before.x : after.x;
}

/* The top of a log axis with headroom, floored so an all-zero series still has a height. */
const logTop = (peak: number) => Math.max(1, logTokens(peak)) * 1.08;

/* A gridline at each power of ten under the top: on a log axis equal steps are equal
   ratios, so the lines say "ten times" without a label. */
export const decadeTicks = (top: number) => Array.from({ length: Math.floor(top) }, (_, k) => logTokens(10 ** (k + 1)));

/* Axis ticks land on whole steps, so trailing zero fields are dead weight: "1h 00m 00s"
   is wide enough on a narrow axis that the thinner drops a neighbour to fit it. */
export const durationTick = (seconds: number) => formatDuration(seconds).replace(/( 00m)? 00s$/, "");

/* Flat between calls and rising at each one: the total only changes when a call
   ends, so a slope between two calls would claim tokens spent in between. */
const stepLine = (points: readonly (readonly [number, number])[]) =>
  points.map(([x, y], k) => (k === 0 ? `M${x},${y}` : `H${x}V${y}`)).join("");
const stepAfter: ChartCurve = {
  line: stepLine,
  area: (top, bottom) =>
    top.length === 0 ? "" : `${stepLine(top)}V${bottom[bottom.length - 1]![1]}H${bottom[0]![0]}Z`
};

/* The renderer's margins around the plot (x-axis labels below, a hairline above); a
   guess is enough, since it only sizes the unknown-call stub and the activity band. */
const AXIS_PX = 28;
const BAR_MAX = 10;
/* A call with no usage is a stub this tall at the floor: visible, and claiming no
   size, where a full bar read as the largest call on screen. */
const UNKNOWN_STUB_PX = 4;
/* Tools and waits sit in rows of this band under the baseline, in px, so a wait is
   never mistaken for the floor and crosses no bar or line. */
const BAND_PAD = 3;
const LANE_PX = 4;
const LANE_GAP = 2;
/* Below this chart width the workflow activity goes, so the token series keep the plot. */
export const LEFT_AXIS_MIN = 360;

/** Only past one model, since a lone model's line is already the total. */
export const drawsTotal = (layout: UsageLayout) => layout.lines.length > 1;

export interface UsageChartInput {
  layout: UsageLayout;
  /**
   * The whole run, when `layout` is only the calls the cursor has reached.
   * The axis, the grid and the collapsed gaps come from here, so they stay
   * put while the cursor moves. Defaults to `layout`.
   */
  domain?: UsageLayout;
  cursor: { x: number } | undefined;
  /** 1-based replay cursor. A tool or wait that opens after it is not drawn. */
  viewIndex?: number;
  /** Seconds from the first call. A wait still open is drawn only up to here. */
  cursorSeconds?: number;
  activity: UsageActivity[];
  /** Idle stretches to fold, in seconds from the first call: see `usageGaps`. */
  gaps?: readonly CollapsedGap[];
  /** The height handed to the renderer, which the stub and the band are sized against. */
  plotHeight: number;
  reducedMotion: boolean;
  /** Series switched off in the legend. A hidden call keeps its bar as a clear focus target. */
  hidden?: ReadonlySet<SeriesKey>;
}

/**
 * Each call as a light bar on the left log axis, behind; the run's total and each
 * model's running total as lines on the right log axis, in front; unknown calls as
 * stubs; tools and waits in rows under the baseline; and the playhead. Neither y axis
 * is labeled: the tooltip and the figures carry the numbers, and the right axis keeps
 * an unlabeled line per power of ten.
 */
export function usageChart({
  layout,
  domain = layout,
  cursor,
  viewIndex,
  cursorSeconds,
  activity,
  gaps: idleGaps = [],
  plotHeight,
  reducedMotion,
  hidden = new Set()
}: UsageChartInput) {
  const { slots, lines, downsampled } = layout;
  const span = domain.span;
  /* Stepped while each slot is one call, because a total only moves when a call
     ends; once slots are stretches of time the steps are a few pixels each and read
     as noise, and a line between them reads as the rate it is. The points are only
     the calls the cursor has reached, so the step stops there instead of holding
     the final total across the rest of the run. */
  const curve = downsampled ? undefined : stepAfter;
  /* The whole run, shown or not, so hiding a series or moving the cursor never rescales. */
  const callTop = logTop(domain.slots.reduce((max, slot) => Math.max(max, slot.tokens), 0));
  const runTop = logTop(domain.slots.at(-1)?.running ?? 0);
  const cursorSlot = cursor ? slots.findIndex((slot) => slot.x === cursor.x) : -1;
  const showCalls = !hidden.has("calls");
  const total: Pick<ModelLine, "running" | "from" | "unknownFrom"> | null =
    drawsTotal(layout) && !hidden.has("total")
      ? { running: slots.map((slot) => slot.running), from: 0, unknownFrom: slots.findIndex((slot) => slot.unknownSoFar > 0) }
      : null;
  const shownLines = lines.map((line, k) => ({ line, k })).filter(({ line }) => !hidden.has(modelKey(line)));
  type ProjectedSlot = UsageSlot & { axisX: number; logTokens: number };

  /* Thin and decorative: the bar under each x is what focus lands on, and its
     tooltip lists every line's value. Past a line's first call with no usage its
     total is a lower bound, so the line goes on faded rather than dashed, since the
     dash already says which model it is. */
  const lineMarks = (
    projected: readonly ProjectedSlot[],
    line: Pick<ModelLine, "running" | "from" | "unknownFrom">,
    id: string,
    style: LineStyle,
    width: number
  ) => {
    const rows = projected.map((slot, position) => ({ axisX: slot.axisX, y: logTokens(line.running[position]!) }));
    const draw = (data: typeof rows, markId: string, opacity: number) =>
      decorative(
        lineY(data, {
          id: markId,
          x: "axisX",
          y: "y",
          yScale: "running",
          z: () => markId,
          stroke: style.color,
          strokeDasharray: style.dash ?? undefined,
          strokeWidth: width,
          strokeOpacity: opacity,
          curve,
          points: false
        })
      );
    const bound = line.unknownFrom;
    return bound === -1
      ? [draw(rows.slice(line.from), id, 1)]
      : [
          ...(bound > line.from ? [draw(rows.slice(line.from, bound + 1), id, 1)] : []),
          draw(rows.slice(bound), `${id}-bound`, 0.45)
        ];
  };

  /* Every slot goes to each bar mark, because the renderer infers bar width from
     the x values of its own mark; a slot a mark does not draw gets no extent. The
     lines go last, over the bars. */
  const marks = (
    visibleActivity: UsageActivity[],
    segments: readonly ScaledSegment[],
    band: { toolRows: number; perPx: number }
  ) => {
    const projectedSlots: ProjectedSlot[] = slots.map((slot) => ({
      ...slot,
      axisX: projectTime(segments, slot.x),
      logTokens: logTokens(slot.tokens)
    }));
    const stub = UNKNOWN_STUB_PX * band.perPx;
    const activityRows = visibleActivity.map((item) => {
      const row = item.kind === "tool" ? 0 : band.toolRows + item.lane;
      const top = BAND_PAD + row * (LANE_PX + LANE_GAP);
      const axisX1 = projectTime(segments, item.x1);
      return {
        ...item,
        axisX1,
        /* A tool is a tick at its start; a wait spans its time, never under 2px. */
        axisX2: item.kind === "tool" ? axisX1 + TOOL_TICK_PX : Math.max(axisX1 + 2, projectTime(segments, item.x2)),
        y1: -top * band.perPx,
        y2: -(top + LANE_PX) * band.perPx
      };
    });
    const activityMarks = (kind: UsageActivity["kind"]) =>
      rect(kind === "tool" ? mergeToolTicks(activityRows.filter((item) => item.kind === "tool")) : activityRows.filter((item) => item.kind === kind), {
        id: kind,
        x: "axisX1",
        x1: "axisX1",
        x2: "axisX2",
        y1: "y1",
        y2: "y2",
        key: (item) => `${item.label}:${item.axisX1}`,
        /* rect insets 0.75px per edge by default, which halves a 4px row. */
        inset: 0,
        fill: kind === "tool" ? "var(--usage-tool)" : "var(--usage-wait-fill)",
        /* Outlined, so a wait's two ends read even where it spans the whole run. */
        stroke: kind === "wait" ? "var(--usage-wait)" : undefined,
        strokeWidth: kind === "wait" ? 1 : undefined
      });
    const totalRows = total
      ? projectedSlots.map((slot, position) => ({ axisX: slot.axisX, y: logTokens(total.running[position]!) }))
      : [];
    return [
    activityMarks("wait"),
    activityMarks("tool"),
    ...(total
      ? [
          decorative(
            areaY(totalRows, {
              id: "total-fill",
              x: "axisX",
              y1: 0,
              y2: "y",
              yScale: "running",
              z: () => "total-fill",
              fill: "url(#total-fill)",
              curve
            })
          )
        ]
      : []),
    /* The timeline's "≀", standing on the baseline at the middle of the fold. */
    ...segments.filter((segment) => segment.collapsed).map((gap) =>
      decorative(
        lineY(
          GAP_BREAK_GLYPH.map(([x, y]) => ({
            axisX: gapBreakX(gap) - 5 + x,
            y: (14 - y) * band.perPx
          })),
          {
            id: `idle-break-${gap.key}`,
            x: "axisX",
            y: "y",
            stroke: "var(--usage-marker)",
            strokeWidth: 1,
            points: false
          }
        )
      )
    ),
    barY(projectedSlots, {
      id: "calls",
      x: "axisX",
      y1: (slot) => (slot.tokens > 0 ? 0 : null),
      y2: (slot) => (slot.tokens > 0 ? slot.logTokens : null),
      key: (slot) => `calls-${slot.index}`,
      /* Clear under its segments, and while hidden: it is what hover and the arrow keys
         land on. Solid only for a total with no parts to split it into. */
      fill: (slot) => (showCalls && categorySum(slot.parts) <= 0 ? "var(--usage-bar)" : "transparent"),
      maxThickness: BAR_MAX
    }),
    ...(showCalls
      ? TOKEN_CATEGORIES.map((category) =>
          decorative(
            barY(projectedSlots, {
              id: `part-${category}`,
              x: "axisX",
              y1: (slot) => segmentSpan(slot, category)?.[0] ?? null,
              y2: (slot) => segmentSpan(slot, category)?.[1] ?? null,
              key: (slot) => `${category}-${slot.index}`,
              fill: TOKEN_CATEGORY_STYLES[category].color,
              /* A hairline of the surface between parts, so two of a like lightness still read as two. */
              stroke: "var(--surface-1)",
              strokeWidth: 0.75,
              maxThickness: BAR_MAX
            })
          )
        )
      : []),
    /* A call with no usage spent something unknown: a muted stub marks it without
       claiming a size. Focus is by column, so the stub needs no tall target. */
    barY(projectedSlots, {
      id: "unknown",
      x: "axisX",
      y1: (slot) => (slot.unknown > 0 && slot.tokens === 0 ? 0 : null),
      y2: (slot) => (slot.unknown > 0 && slot.tokens === 0 ? stub : null),
      key: (slot) => `unknown-${slot.index}`,
      fill: showCalls ? "var(--usage-unknown)" : "transparent",
      maxThickness: BAR_MAX
    }),
    /* Under the models: a model that is most of the run rides just below the total on
       a log axis, and drawn on top the heavier line would cover it. */
    ...(total ? lineMarks(projectedSlots, total, "total", TOTAL_LINE_STYLE, TOTAL_WIDTH) : []),
    ...shownLines.flatMap(({ line, k }) => lineMarks(projectedSlots, line, `model-${k}`, lineStyle(line.line), MODEL_WIDTH)),
    decorative(
      ruleX([playheadAxisX(segments, cursor?.x ?? 0, cursorSeconds)], {
        stroke: "var(--usage-marker)",
        strokeWidth: 1,
        strokeDasharray: "3 4"
      })
    ),
    /* A dot where the playhead crosses each shown line its model has reached. */
    ...[
      ...shownLines.map(({ line }) => ({ line, color: lineStyle(line.line).color })),
      ...(total ? [{ line: total, color: TOTAL_LINE_STYLE.color }] : [])
    ]
      .filter(({ line }) => cursorSlot >= 0 && line.from <= cursorSlot)
      .map(({ line, color }) =>
        decorative(
          dot([{ axisX: projectedSlots[cursorSlot]!.axisX, y: logTokens(line.running[cursorSlot]!) }], {
            x: "axisX",
            y: "y",
            yScale: "running",
            r: 3,
            fill: color,
            stroke: "var(--surface-2)",
            strokeWidth: 1.5
          })
        )
      )
    ];
  };

  return defineChart({
    chart: ({ width }) => {
      const segments = buildWorkflowScale({ startTs: 0, endTs: span }, idleGaps, width, IDLE_GAP_WIDTH);
      const folded = segments.filter((segment) => segment.collapsed);
      const tickTimes = distinctTickLabels(
        (span > 0 ? niceTimeTicks(span, Math.max(3, Math.min(8, Math.round(width / 70)))) : [0])
          .filter((tick) => !folded.some((gap) => tick > gap.startTs && tick < gap.endTs)),
        durationTick
      );
      const shownActivity = width < LEFT_AXIS_MIN
        ? []
        : activity.flatMap((item) => {
            if (hidden.has(item.kind === "tool" ? "tools" : "waits")) return [];
            if (viewIndex != null && item.startIndex != null && item.startIndex > viewIndex) return [];
            if (cursorSeconds == null || item.x2 <= cursorSeconds) return [item];
            if (item.x1 >= cursorSeconds) return [];
            return [{ ...item, x2: cursorSeconds, durationSeconds: Math.max(0, cursorSeconds - item.x1) }];
          });
      /* The band is room below zero on both axes, the same px on each, so bars and
         lines keep one baseline and every row sits under it. */
      const toolRows = shownActivity.some((item) => item.kind === "tool") ? 1 : 0;
      const waitRows = shownActivity.reduce((rows, item) => (item.kind === "wait" ? Math.max(rows, item.lane + 1) : rows), 0);
      const rows = toolRows + waitRows;
      const bandPx = rows ? BAND_PAD + rows * LANE_PX + (rows - 1) * LANE_GAP : 0;
      const inner = Math.max(bandPx + 1, plotHeight - AXIS_PX) - bandPx;
      const floor = (top: number) => -(bandPx * top) / inner;
      return {
      marks: marks(shownActivity, segments, { toolRows, perPx: callTop / inner }),
      gradients: [
        {
          id: "total-fill",
          x1: 0,
          y1: 0,
          x2: 0,
          y2: 1,
          stops: [
            { offset: 0, color: TOTAL_LINE_STYLE.color, opacity: 0.1 },
            { offset: 1, color: TOTAL_LINE_STYLE.color, opacity: 0 }
          ]
        }
      ],
      scales: {
        /* Configured instances, not factories, and not niced: nicing rounded a 430k
           peak into a 600k axis and a 12,005s run into a 20,000s one. The ticks are
           chosen inside the domains instead. */
        x: {
          scale: scaleLinear().domain([0, width > 0 ? width : 1]),
          nice: false,
          axis: {
            ticks: {
              values: tickTimes.map((tick) => projectTime(segments, tick)),
              format: (value) => durationTick(unprojectTime(segments, value))
            },
            tickLabels: { thin: { minGap: 12, priority: "ends" } }
          }
        },
        /* Both y scales hold logTokens values on a linear scale, which is a log axis
           with zero on the floor. Per call, on the left, with no axis or grid. */
        y: {
          scale: scaleLinear().domain([floor(callTop), callTop]),
          nice: false,
          grid: false,
          axis: false
        },
        /* The totals, on the right: no labels, only a gridline per decade. */
        running: {
          channel: "y",
          side: "right",
          scale: scaleLinear().domain([floor(runTop), runTop]),
          nice: false,
          grid: true,
          axis: { line: false, ticks: { values: decadeTicks(runTop), line: false, size: 0, padding: 0 }, tickLabels: false }
        }
      }
    };
    },
    focus: "group-x",
    keyboard: true,
    /* `use`, not a spread: a spread copies the extension's `create`, and the host then
       takes it for the bare extension and drops every option. */
    tooltip: { use: tooltip, portal: tipAbove, placement: "top", offset: TIP_OFFSET },
    svgAnimation: reducedMotion ? false : { respectReducedMotion: true }
  });
}

/* tipFocus's gap, and the margin the chart's own placement keeps from the window's edge. */
const TIP_OFFSET = 12;
const TIP_EDGE = 8;

/** Whether the tip opens under the plot: only when the window has no room above the point
 *  and more room under the plot's bottom, which is past the axis and its gap breaks. */
export function usageTipBelow(
  pointTop: number,
  plotBottom: number,
  tipHeight: number,
  view: { top: number; bottom: number }
): boolean {
  const above = pointTop - view.top;
  return above < tipHeight + TIP_OFFSET + TIP_EDGE && view.bottom - plotBottom > above;
}

/* Above the hovered point, as every tip in the app opens, and in the top layer so the window
   is the boundary: inside the plot's own box a full card rarely fits above, and is clamped
   down over the x axis, its gap breaks and its ticks. */
const tipAbove: ChartTooltipPortalExtension = {
  id: "usage-tip-above",
  create(context, options) {
    const inner = portal.create(context, options);
    return {
      ...inner,
      position(next) {
        const box = next.surface.element.getBoundingClientRect();
        const scale = next.scene.height > 0 ? box.height / next.scene.height : 1;
        const win = context.element.ownerDocument.defaultView;
        const top = win?.visualViewport?.offsetTop ?? 0;
        const bottom = top + (win?.visualViewport?.height ?? win?.innerHeight ?? 0);
        const below = usageTipBelow(box.top + next.anchor.y * scale, box.bottom, context.element.offsetHeight, { top, bottom });
        return inner.position(
          below ? { ...next, anchor: { x: next.anchor.x, y: next.scene.height }, placement: "bottom" } : next
        );
      }
    };
  }
};

/** The slot under a hovered or focused group, whichever of its marks the pointer is on. */
export function hoveredSlot(points: readonly { datum: unknown }[]): UsageSlot | undefined {
  for (const { datum } of points) {
    if (typeof datum === "object" && datum !== null && "calls" in datum) return datum as UsageSlot;
  }
  return undefined;
}

export function hoveredActivity(points: readonly { datum: unknown }[]): UsageActivity | undefined {
  for (const { datum } of points) {
    if (typeof datum === "object" && datum !== null && "durationSeconds" in datum && "kind" in datum) {
      return datum as UsageActivity;
    }
  }
  return undefined;
}
