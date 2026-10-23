import type { AgentSseFrame, TokenUsage } from "$lib/api/types";

export interface ModelPricing {
  inputPerMillion: number;
  outputPerMillion: number;
  thoughtPerMillion?: number;
  cachedPerMillion?: number;
  toolUsePerMillion?: number;
}

export interface UsageTotals {
  input: number;
  output: number;
  thought: number;
  cached: number;
  toolUse: number;
  total: number;
}

export interface CostSummary {
  tokens: UsageTotals;
  estimatedCostUsd: number | null;
  /** Calls that explicitly reported a prompt-cache count, including zero. */
  cacheReadReportedCalls: number;
  /** Calls that reported no usage object at all. */
  cacheReadUnreportedCalls: number;
  modelBreakdown: Array<{
    model: string;
    tokens: UsageTotals;
    estimatedCostUsd: number | null;
    /** Calls by this model that ended without usage, so its `tokens` are a lower bound. */
    unreportedCalls: number;
  }>;
  /**
   * Model calls that ended with no usage object. Any at all and the totals are
   * not a measurement: a later call cannot fill in a number the provider never
   * sent.
   */
  unreportedCalls: number;
}

/** One closed model call: its own counts, and the reported running sum after it. */
export interface UsageTimelinePoint {
  /** 1-based frame position, aligned with AgentRunController.viewIndex. */
  index: number;
  timestamp: number;
  turnNumber: number;
  model: string;
  /** This call's own counts; null when the call reported no usage. */
  call: UsageTotals | null;
  /** An auto-approval evaluation's call rather than the agent's own. */
  decision?: boolean;
  /** Optional counts this call's usage left null, which is not the same as zero. */
  missing?: readonly OptionalUsageField[];
  /** Wall time from this operation's matching start frame, when both were observed. */
  durationSeconds: number | null;
  /** Every reported call so far, so a lower bound once any call reported nothing. */
  tokens: UsageTotals;
  estimatedCostUsd: number | null;
  /** The complete usage reading at this call, reused while the replay cursor moves. */
  usage: CostSummary;
}

export type OptionalUsageField = "cached" | "thought" | "toolUse";

const OPTIONAL_FIELDS: readonly [OptionalUsageField, keyof TokenUsage][] = [
  ["cached", "cached_tokens"],
  ["thought", "thought_tokens"],
  ["toolUse", "tool_use_tokens"]
];

const pricing: Record<string, ModelPricing> = {
  "gemini-3.5-flash": {
    inputPerMillion: 0.35,
    outputPerMillion: 1.05,
    thoughtPerMillion: 1.05,
    cachedPerMillion: 0.0875
  },
  "gemini-3.1-flash-lite": {
    inputPerMillion: 0.1,
    outputPerMillion: 0.4,
    thoughtPerMillion: 0.4,
    cachedPerMillion: 0.025
  }
};

const emptyTotals = (): UsageTotals => ({
  input: 0,
  output: 0,
  thought: 0,
  cached: 0,
  toolUse: 0,
  total: 0
});

const copyTotals = (tokens: UsageTotals): UsageTotals => ({ ...tokens });

function addUsage(totals: UsageTotals, usage: TokenUsage): void {
  totals.input += usage.input_tokens ?? 0;
  totals.output += usage.output_tokens ?? 0;
  totals.thought += usage.thought_tokens ?? 0;
  totals.cached += usage.cached_tokens ?? 0;
  totals.toolUse += usage.tool_use_tokens ?? 0;
  /* The five fields above OVERLAP, so their sum is not a token count. `cached` is
     the slice of `input` the prompt cache served; for the OpenAI and pydantic-ai
     producers `thought` is likewise the slice of `output` spent reasoning. Gemini
     instead reports thought and tool-use outside input/output and folds them into
     its own `total_tokens` ("prompt + responses + other internal tokens"). Only
     the provider knows which convention it used, which is why the protocol ships
     a grand total and documents it as "not necessarily the sum of the parts".
     Falling back to input + output keeps the previous answer for a producer that
     reports no total of its own. */
  totals.total +=
    usage.total_tokens ?? (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
}

function estimate(model: string, tokens: UsageTotals): number | null {
  const p = pricing[model];
  if (!p) return null;
  return (
    (tokens.input / 1_000_000) * p.inputPerMillion +
    (tokens.output / 1_000_000) * p.outputPerMillion +
    (tokens.thought / 1_000_000) * (p.thoughtPerMillion ?? p.outputPerMillion) +
    (tokens.cached / 1_000_000) * (p.cachedPerMillion ?? p.inputPerMillion) +
    (tokens.toolUse / 1_000_000) * (p.toolUsePerMillion ?? 0)
  );
}

function timestampOf(frame: AgentSseFrame): number | null {
  if (!("type" in frame.data)) return null;
  return frame.data.timestamp;
}

interface MeteredUsage {
  model: string;
  usage: TokenUsage;
}

function tokenCount(value: unknown): number | null {
  return typeof value === "number" &&
    Number.isFinite(value) &&
    Number.isInteger(value) &&
    value >= 0
    ? value
    : null;
}

/**
 * Every event that closes a model-metered operation.
 *
 * Jev runs outside the agent's ordinary model span, so its usage only exists in
 * the structured audit record on `auto_approval_evaluation_ended`. Reading that
 * record here keeps the headline, chart, and per-model rows on the same ledger.
 */
function meteredUsage(frame: AgentSseFrame): MeteredUsage | null {
  if (
    frame.event === "model_interaction_ended" &&
    "type" in frame.data &&
    frame.data.usage
  ) {
    return { model: frame.data.model ?? "unknown", usage: frame.data.usage };
  }

  if (frame.event !== "auto_approval_evaluation_ended") return null;
  const details = frame.data.details;
  const rawUsage = details.usage;
  if (typeof rawUsage !== "object" || rawUsage === null || Array.isArray(rawUsage)) {
    return null;
  }

  const values = rawUsage as Record<string, unknown>;
  const usage: TokenUsage = {
    input_tokens: null,
    output_tokens: null,
    thought_tokens: null,
    cached_tokens: null,
    tool_use_tokens: null,
    total_tokens: null
  };
  let measured = false;
  for (const key of Object.keys(usage) as (keyof TokenUsage)[]) {
    const count = tokenCount(values[key]);
    if (count === null) continue;
    usage[key] = count;
    measured = true;
  }
  if (!measured) return null;

  const model =
    typeof details.model === "string" && details.model.trim().length > 0
      ? details.model
      : "unknown";
  return { model, usage };
}

/** A closed model call that sent no usage object, which is not a call that spent zero. */
function unreportedModel(frame: AgentSseFrame): string | null {
  if (frame.event !== "model_interaction_ended" || !("type" in frame.data)) return null;
  return frame.data.usage == null ? (frame.data.model ?? "unknown") : null;
}

export function summarizeCost(frames: AgentSseFrame[]): CostSummary {
  const byModel = new Map<string, { tokens: UsageTotals; unreportedCalls: number }>();
  const aggregate = emptyTotals();
  let unreportedCalls = 0;
  let cacheReadReportedCalls = 0;
  let cacheReadUnreportedCalls = 0;
  const entry = (model: string) => {
    let found = byModel.get(model);
    if (!found) byModel.set(model, (found = { tokens: emptyTotals(), unreportedCalls: 0 }));
    return found;
  };

  for (const frame of frames) {
    const unreported = unreportedModel(frame);
    if (unreported != null) {
      unreportedCalls += 1;
      cacheReadUnreportedCalls += 1;
      entry(unreported).unreportedCalls += 1;
      continue;
    }
    const metered = meteredUsage(frame);
    if (!metered) continue;
    if (metered.usage.cached_tokens != null) cacheReadReportedCalls += 1;
    addUsage(entry(metered.model).tokens, metered.usage);
    addUsage(aggregate, metered.usage);
  }

  const modelBreakdown = [...byModel.entries()].map(([model, { tokens, unreportedCalls }]) => ({
    model,
    tokens,
    estimatedCostUsd: unreportedCalls > 0 ? null : estimate(model, tokens),
    unreportedCalls
  }));

  const knownCost = modelBreakdown.every((item) => item.estimatedCostUsd != null);
  return {
    tokens: aggregate,
    estimatedCostUsd: knownCost
      ? modelBreakdown.reduce(
          (sum, item) => sum + (item.estimatedCostUsd ?? 0),
          0
        )
      : null,
    modelBreakdown,
    unreportedCalls,
    cacheReadReportedCalls,
    cacheReadUnreportedCalls
  };
}

export function buildUsageTimeline(frames: readonly AgentSseFrame[]): UsageTimelinePoint[] {
  return usageTimelineBuilder()(frames);
}

/** The last cumulative usage point reached by a 1-based replay cursor. */
export function usagePointAt(
  points: readonly UsageTimelinePoint[],
  viewIndex: number
): UsageTimelinePoint | undefined {
  let lo = 0;
  let hi = points.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (points[mid]!.index <= viewIndex) lo = mid + 1;
    else hi = mid;
  }
  return points[lo - 1];
}

/**
 * The usage series, one point per model call, folded forward while the frames only
 * grow (same rule as replayTimelineBuilder). It also starts over when the first
 * timestamp arrives late, because every earlier point falls back to it.
 *
 * Hands back the same array until a call is added, so the frames between calls
 * (nearly all of them) do not redraw the chart.
 */
export function usageTimelineBuilder(): (frames: readonly AgentSseFrame[]) => UsageTimelinePoint[] {
  let seen = 0;
  let last: AgentSseFrame | undefined;
  let firstTimestamp: number | undefined;
  let cumulative = emptyTotals();
  let cumulativeCost = 0;
  let hasUnknownCost = false;
  let byModel = new Map<string, { tokens: UsageTotals; unreportedCalls: number }>();
  let unreportedCalls = 0;
  let cacheReadReportedCalls = 0;
  let cacheReadUnreportedCalls = 0;
  let startedAt = new Map<string, number>();
  let points: UsageTimelinePoint[] = [];
  let shared: UsageTimelinePoint[] = [];

  return (frames) => {
    const grows = seen > 0 && frames.length >= seen && frames[seen - 1] === last;
    const before = grows && firstTimestamp != null ? points.length : -1;
    if (before === -1) {
      seen = 0;
      firstTimestamp = undefined;
      for (const frame of frames) {
        firstTimestamp = timestampOf(frame) ?? undefined;
        if (firstTimestamp != null) break;
      }
      cumulative = emptyTotals();
      cumulativeCost = 0;
      hasUnknownCost = false;
      byModel = new Map();
      unreportedCalls = 0;
      cacheReadReportedCalls = 0;
      cacheReadUnreportedCalls = 0;
      startedAt = new Map();
      points = [];
    }
    for (let index = seen; index < frames.length; index += 1) {
      foldFrame(frames[index]!, index);
    }
    seen = frames.length;
    last = frames[seen - 1];
    if (points.length !== before) shared = points.slice();
    return shared;
  };

  function foldFrame(frame: AgentSseFrame, index: number): void {
    if ("type" in frame.data) {
      if (frame.event === "model_interaction_started") {
        startedAt.set(`model:${frame.data.agent_id}`, frame.data.timestamp);
      } else if (frame.event === "auto_approval_evaluation_started") {
        startedAt.set(`evaluation:${frame.data.evaluation_id}`, frame.data.timestamp);
      }
    }
    const unreported = unreportedModel(frame);
    const metered = unreported == null ? meteredUsage(frame) : null;
    let call: UsageTotals | null = null;
    if (unreported != null) {
      /* Unknown tokens cost an unknown amount, at any price. */
      hasUnknownCost = true;
      unreportedCalls += 1;
      cacheReadUnreportedCalls += 1;
      entry(unreported).unreportedCalls += 1;
    } else if (metered) {
      const tokens = (call = emptyTotals());
      addUsage(tokens, metered.usage);
      addUsage(cumulative, metered.usage);
      addUsage(entry(metered.model).tokens, metered.usage);
      if (metered.usage.cached_tokens != null) cacheReadReportedCalls += 1;
      const estimatedCostUsd = estimate(metered.model, tokens);
      if (estimatedCostUsd == null) {
        /* The latch is deliberate and stays: this series is CUMULATIVE, so once a
           term is missing every later sum is a lower bound rather than a value,
           and reporting the known part as if it were the whole is a quieter lie
           than reporting nothing. What is not deliberate is tripping on an
           interaction that spent nothing — zero tokens cost zero at any price, so
           it cannot make the running total unknown. Summed field-wise rather than
           via tokens.total because a provider that reports no grand total of its
           own still leaves thought and tool-use counts outside that fallback. */
        if (
          tokens.input + tokens.output + tokens.thought + tokens.cached + tokens.toolUse >
          0
        ) {
          hasUnknownCost = true;
        }
      } else {
        cumulativeCost += estimatedCostUsd;
      }
    } else {
      return;
    }
    const startKey =
      frame.event === "auto_approval_evaluation_ended"
        ? `evaluation:${frame.data.evaluation_id}`
        : `model:${"type" in frame.data ? frame.data.agent_id : ""}`;
    const start = startedAt.get(startKey);
    startedAt.delete(startKey);

    points.push({
      index: index + 1,
      timestamp: timestampOf(frame) ?? firstTimestamp ?? 0,
      turnNumber: "type" in frame.data ? frame.data.turn_number : 0,
      model: unreported ?? metered!.model,
      call,
      decision: frame.event === "auto_approval_evaluation_ended",
      missing: metered ? OPTIONAL_FIELDS.filter(([, key]) => metered.usage[key] == null).map(([field]) => field) : [],
      durationSeconds: start == null ? null : Math.max(0, (timestampOf(frame) ?? start) - start),
      tokens: copyTotals(cumulative),
      estimatedCostUsd: hasUnknownCost ? null : cumulativeCost,
      usage: snapshot()
    });
  }

  function entry(model: string) {
    let found = byModel.get(model);
    if (!found) byModel.set(model, (found = { tokens: emptyTotals(), unreportedCalls: 0 }));
    return found;
  }

  function snapshot(): CostSummary {
    const modelBreakdown = [...byModel.entries()].map(([model, value]) => ({
      model,
      tokens: copyTotals(value.tokens),
      estimatedCostUsd: value.unreportedCalls > 0 ? null : estimate(model, value.tokens),
      unreportedCalls: value.unreportedCalls
    }));
    return {
      tokens: copyTotals(cumulative),
      estimatedCostUsd: hasUnknownCost ? null : cumulativeCost,
      modelBreakdown,
      unreportedCalls,
      cacheReadReportedCalls,
      cacheReadUnreportedCalls
    };
  }
}

export interface ContextWindowUsage {
  used: number;
  limit: number;
}

/**
 * How full the session agent's context window is: the prompt and reply of its latest
 * reported model call, against the window size the producer reported for that call's
 * model. Subagents and the approval evaluator run in contexts of their own, so only the
 * root's calls count.
 *
 * Null until a call reports both numbers. The window is never guessed from the model
 * name: no producer sends `context_window` yet, so today this is always null.
 */
export function contextWindowUsage(
  entries: readonly { role: string; frame: AgentSseFrame }[]
): ContextWindowUsage | null {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const { role, frame } = entries[index]!;
    if (role !== "parent" || frame.event !== "model_interaction_ended" || !frame.data.usage) continue;
    const limit = tokenCount((frame.data as { context_window?: unknown }).context_window);
    const input = frame.data.usage.input_tokens;
    if (!limit || input == null) return null;
    return { used: input + (frame.data.usage.output_tokens ?? 0), limit };
  }
  return null;
}

export function formatCost(cost: number | null): string {
  if (cost == null) return "—";
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

/** Models in this run that we hold no price for, so their cost is unknown. */
export function unpricedModels(usage: CostSummary): string[] {
  return usage.modelBreakdown
    .filter((item) => item.estimatedCostUsd == null)
    .map((item) => item.model);
}

/** Hover text for a cost we could not compute. Null when the cost is real. */
export function unpricedNote(models: string[]): string | null {
  if (models.length === 0) return null;
  return `No price configured for ${models.join(", ")} — token counts are exact, cost is not estimated.`;
}

const tokenFormat = new Intl.NumberFormat("en-US");

export function formatTokens(value: number): string {
  return tokenFormat.format(value);
}

/**
 * A token count from a summary, the one place unreported usage is written down.
 *
 * Once any call ended without usage the sum is what the reported calls spent, a
 * lower bound; with nothing reported at all it is unknown. Neither is the bare
 * number, and least of all the zero an empty sum prints.
 */
export function tokenFigure(value: number, unreportedCalls: number): string {
  if (unreportedCalls === 0) return formatTokens(value);
  return value > 0 ? `≥ ${formatTokens(value)}` : "—";
}

export const UNREPORTED_TIP =
  "A model call ended without reporting token usage, so this counts only the calls that did: " +
  "a lower bound, not a total.";
