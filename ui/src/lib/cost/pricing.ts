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
  modelBreakdown: Array<{
    model: string;
    tokens: UsageTotals;
    estimatedCostUsd: number | null;
  }>;
}

export interface UsageTimelinePoint {
  index: number;
  event: AgentSseFrame["event"] | "start";
  timestamp: number;
  tokens: UsageTotals;
  estimatedCostUsd: number | null;
}

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

/* `total_tokens` rides on the wire — the harness's TokenUsage defines it — but is
   not yet declared in $lib/api/types, so read it through a local widening rather
   than reach into a module five other agents are editing. */
type ReportedUsage = TokenUsage & { total_tokens?: number | null };

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
  const reported = (usage as ReportedUsage).total_tokens;
  totals.total += reported ?? (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
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

export function summarizeCost(frames: AgentSseFrame[]): CostSummary {
  const byModel = new Map<string, UsageTotals>();
  const aggregate = emptyTotals();

  for (const frame of frames) {
    if (
      frame.event !== "model_interaction_ended" ||
      !("type" in frame.data) ||
      !frame.data.usage
    ) {
      continue;
    }
    const model = frame.data.model ?? "unknown";
    const totals = byModel.get(model) ?? emptyTotals();
    addUsage(totals, frame.data.usage);
    addUsage(aggregate, frame.data.usage);
    byModel.set(model, totals);
  }

  const modelBreakdown = [...byModel.entries()].map(([model, tokens]) => ({
    model,
    tokens,
    estimatedCostUsd: estimate(model, tokens)
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
    modelBreakdown
  };
}

export function buildUsageTimeline(frames: AgentSseFrame[]): UsageTimelinePoint[] {
  const firstTimestamp =
    frames
      .map((frame) => timestampOf(frame))
      .find((timestamp): timestamp is number => timestamp != null) ?? 0;
  const cumulative = emptyTotals();
  let cumulativeCost = 0;
  let hasUnknownCost = false;
  const points: UsageTimelinePoint[] = [
    {
      index: 0,
      event: "start",
      timestamp: firstTimestamp,
      tokens: emptyTotals(),
      estimatedCostUsd: 0
    }
  ];

  frames.forEach((frame, index) => {
    if (
      frame.event === "model_interaction_ended" &&
      "type" in frame.data &&
      frame.data.usage
    ) {
      const tokens = emptyTotals();
      addUsage(tokens, frame.data.usage);
      addUsage(cumulative, frame.data.usage);
      const estimatedCostUsd = estimate(frame.data.model ?? "unknown", tokens);
      if (estimatedCostUsd == null) {
        hasUnknownCost = true;
      } else {
        cumulativeCost += estimatedCostUsd;
      }
    }

    points.push({
      index: index + 1,
      event: frame.event,
      timestamp: timestampOf(frame) ?? firstTimestamp,
      tokens: copyTotals(cumulative),
      estimatedCostUsd: hasUnknownCost ? null : cumulativeCost
    });
  });

  return points;
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

export function formatTokens(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}
