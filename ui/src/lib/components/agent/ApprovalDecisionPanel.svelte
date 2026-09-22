<script lang="ts">
  import { CheckCircle2, GitBranch, ShieldAlert, XCircle } from "@lucide/svelte";
  import Chip, { type ChipTone } from "$lib/components/primitives/Chip.svelte";
  import type {
    ApprovalDecision,
    ApprovalProbabilities,
    ApprovalVerdict
  } from "$lib/state/approvalDecisionTree";

  interface Props {
    decisions: ApprovalDecision[];
  }

  let { decisions }: Props = $props();
  let chosenKey = $state<string | null>(null);

  const newestFirst = $derived([...decisions].reverse());
  /* Null follows the live edge; a deliberate selection stays put until that
     decision disappears because the cursor or session moved behind it. */
  const current = $derived(
    decisions.find((decision) => decision.key === chosenKey) ?? decisions.at(-1) ?? null
  );

  function percent(value: number): string {
    const scaled = value * 100;
    return `${Number.isInteger(scaled) ? scaled : scaled.toFixed(1)}%`;
  }

  function verdictLabel(verdict: ApprovalVerdict): string {
    if (verdict === "approve") return "Approve";
    if (verdict === "deny") return "Deny";
    return "Escalate";
  }

  function verdictTone(verdict: ApprovalVerdict): ChipTone {
    if (verdict === "approve") return "success";
    if (verdict === "deny") return "error";
    return "live";
  }

  function outcomeLabel(verdict: ApprovalVerdict): string {
    if (verdict === "approve") return "Auto-approved";
    if (verdict === "deny") return "Call denied";
    return "Human review";
  }

  function probability(
    probabilities: ApprovalProbabilities,
    verdict: ApprovalVerdict
  ): number | null {
    return probabilities[verdict];
  }

  function approveDestination(decision: ApprovalDecision): string {
    return decision.irreversibleThreshold === null ? "Approve" : "Risk check";
  }

  function criteriaLabel(decision: ApprovalDecision): string | null {
    if (!decision.criteriaSet) return null;
    return decision.criteriaVersion === null
      ? decision.criteriaSet
      : `${decision.criteriaSet} · v${decision.criteriaVersion}`;
  }
</script>

<section class="approval-decisions" aria-label="Automatic approval decisions">
  {#if !current}
    <div class="empty">
      <GitBranch size={22} aria-hidden="true" />
      <h3>No completed approval decisions</h3>
      <p>
        Completed <code>auto_approval_evaluation_ended</code> events will appear here as
        the replay cursor reaches them.
      </p>
    </div>
  {:else}
    <div class="decision-layout">
      <nav class="evaluation-list" aria-label="Completed approval evaluations">
        <div class="list-head">
          <span class="kicker">Completed</span>
          <span class="list-count">{decisions.length}</span>
        </div>
        <div class="evaluation-items">
          {#each newestFirst as decision (decision.key)}
            <button
              type="button"
              class="evaluation"
              class:chosen={decision.key === current.key}
              aria-pressed={decision.key === current.key}
              onclick={() => (chosenKey = decision.key)}
            >
              <span class="evaluation-main">
                <span class="tool-name">{decision.toolName}</span>
                <span class="evaluation-meta">
                  turn {decision.turnNumber} · {decision.agentLabel}
                </span>
              </span>
              <span class="evaluation-signal">
                {#if decision.confidence !== null}
                  <span class="confidence-figure">{percent(decision.confidence)}</span>
                {/if}
                <Chip
                  label={verdictLabel(decision.verdict)}
                  tone={verdictTone(decision.verdict)}
                  size="xs"
                />
              </span>
            </button>
          {/each}
        </div>
      </nav>

      <article class="trace" aria-label={`Decision path for ${current.toolName}`}>
        <header class="trace-head">
          <div class="trace-title">
            <p class="kicker">Decision trace</p>
            <h3>{current.toolName}</h3>
            <p class="trace-meta">
              {current.evaluator}, turn {current.turnNumber}{#if current.role === "subagent"}
                , {current.agentLabel}{/if}
            </p>
          </div>
          <Chip
            label={outcomeLabel(current.verdict)}
            tone={verdictTone(current.verdict)}
            size="xs"
            toned
          />
        </header>

        <ol class="decision-tree" aria-label="Decision stages">
          {#each current.stages as stage}
            <li class={`stage ${stage.kind}`}>
              <span class="stage-marker" aria-hidden="true"></span>

              {#if stage.kind === "confidence"}
                <section class="decision-node">
                  <div class="node-head">
                    <div>
                      <p class="kicker">Confidence gate</p>
                      <h4>Is the verdict certain enough?</h4>
                    </div>
                    <strong class="score">{percent(stage.score)}</strong>
                  </div>

                  <div
                    class:gate-failed={stage.passed === false}
                    class="meter"
                    style={`--score: ${stage.score}; --threshold: ${stage.threshold ?? 0}`}
                    role="meter"
                    aria-label="Verdict confidence"
                    aria-valuemin="0"
                    aria-valuemax="100"
                    aria-valuenow={stage.score * 100}
                    aria-valuetext={`${percent(stage.score)} confidence${stage.threshold === null ? "" : `; ${percent(stage.threshold)} required`}`}
                  >
                    <span class="meter-fill"></span>
                    {#if stage.threshold !== null}
                      <span class="threshold-mark" aria-hidden="true"></span>
                    {/if}
                  </div>
                  <div class="meter-scale">
                    <span>0%</span>
                    {#if stage.threshold !== null}
                      <span>{percent(stage.threshold)} bar</span>
                    {:else}
                      <span>bar not reported</span>
                    {/if}
                    <span>100%</span>
                  </div>

                  {#if stage.threshold === null}
                    <div class="branch-grid one">
                      <div class="branch taken neutral">
                        <span class="branch-label">No threshold in event</span>
                        <strong>Use evaluator verdict</strong>
                      </div>
                    </div>
                  {:else}
                    <div class="branch-grid">
                      <div class="branch error" class:taken={stage.passed === false}>
                        <span class="branch-label">Below {percent(stage.threshold)}</span>
                        <strong>Escalate</strong>
                      </div>
                      <div class="branch success" class:taken={stage.passed === true}>
                        <span class="branch-label">At least {percent(stage.threshold)}</span>
                        <strong>Continue</strong>
                      </div>
                    </div>
                  {/if}
                </section>
              {:else if stage.kind === "verdict"}
                <section class="decision-node">
                  <div class="node-head">
                    <div>
                      <p class="kicker">Evaluator verdict</p>
                      <h4>Which policy branch applies?</h4>
                    </div>
                    <strong class={`verdict-word ${stage.verdict}`}>
                      {verdictLabel(stage.verdict)}
                    </strong>
                  </div>

                  <div class="branch-grid three">
                    {#each ["approve", "deny", "escalate"] as branchVerdict}
                      {@const typedVerdict = branchVerdict as ApprovalVerdict}
                      {@const branchProbability = probability(stage.probabilities, typedVerdict)}
                      <div
                        class={`branch ${typedVerdict}`}
                        class:taken={stage.verdict === typedVerdict}
                        data-verdict={typedVerdict}
                      >
                        <span class="branch-label">
                          {verdictLabel(typedVerdict)}{#if branchProbability !== null}
                            · {percent(branchProbability)}{/if}
                        </span>
                        <strong>
                          {typedVerdict === "approve"
                            ? approveDestination(current)
                            : typedVerdict === "deny"
                              ? "Deny call"
                              : "Human review"}
                        </strong>
                      </div>
                    {/each}
                  </div>
                </section>
              {:else if stage.kind === "irreversibility"}
                <section class="decision-node">
                  <div class="node-head">
                    <div>
                      <p class="kicker">Effect gate</p>
                      <h4>Can the action be safely automated?</h4>
                    </div>
                    <strong class="score risk-score">{percent(stage.score)}</strong>
                  </div>

                  <div
                    class:gate-failed={!stage.passed}
                    class="meter risk-meter"
                    style={`--score: ${stage.score}; --threshold: ${stage.threshold}`}
                    role="meter"
                    aria-label="Estimated irreversibility"
                    aria-valuemin="0"
                    aria-valuemax="100"
                    aria-valuenow={stage.score * 100}
                    aria-valuetext={`${percent(stage.score)} irreversible; ${percent(stage.threshold)} ceiling`}
                  >
                    <span class="meter-fill"></span>
                    <span class="threshold-mark" aria-hidden="true"></span>
                  </div>
                  <div class="meter-scale">
                    <span>Reversible</span>
                    <span>{percent(stage.threshold)} ceiling</span>
                    <span>Irreversible</span>
                  </div>

                  <div class="branch-grid">
                    <div class="branch success" class:taken={stage.passed}>
                      <span class="branch-label">At or below ceiling</span>
                      <strong>Approve</strong>
                    </div>
                    <div class="branch error" class:taken={!stage.passed}>
                      <span class="branch-label">Above ceiling</span>
                      <strong>Escalate</strong>
                    </div>
                  </div>
                </section>
              {:else}
                <section
                  class={`decision-node outcome-node ${stage.verdict}`}
                  data-verdict={stage.verdict}
                >
                  <div class="outcome-icon" aria-hidden="true">
                    {#if stage.verdict === "approve"}
                      <CheckCircle2 size={20} />
                    {:else if stage.verdict === "deny"}
                      <XCircle size={20} />
                    {:else}
                      <ShieldAlert size={20} />
                    {/if}
                  </div>
                  <div class="outcome-copy">
                    <p class="kicker">Final verdict</p>
                    <h4>{outcomeLabel(stage.verdict)}</h4>
                    {#if current.reason}
                      <p class="reason">{current.reason}</p>
                    {/if}
                  </div>
                </section>
              {/if}
            </li>
          {/each}
        </ol>

        <dl class="audit-meta">
          {#if criteriaLabel(current)}
            <div>
              <dt class="kicker">Criteria</dt>
              <dd>{criteriaLabel(current)}</dd>
            </div>
          {/if}
          {#if current.model}
            <div>
              <dt class="kicker">Model</dt>
              <dd>{current.model}</dd>
            </div>
          {/if}
          <div>
            <dt class="kicker">Evaluation</dt>
            <dd>{current.evaluationId}</dd>
          </div>
          {#if current.requestId}
            <div>
              <dt class="kicker">Request</dt>
              <dd>{current.requestId}</dd>
            </div>
          {/if}
        </dl>
      </article>
    </div>
  {/if}
</section>

<style>
  .approval-decisions {
    container: approval-panel / inline-size;
    min-width: 0;
    min-height: 0;
    height: 100%;
    padding: var(--gutter);
    background: var(--surface-1);
    overflow: hidden;
  }

  .decision-layout {
    display: grid;
    grid-template-columns: minmax(176px, 0.62fr) minmax(340px, 1.38fr);
    gap: var(--gutter);
    min-width: 0;
    min-height: 0;
    height: 100%;
  }

  .evaluation-list,
  .trace {
    min-width: 0;
    min-height: 0;
    border: 1px solid var(--border);
    background: var(--surface-0);
  }

  .evaluation-list {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .list-head {
    flex: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: var(--control-height);
    padding-inline: var(--gutter-tight);
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }

  .list-count,
  .confidence-figure {
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--figure-size);
    font-variant-numeric: tabular-nums;
  }

  .evaluation-items {
    display: grid;
    align-content: start;
    gap: 1px;
    min-height: 0;
    overflow: auto;
    background: var(--border);
  }

  .evaluation {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--gap-md);
    min-height: 58px;
    padding: var(--gutter-tight);
    border: 0;
    background: var(--surface-1);
    color: var(--text-1);
    text-align: left;
    cursor: pointer;
    transition:
      background var(--duration-fast) var(--ease-out),
      border-color var(--duration-fast) var(--ease-out),
      transform var(--duration-press) var(--ease-out);
  }

  .evaluation:active {
    transform: scale(0.99);
  }

  .evaluation.chosen {
    background: var(--surface-2);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 60%, var(--border));
  }

  .evaluation:focus-visible {
    position: relative;
    z-index: 1;
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  .evaluation-main,
  .evaluation-signal {
    min-width: 0;
  }

  .evaluation-main {
    display: grid;
    gap: var(--gap-xs);
  }

  .tool-name {
    min-width: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: var(--font-lg);
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .evaluation-meta,
  .trace-meta {
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    line-height: 1.4;
  }

  .evaluation-signal {
    display: grid;
    justify-items: end;
    gap: var(--gap-xs);
  }

  .trace {
    overflow: auto;
    padding: var(--gutter);
  }

  .trace-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--gutter);
    padding-bottom: var(--gutter);
    border-bottom: 1px solid var(--border);
  }

  .trace-title {
    min-width: 0;
  }

  .trace-title > :is(p, h3) {
    margin: 0;
  }

  .trace-title h3 {
    margin-top: var(--gap-xs);
    overflow-wrap: anywhere;
    color: var(--text-1);
    font-size: var(--font-title);
    font-weight: 600;
    letter-spacing: -0.025em;
    line-height: 1.15;
  }

  .trace-meta {
    margin-top: var(--gap-sm) !important;
  }

  .decision-tree {
    position: relative;
    display: grid;
    gap: var(--gutter);
    margin: var(--gutter) 0 0;
    padding: 0 0 0 30px;
    list-style: none;
  }

  .decision-tree::before {
    content: "";
    position: absolute;
    inset-block: 10px;
    inset-inline-start: 9px;
    width: 1px;
    background: var(--border-strong);
  }

  .stage {
    position: relative;
    min-width: 0;
  }

  .stage-marker {
    position: absolute;
    inset-block-start: 17px;
    inset-inline-start: -25px;
    z-index: 1;
    width: 9px;
    height: 9px;
    border: 1px solid var(--border-strong);
    background: var(--surface-0);
  }

  .stage.outcome .stage-marker {
    border-color: color-mix(in srgb, var(--outcome-color, var(--accent)) 55%, var(--border));
    background: var(--outcome-color, var(--accent));
  }

  .stage.outcome:has(.outcome-node.approve) .stage-marker {
    --outcome-color: var(--success);
  }

  .stage.outcome:has(.outcome-node.deny) .stage-marker {
    --outcome-color: var(--error);
  }

  .stage.outcome:has(.outcome-node.escalate) .stage-marker {
    --outcome-color: var(--live);
  }

  .decision-node {
    min-width: 0;
    padding: var(--gutter);
    border: 1px solid var(--border);
    background: var(--surface-1);
  }

  .node-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--gutter);
  }

  .node-head :is(p, h4),
  .outcome-copy :is(p, h4) {
    margin: 0;
  }

  .node-head h4,
  .outcome-copy h4 {
    margin-top: var(--gap-xs);
    color: var(--text-1);
    font-size: var(--font-xl);
    font-weight: 600;
    line-height: 1.3;
    text-wrap: balance;
  }

  .score,
  .verdict-word {
    flex: none;
    color: var(--accent-soft);
    font-family: var(--font-mono);
    font-size: var(--font-display);
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }

  .risk-score {
    color: var(--warning);
  }

  .verdict-word {
    font-size: var(--font-lg);
    letter-spacing: var(--letter-tight);
    text-transform: uppercase;
  }

  .verdict-word.approve {
    color: var(--success);
  }

  .verdict-word.deny {
    color: var(--error);
  }

  .verdict-word.escalate {
    color: var(--live);
  }

  .meter {
    position: relative;
    height: 10px;
    margin-top: var(--gutter);
    border: 1px solid var(--border-strong);
    background: var(--surface-0);
    overflow: visible;
  }

  .meter-fill {
    position: absolute;
    inset: 0;
    background: var(--accent);
    transform: scaleX(var(--score));
    transform-origin: left center;
  }

  .risk-meter .meter-fill {
    background: var(--warning);
  }

  .meter.gate-failed .meter-fill {
    background: var(--error);
  }

  .threshold-mark {
    position: absolute;
    inset-block: -5px;
    inset-inline-start: calc(var(--threshold) * 100%);
    width: 1px;
    background: var(--text-1);
    box-shadow: 0 0 0 1px var(--surface-0);
  }

  .meter-scale {
    display: flex;
    justify-content: space-between;
    gap: var(--gap-md);
    margin-top: var(--gap-xs);
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    font-variant-numeric: tabular-nums;
  }

  .branch-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--gap-sm);
    margin-top: var(--gutter);
  }

  .branch-grid.three {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .branch-grid.one {
    grid-template-columns: minmax(0, 1fr);
  }

  .branch {
    --branch-color: var(--text-3);
    display: grid;
    align-content: center;
    gap: var(--gap-xs);
    min-width: 0;
    min-height: 48px;
    padding: var(--gap-sm) var(--gutter-tight);
    border: 1px solid var(--border);
    border-block-start: 2px solid color-mix(in srgb, var(--branch-color) 34%, var(--border));
    background: var(--surface-0);
    color: var(--text-3);
  }

  .branch.approve,
  .branch.success {
    --branch-color: var(--success);
  }

  .branch.deny,
  .branch.error {
    --branch-color: var(--error);
  }

  .branch.escalate {
    --branch-color: var(--live);
  }

  .branch.taken {
    border-color: color-mix(in srgb, var(--branch-color) 45%, var(--border));
    border-block-start-color: var(--branch-color);
    background: color-mix(in srgb, var(--branch-color) 8%, var(--surface-0));
    color: var(--text-1);
  }

  .branch-label {
    min-width: 0;
    overflow-wrap: anywhere;
    color: inherit;
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    font-variant-numeric: tabular-nums;
    line-height: 1.35;
  }

  .branch strong {
    min-width: 0;
    overflow-wrap: anywhere;
    color: inherit;
    font-size: var(--font-sm);
    font-weight: 600;
    line-height: 1.3;
  }

  .outcome-node {
    --outcome-color: var(--live);
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--gutter-tight);
    border-color: color-mix(in srgb, var(--outcome-color) 48%, var(--border));
    background: color-mix(in srgb, var(--outcome-color) 8%, var(--surface-1));
  }

  .outcome-node.approve {
    --outcome-color: var(--success);
  }

  .outcome-node.deny {
    --outcome-color: var(--error);
  }

  .outcome-icon {
    display: inline-flex;
    color: var(--outcome-color);
  }

  .reason {
    margin-top: var(--gutter-tight) !important;
    max-width: 72ch;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.55;
    overflow-wrap: anywhere;
    text-wrap: pretty;
  }

  .audit-meta {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: var(--gap-xs);
    margin: var(--gutter) 0 0 30px;
  }

  .audit-meta > div {
    min-width: 0;
    padding: var(--gutter-tight);
    border: 1px solid var(--border);
    background: var(--surface-1);
  }

  .audit-meta :is(dt, dd) {
    margin: 0;
  }

  .audit-meta dd {
    margin-top: var(--gap-xs);
    overflow-wrap: anywhere;
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    line-height: 1.45;
  }

  .empty {
    display: grid;
    align-content: start;
    justify-items: start;
    gap: var(--gutter-tight);
    max-width: 560px;
    padding: var(--gutter);
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-3);
  }

  .empty :is(h3, p) {
    margin: 0;
  }

  .empty h3 {
    color: var(--text-1);
    font-size: var(--font-xl);
  }

  .empty p {
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.5;
  }

  .empty code {
    color: var(--accent-soft);
    font-family: var(--font-mono);
    font-size: inherit;
    overflow-wrap: anywhere;
  }

  @media (hover: hover) and (pointer: fine) {
    .evaluation:hover {
      background: var(--control-hover);
    }
  }

  @container approval-panel (max-width: 620px) {
    .decision-layout {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: minmax(116px, 0.34fr) minmax(0, 1.66fr);
    }
  }

  @container approval-panel (max-width: 430px) {
    .approval-decisions {
      padding: var(--gutter-tight);
    }

    .trace {
      padding: var(--gutter-tight);
    }

    .trace-head,
    .node-head {
      align-items: flex-start;
      flex-direction: column;
      gap: var(--gutter-tight);
    }

    .decision-tree {
      padding-inline-start: 26px;
    }

    .stage-marker {
      inset-inline-start: -22px;
    }

    .branch-grid,
    .branch-grid.three {
      grid-template-columns: minmax(0, 1fr);
    }

    .audit-meta {
      margin-inline-start: 26px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .evaluation {
      transition: none;
    }
  }
</style>
