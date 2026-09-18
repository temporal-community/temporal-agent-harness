<script lang="ts">
  import {
    Activity,
    ArrowUpRight,
    Brain,
    Check,
    Circle,
    Code2,
    Crosshair,
    FileCode2,
    GitBranch,
    ShieldCheck,
    Terminal,
  } from "@lucide/svelte";
  import type { State } from "./types";

  let {
    progress,
    live,
    version,
    turnCalls,
    onfile,
    onreview,
    ontrace,
    onchecks,
  }: {
    progress: State;
    live: boolean;
    version: number;
    turnCalls: number;
    onfile: (path: string) => void;
    onreview: () => void;
    ontrace: (identity?: string) => void;
    onchecks: () => void;
  } = $props();
  let following = $state(true);
  let pinned = $state("");
  let inspector = $state("artifact");
  let actors = $derived(progress.actors ?? []);
  let current = $derived(
    [...actors].reverse().find((a) => a.status === "running") ?? actors.at(-1),
  );
  let selected = $derived(
    (following ? current : actors.find((a) => a.id === pinned)) ?? current,
  );
  let blueprint = $derived(progress.blueprints?.at(-1));
  let role = $derived(selected?.role ?? "coordinator");
  let remaining = $derived(
    progress.steps.filter((step) => step.status !== "completed"),
  );
  let output = $derived(
    [...progress.entries]
      .reverse()
      .find((entry) => entry.role === (role === "coordinator" ? "agent" : role))
      ?.text,
  );
  let operationPath = $derived(selected?.operation.split(" · ").at(1) ?? "");
  let relevantFiles = $derived([
    ...new Set([
      ...(operationPath &&
      /^(read|edit|patch) · /.test(selected?.operation ?? "")
        ? [operationPath]
        : []),
      ...progress.files_changed,
    ]),
  ]);
  const icon = (name: string) =>
    name === "implementer" ? Code2 : name === "reviewer" ? ShieldCheck : Brain;
  function choose(identity: string) {
    pinned = identity;
    following = false;
    inspector = "artifact";
  }
</script>

<section class="execution" aria-label="Execution workspace">
  <div class="execution-bar">
    <span class="eyebrow"
      ><span class:live={live && progress.status === "running"} class="signal"
      ></span>{!live
        ? "SAVED STATE"
        : progress.status === "running"
          ? "LIVE EXECUTION"
          : "EXECUTION WORKSPACE"}</span
    >
    <span class="state-version"
      >state v{version} · {turnCalls}/{progress.max_steps} calls this message</span
    >
  </div>
  <ol class="stage-track" aria-label="Agent lifecycle">
    {#each progress.steps as step, index}
      <li
        class:done={step.status === "completed"}
        class:current={step.status === "active"}
        aria-current={step.status === "active" ? "step" : undefined}
      >
        <span class="stage-number"
          >{#if step.status === "completed"}<Check size={12} />{:else}{String(
              index + 1,
            ).padStart(2, "0")}{/if}</span
        >
        <span
          >{step.title}<small
            >{step.status === "active"
              ? progress.status.replaceAll("_", " ")
              : step.status}</small
          ></span
        >
      </li>
    {/each}
  </ol>
  <div class="execution-grid">
    <div class="agent-map">
      <div class="map-heading">
        <span class="eyebrow">AGENTS</span><button
          class:following
          aria-pressed={following}
          title="Follow the current agent"
          onclick={() => {
            following = !following;
            pinned = selected?.id ?? "";
          }}><Crosshair size={13} />Follow live</button
        >
      </div>
      <div class="agent-chain">
        {#each actors as actor, index}
          {@const Icon = icon(actor.role)}
          <button
            class="agent-node"
            class:selected={selected?.id === actor.id}
            class:working={actor.id === current?.id &&
              progress.status === "running"}
            aria-pressed={selected?.id === actor.id}
            onclick={() => choose(actor.id)}
          >
            <div class="node-label">
              <span class="node-icon"><Icon size={19} strokeWidth={1.5} /></span
              ><strong>{actor.role}</strong><small
                >{String(index + 1).padStart(2, "0")}</small
              >
            </div>
            <div class="node-status">
              <span></span>{actor.status === "running" &&
              progress.status !== "running"
                ? progress.status.replaceAll("_", " ")
                : actor.status}
            </div>
            <p>{actor.operation}</p>
            <span class="node-count"
              >{actor.model_calls} calls <span>·</span>
              {actor.host_calls} tools</span
            >
          </button>
        {:else}
          <p class="waiting">
            The coordinator will appear when the first operation starts.
          </p>
        {/each}
      </div>
      <div class="authority">
        <GitBranch size={13} /><span
          >{progress.mode === "ask"
            ? "Read-only investigation"
            : "Isolated workspace · one writer"}</span
        >
      </div>
    </div>
    <div class="artifact-inspector">
      <div class="inspector-tabs" aria-label="Agent inspection">
        <button
          class:chosen={inspector === "artifact"}
          onclick={() => (inspector = "artifact")}>Artifact</button
        >
        <button
          class:chosen={inspector === "remaining"}
          onclick={() => (inspector = "remaining")}
          >Remaining <span>{remaining.length}</span></button
        >
        <button
          class="trace-link"
          title="Open the session trace for all agents"
          onclick={() => ontrace()}
          ><Activity size={13} />Trace <ArrowUpRight size={12} /></button
        >
      </div>
      <div class="inspector-body">
        {#if inspector === "remaining"}
          <span class="eyebrow">WHAT HAPPENS NEXT</span>
          <h2>{progress.next_action || "Waiting for the next instruction"}</h2>
          <ol class="remaining-list">
            {#each remaining as step}<li>
                <Circle size={12} /><span
                  >{step.title}<small>{step.status}</small></span
                >
              </li>{:else}<li>
                <Check size={14} />All planned steps are complete.
              </li>{/each}
          </ol>
          {#if progress.missing_evidence?.length}<h3>Evidence still needed</h3>
            <ul>
              {#each progress.missing_evidence as item}<li>{item}</li>{/each}
            </ul>{/if}
          {#if progress.gate}<div class="decision-hint">
              <strong>{progress.gate.title}</strong>
              <p>Respond in the decision panel below to continue.</p>
            </div>{/if}
        {:else if role === "coordinator"}
          <span class="eyebrow"
            >{blueprint
              ? `BLUEPRINT ${blueprint.revision} / ${blueprint.approved ? "APPROVED" : "PROPOSED"}`
              : "REPOSITORY UNDERSTANDING"}</span
          >
          <h2>{blueprint?.summary || progress.focus}</h2>
          {#if blueprint}
            <h3>Acceptance requirements</h3>
            <ul class="requirements">
              {#each blueprint.requirements as requirement}<li>
                  <span></span>{requirement}
                </li>{/each}
            </ul>
            {#if blueprint.assumptions.length}<h3>Assumptions</h3>
              <ul>
                {#each blueprint.assumptions as assumption}<li>
                    {assumption}
                  </li>{/each}
              </ul>{/if}
            <h3>Write scope</h3>
            <div class="scope">
              {#each blueprint.scope as path}<code>{path}</code>{:else}<span
                  >No file writes authorized.</span
                >{/each}
            </div>
          {:else}<p class="agent-output">
              {output ||
                "The agent is inspecting the request and repository. Its blueprint will appear here."}
            </p>{/if}
          {#if progress.risks.length}<h3>Risks to account for</h3>
            <ul>
              {#each progress.risks as risk}<li>{risk}</li>{/each}
            </ul>{/if}
        {:else if role === "implementer"}
          <span class="eyebrow">IMPLEMENTATION / {selected?.status}</span>
          <h2>
            {selected?.status === "running"
              ? "Working in your task workspace"
              : "Latest implementation output"}
          </h2>
          <p class="agent-output">
            {selected?.status === "running"
              ? selected.operation
              : output || selected?.operation}
          </p>
          <h3>Files touched</h3>
          <div class="artifact-files">
            {#each relevantFiles as path}<button onclick={() => onfile(path)}
                ><FileCode2 size={14} /><code>{path}</code><ArrowUpRight
                  size={13}
                /></button
              >{:else}<p>
                File changes appear after the agent records an edit.
              </p>{/each}
          </div>
          {#if blueprint}<h3>Approved write scope</h3>
            <div class="scope">
              {#each blueprint.scope as path}<code>{path}</code>{/each}
            </div>{/if}
          <button class="artifact-action" onclick={onreview}
            >Review changes <ArrowUpRight size={14} /></button
          >
        {:else}
          <span class="eyebrow"
            >INDEPENDENT REVIEW / {progress.verification?.replaceAll(
              "_",
              " ",
            ) ?? "NOT RUN"}</span
          >
          <h2>Latest review evidence</h2>
          <p class="agent-output">{output || selected?.operation}</p>
          <button class="check-summary" onclick={onchecks}
            ><Terminal size={16} /><span
              >{progress.checks.filter((c) => !c.stale && c.exit_code === 0)
                .length} passing · {progress.checks.filter(
                (c) => !c.stale && c.exit_code !== 0,
              ).length} failing · {progress.checks.filter((c) => c.stale)
                .length} stale</span
            ><ArrowUpRight size={13} /></button
          >
          {#each progress.findings ?? [] as finding}<div class="finding">
              <span>{finding.severity}</span><button
                onclick={() => onfile(finding.path)}
                ><code
                  >{finding.path}{finding.line ? `:${finding.line}` : ""}</code
                ></button
              >
              <p>{finding.explanation}</p>
            </div>{/each}
          {#if progress.missing_evidence?.length}<h3>Still needed</h3>
            <ul>
              {#each progress.missing_evidence as item}<li>{item}</li>{/each}
            </ul>{/if}
          {#if progress.review_revision}<p class="revision">
              Reviewed source <code
                >{progress.review_revision.slice(0, 12)}</code
              >. Checks and findings describe that revision.
            </p>{/if}
          <button class="artifact-action" onclick={onreview}
            >Open your review <ArrowUpRight size={14} /></button
          >
        {/if}
      </div>
    </div>
  </div>
  <div class="execution-footer">
    <span
      >{progress.gate
        ? "DECISION REQUIRED"
        : progress.status.replaceAll("_", " ").toUpperCase()}</span
    >
    <p>{progress.gate?.title || progress.next_action || progress.focus}</p>
    <span class="operations">{progress.host_calls ?? 0} host operations</span>
  </div>
</section>

<style>
  .execution {
    border-bottom: 1px solid var(--border);
    background: var(--surface-0);
    container-type: inline-size;
  }
  .execution-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 16px 20px;
  }
  .execution-bar .eyebrow {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .signal {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--text-3);
  }
  .signal.live {
    background: var(--live);
    box-shadow: 0 0 12px color-mix(in srgb, var(--live) 35%, transparent);
  }
  .state-version {
    font: 10px var(--font-mono);
    color: var(--text-3);
  }
  .stage-track {
    display: flex;
    list-style: none;
    margin: 0;
    padding: 0 20px 16px;
    gap: 8px;
    overflow: auto;
  }
  .stage-track li {
    flex: 1;
    display: flex;
    gap: 8px;
    align-items: center;
    padding: 10px 0;
    border-top: 1px solid var(--border-strong);
    color: var(--text-3);
    min-width: 105px;
    font-size: 11px;
  }
  .stage-track li.current {
    color: var(--live);
    border-color: var(--live);
  }
  .stage-track li.done {
    color: var(--text-2);
    border-color: var(--accent);
  }
  .stage-track small {
    display: block;
    font-size: 9px;
    margin-top: 3px;
    opacity: 0.8;
  }
  .stage-number {
    display: grid;
    place-items: center;
    min-width: 22px;
    font: 10px var(--font-mono);
  }
  .execution-grid {
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr);
    min-height: 420px;
    border-top: 1px solid var(--border);
  }
  .agent-map {
    min-width: 0;
    padding: 18px;
    border-right: 1px solid var(--border);
    background-image: radial-gradient(var(--border) 0.65px, transparent 0.65px);
    background-size: 16px 16px;
  }
  .map-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 18px;
  }
  .map-heading button {
    font-size: 10px;
    min-height: 26px;
    padding: 4px 6px;
    background: var(--surface-0);
  }
  .map-heading .following {
    color: var(--accent-soft);
    border-color: var(--accent-border);
  }
  .agent-chain {
    display: grid;
    gap: 22px;
  }
  .agent-node {
    display: block;
    position: relative;
    width: 100%;
    text-align: left;
    padding: 13px;
    border-radius: 6px;
    border-color: var(--border);
    background: var(--surface-1);
  }
  .agent-node + .agent-node::before {
    content: "";
    position: absolute;
    left: 25px;
    bottom: 100%;
    height: 23px;
    width: 1px;
    background: var(--border-strong);
  }
  .agent-node.selected {
    border-color: var(--accent);
    box-shadow: inset 2px 0 var(--accent);
    background: var(--accent-bg);
  }
  .node-label {
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .node-icon {
    color: var(--accent-soft);
  }
  .node-label strong {
    text-transform: capitalize;
    font-size: 12px;
  }
  .node-label small {
    margin-left: auto;
    color: var(--text-3);
    font: 10px var(--font-mono);
  }
  .node-status {
    font: 9px var(--font-mono);
    text-transform: uppercase;
    display: flex;
    gap: 6px;
    align-items: center;
    color: var(--text-3);
    margin: 10px 0 6px;
  }
  .node-status span {
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: currentColor;
  }
  .working .node-status {
    color: var(--live);
  }
  .agent-node p {
    font-size: 11px;
    color: var(--text-2);
    overflow-wrap: anywhere;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .node-count {
    display: block;
    font: 9px var(--font-mono);
    color: var(--text-3);
    margin-top: 12px;
  }
  .node-count span {
    margin: 0 5px;
  }
  .authority {
    display: flex;
    align-items: center;
    gap: 7px;
    margin: 22px 0 3px;
    color: var(--text-3);
    font-size: 10px;
  }
  .artifact-inspector {
    min-width: 0;
    background: var(--surface-spine);
  }
  .inspector-tabs {
    display: flex;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding: 0 16px;
    gap: 15px;
  }
  .inspector-tabs button {
    border: 0;
    border-bottom: 1px solid transparent;
    background: transparent;
    border-radius: 0;
    font-size: 11px;
    padding: 12px 0;
    color: var(--text-3);
  }
  .inspector-tabs .chosen {
    color: var(--text-1);
    border-color: var(--accent);
  }
  .inspector-tabs .trace-link {
    margin-left: auto;
  }
  .inspector-tabs span {
    font: 9px var(--font-mono);
    color: var(--accent-soft);
  }
  .inspector-body {
    padding: 24px;
    max-height: 560px;
    overflow: auto;
  }
  h2 {
    margin: 10px 0 20px;
    font-size: 19px;
    line-height: 1.5;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  h3 {
    font-size: 11px;
    color: var(--text-3);
    margin: 22px 0 10px;
  }
  ul {
    padding-left: 18px;
    font-size: 12px;
    line-height: 1.7;
    color: var(--text-2);
  }
  li {
    margin: 8px 0;
    overflow-wrap: anywhere;
  }
  .requirements {
    list-style: none;
    padding: 0;
  }
  .requirements li {
    display: flex;
    gap: 10px;
  }
  .requirements li span {
    min-width: 5px;
    height: 5px;
    background: var(--accent);
    margin-top: 8px;
  }
  .scope {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    font-size: 12px;
  }
  .scope code {
    border: 1px solid var(--border);
    padding: 5px 8px;
    border-radius: 3px;
    overflow-wrap: anywhere;
  }
  .agent-output {
    color: var(--text-2);
    font-size: 12px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .artifact-files {
    display: grid;
    gap: 1px;
    margin-bottom: 16px;
  }
  .artifact-files button {
    justify-content: start;
    border: 0;
    border-bottom: 1px solid var(--border);
    border-radius: 0;
    padding: 12px 0;
    background: transparent;
  }
  .artifact-files code {
    flex: 1;
    text-align: left;
    overflow-wrap: anywhere;
  }
  .artifact-files p,
  .waiting {
    color: var(--text-3);
    font-size: 12px;
  }
  .artifact-action {
    margin-top: 20px;
  }
  .check-summary {
    margin: 18px 0;
    width: 100%;
    justify-content: space-between;
    flex-wrap: wrap;
  }
  .check-summary span {
    flex: 1;
    font-size: 11px;
    text-align: left;
  }
  .finding {
    padding: 14px 0;
    border-top: 1px solid var(--border);
  }
  .finding > span {
    font: 9px var(--font-mono);
    color: var(--warning);
    text-transform: uppercase;
  }
  .finding button {
    border: 0;
    background: none;
    padding: 0 8px;
    max-width: 100%;
  }
  .finding code {
    overflow-wrap: anywhere;
  }
  .finding p,
  .revision {
    color: var(--text-2);
    font-size: 12px;
  }
  .revision {
    margin-top: 16px;
    color: var(--text-3);
  }
  .remaining-list {
    padding: 0;
    list-style: none;
  }
  .remaining-list li {
    display: flex;
    gap: 12px;
    align-items: center;
    font-size: 12px;
  }
  .remaining-list small {
    display: block;
    color: var(--text-3);
    font-size: 10px;
    margin-top: 4px;
  }
  .decision-hint {
    border-left: 2px solid var(--live);
    padding: 12px;
    background: var(--warning-bg);
    margin-top: 20px;
    font-size: 12px;
  }
  .decision-hint p {
    color: var(--text-3);
    margin-top: 6px;
  }
  .execution-footer {
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 14px 20px;
  }
  .execution-footer > span {
    font: 9px var(--font-mono);
    color: var(--accent-soft);
    flex-shrink: 0;
  }
  .execution-footer p {
    color: var(--text-2);
    font-size: 11px;
    overflow-wrap: anywhere;
  }
  .execution-footer .operations {
    margin-left: auto;
    color: var(--text-3);
  }
  @container (max-width:620px) {
    .execution-grid {
      grid-template-columns: minmax(0, 1fr);
    }
    .agent-map {
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }
    .agent-chain {
      display: flex;
      overflow: auto;
      padding: 3px;
      gap: 12px;
    }
    .agent-node {
      min-width: 190px;
      width: 190px;
    }
    .agent-node + .agent-node::before {
      display: none;
    }
    .authority {
      margin-top: 14px;
    }
    .execution-bar {
      align-items: start;
      flex-direction: column;
      gap: 8px;
    }
    .inspector-body {
      padding: 18px;
      max-height: 460px;
    }
    .execution-footer {
      align-items: start;
      flex-wrap: wrap;
      gap: 8px;
    }
    .execution-footer .operations {
      display: none;
    }
  }
</style>
